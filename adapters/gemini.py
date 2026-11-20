# adapters/gemini.py
"""Gemini behind the interface the agents already use.

The planner and tester call `client.messages.create(...)` and read Anthropic-shaped responses (content
blocks of type text / tool_use, a stop_reason, usage). GeminiClient accepts exactly those calls,
translates them to Google's `generate_content`, and translates the reply back, so neither agent knows
which provider is behind it.

What it also does, because the free tier needs it:
- paces calls to `requests_per_minute`, and retries 429s and 5xx with backoff (daily-quota errors fail loudly);
- sanitises JSON schemas down to the subset Gemini accepts (our pydantic models still validate everything);
- keeps Gemini's own reply object (with its thought signatures) so multi-turn tool calling stays valid.
"""
import json
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace as NS
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from google import genai
from google.genai import errors, types

REFUSAL_FINISHES = {"SAFETY", "PROHIBITED_CONTENT", "BLOCKLIST", "SPII", "RECITATION"}
# JSON-schema keywords we pass through. Anything else (title, default, pattern, additionalProperties,
# minLength...) is dropped: Gemini either ignores it or rejects the schema, and pydantic checks it anyway.
SCHEMA_KEEP = {"type", "properties", "required", "description", "enum", "items", "anyOf", "minimum",
               "maximum", "minItems", "maxItems"}


class GeminiError(Exception):
    """The call to Gemini failed for good (quota, bad request, repeated server errors)."""


class BlockList(list):
    """The content blocks of one model turn. `raw` is Gemini's original reply, sent back unchanged on the
    next turn so thought signatures are preserved."""
    raw: types.Content | None = None


def clean_schema(node: Any) -> Any:
    """JSON schema -> the subset Gemini accepts. `anyOf: [X, null]` becomes `type: [X, "null"]`."""
    if isinstance(node, list):
        return [clean_schema(n) for n in node]
    if not isinstance(node, dict):
        return node
    any_of = node.get("anyOf")
    if any_of:
        non_null = [b for b in any_of if b.get("type") != "null"]
        if len(non_null) == 1 and len(any_of) == 2 and isinstance(non_null[0].get("type"), str):
            merged = {**clean_schema(non_null[0]), "type": [non_null[0]["type"], "null"]}
            if "description" in node:
                merged["description"] = node["description"]
            return merged
    out: dict[str, Any] = {}
    for key, value in node.items():
        if key not in SCHEMA_KEEP:
            continue
        if key == "properties":
            out[key] = {name: clean_schema(sub) for name, sub in value.items()}
        else:
            out[key] = clean_schema(value)
    if "properties" in out and "required" in out:
        out["required"] = [r for r in out["required"] if r in out["properties"]]
    return out


class RateLimiter:
    """Keeps calls at least 60/rpm seconds apart."""

    def __init__(self, rpm: int | None, clock: Callable[[], float] = time.monotonic,
                 sleep: Callable[[float], None] = time.sleep):
        self._gap = 60.0 / rpm if rpm else 0.0
        self._clock, self._sleep, self._last = clock, sleep, None

    def wait(self) -> None:
        if self._gap and self._last is not None:
            remaining = self._gap - (self._clock() - self._last)
            if remaining > 0:
                self._sleep(remaining)
        self._last = self._clock()


def _pacific_today() -> str:
    """Google resets daily quotas at midnight Pacific time (as I understand it), so the day is counted there."""
    try:
        tz = ZoneInfo("America/Los_Angeles")
    except ZoneInfoNotFoundError:
        tz = timezone(timedelta(hours=-8))
    return datetime.now(tz).date().isoformat()


class UsageCounter:
    """Calls made today per model, saved to a file so the count survives between runs. It counts every
    attempt, retries included, because the provider does. It only knows about calls made by this tool."""

    def __init__(self, path: Path | None, today: Callable[[], str] = _pacific_today):
        self._path, self._today = path, today
        self._data: dict[str, Any] = {"date": today(), "counts": {}}
        if path and path.exists():
            try:
                saved = json.loads(path.read_text(encoding="utf-8"))
                if saved.get("date") == today():
                    self._data = saved
            except (OSError, ValueError):
                pass                                          # a corrupt file just starts the day fresh

    def _roll(self) -> None:
        if self._data["date"] != self._today():
            self._data = {"date": self._today(), "counts": {}}

    def count(self, model: str) -> int:
        self._roll()
        return self._data["counts"].get(model, 0)

    def add(self, model: str) -> None:
        self._roll()
        self._data["counts"][model] = self._data["counts"].get(model, 0) + 1
        if self._path:
            self._path.parent.mkdir(parents=True, exist_ok=True)
            self._path.write_text(json.dumps(self._data), encoding="utf-8")


def _is_timeout(e: Exception) -> bool:
    return "timeout" in type(e).__name__.lower() or "timed out" in str(e).lower()


def _retry_after(e: Exception) -> float | None:
    """Best effort: Gemini says how long to wait, e.g. retryDelay: '12s'."""
    m = re.search(r"retry(?:Delay)?\D{0,20}?(\d+(?:\.\d+)?)\s*s", str(e), re.IGNORECASE)
    return float(m.group(1)) if m else None


class GeminiClient:
    def __init__(self, api_key: str | None = None, *, limits: dict[str, tuple[int, int]] | None = None,
                 usage_path: Path | None = None, max_retries: int = 2, backoff_s: float = 10.0,
                 timeout_s: float = 90.0, client: Any = None, sleep: Callable[[float], None] = time.sleep,
                 clock: Callable[[], float] = time.monotonic, log: Callable[[str], None] | None = None,
                 today: Callable[[], str] = _pacific_today):
        """`limits` maps a model to (requests per minute, requests per day). Calls are paced to the first
        and refused once the second is used up. Retries are kept few because each one counts."""
        self._client = client or genai.Client(api_key=api_key)
        self._limits = limits or {}
        self._limiters: dict[str, RateLimiter] = {}
        self._usage = UsageCounter(usage_path, today)
        self._clock, self._sleep_fn = clock, sleep
        self._max_retries, self._backoff_s, self._sleep = max_retries, backoff_s, sleep
        self._timeout_ms = int(timeout_s * 1000)         # a call that hangs becomes an error, then a retry
        self._log = log or (lambda msg: print(msg, file=sys.stderr, flush=True))
        self._call_names: dict[str, str] = {}        # tool call id -> function name, to answer calls later
        self._counter = 0
        self.messages = self                         # so callers can write client.messages.create(...)

    # --- the Anthropic-shaped entry point -----------------------------------------------------
    def create(self, *, model: str, max_tokens: int, messages: list, system: str | None = None,
               tools: list | None = None, output_config: dict | None = None, **_unused: Any) -> NS:
        """`_unused` swallows Anthropic-only options (cache_control, effort...) that mean nothing here."""
        config = self._config(max_tokens, system, tools, output_config)
        config.http_options = types.HttpOptions(timeout=self._timeout_ms)
        response = self._generate(model, self._contents(messages), config)
        return self._reply(response)

    # --- request ----------------------------------------------------------------------------
    @staticmethod
    def _config(max_tokens: int, system: str | None, tools: list | None, output_config: dict | None) -> types.GenerateContentConfig:
        kw: dict[str, Any] = {"max_output_tokens": max_tokens,
                              "automatic_function_calling": types.AutomaticFunctionCallingConfig(disable=True)}
        if system:
            kw["system_instruction"] = system
        if tools:
            kw["tools"] = [types.Tool(function_declarations=[
                types.FunctionDeclaration(name=t["name"], description=t.get("description", ""),
                                          parameters_json_schema=_params(t["input_schema"])) for t in tools])]
        fmt = (output_config or {}).get("format")
        if fmt:
            kw["response_mime_type"] = "application/json"
            kw["response_json_schema"] = clean_schema(fmt["schema"])
        return types.GenerateContentConfig(**kw)

    def _contents(self, messages: list) -> list[types.Content]:
        out: list[types.Content] = []
        for msg in messages:
            content = msg["content"]
            if msg["role"] == "assistant":
                if isinstance(content, BlockList) and content.raw is not None:
                    out.append(content.raw)                       # Gemini's own turn, signatures intact
                    continue
                parts = [self._assistant_part(b) for b in (content if isinstance(content, list) else [NS(type="text", text=content)])]
                out.append(types.Content(role="model", parts=parts))
            else:
                blocks = content if isinstance(content, list) else [{"type": "text", "text": content}]
                out.append(types.Content(role="user", parts=[self._user_part(b) for b in blocks]))
        return out

    @staticmethod
    def _assistant_part(block: Any) -> types.Part:
        if block.type == "tool_use":
            return types.Part(function_call=types.FunctionCall(name=block.name, args=block.input))
        return types.Part(text=block.text)

    def _user_part(self, block: dict) -> types.Part:
        if block["type"] == "tool_result":
            name = self._call_names.get(block["tool_use_id"])
            if name is None:
                raise GeminiError(f"tool result for unknown call id {block['tool_use_id']!r}")
            key = "error" if block.get("is_error") else "result"
            return types.Part.from_function_response(name=name, response={key: block["content"]})
        return types.Part(text=block["text"])

    # --- budget ---------------------------------------------------------------------------------------
    def usage_today(self, model: str) -> tuple[int, int | None]:
        """(calls this tool made today, the model's daily limit if known)."""
        lim = self._limits.get(model)
        return self._usage.count(model), (lim[1] if lim else None)

    def _admit(self, model: str) -> None:
        """Before every attempt: refuse if the day's budget is spent, pace to the per-minute limit, count it."""
        lim = self._limits.get(model)
        if lim:
            rpm, rpd = lim
            used = self._usage.count(model)
            if used >= rpd:
                raise GeminiError(f"daily budget for {model} is used up ({used} of {rpd} calls counted by this tool today). "
                                  "Switch model in the manifest or wait for the daily reset.")
            if model not in self._limiters:
                self._limiters[model] = RateLimiter(rpm, self._clock, self._sleep_fn)
            self._limiters[model].wait()
        self._usage.add(model)

    # --- calling Gemini ---------------------------------------------------------------------------
    def _generate(self, model: str, contents: list, config: types.GenerateContentConfig) -> Any:
        delay = self._backoff_s
        for attempt in range(self._max_retries + 1):
            self._admit(model)
            try:
                return self._client.models.generate_content(model=model, contents=contents, config=config)
            except (errors.ClientError, errors.ServerError) as e:
                retryable = e.code == 429 or e.code >= 500
                if e.code == 429 and re.search(r"per ?day|daily", str(e), re.IGNORECASE):
                    raise GeminiError(f"daily quota exhausted for {model}: {e.message}") from e
                if not retryable or attempt == self._max_retries:
                    raise GeminiError(f"Gemini call failed ({e.code} {e.status}): {e.message}") from e
                wait = _retry_after(e) or delay
                self._log(f"  Gemini said {e.code} {e.status}; waiting {wait:.0f}s (retry {attempt + 1}/{self._max_retries})")
                self._sleep(wait)
                delay *= 2
            except Exception as e:                       # a timeout or dropped connection: same treatment
                if not _is_timeout(e) or attempt == self._max_retries:
                    raise GeminiError(f"Gemini call failed: {type(e).__name__}: {e}") from e
                self._log(f"  Gemini call timed out after {self._timeout_ms // 1000}s; retrying ({attempt + 1}/{self._max_retries})")
        raise AssertionError("unreachable")

    # --- reply ------------------------------------------------------------------------------------
    def _reply(self, response: Any) -> NS:
        blocks, texts = BlockList(), []
        candidate = response.candidates[0] if response.candidates else None
        raw = candidate.content if candidate and candidate.content else None
        for part in (raw.parts or []) if raw else []:
            if part.thought:
                continue                                          # reasoning summary, not part of the answer
            if part.function_call:
                fc = part.function_call
                self._counter += 1
                call_id = fc.id or f"call_{self._counter}"
                self._call_names[call_id] = fc.name
                blocks.append(NS(type="tool_use", id=call_id, name=fc.name, input=dict(fc.args or {})))
            elif part.text:
                texts.append(part.text)
        if texts:
            blocks.insert(0, NS(type="text", text="".join(texts)))
        blocks.raw = raw

        finish = candidate.finish_reason.name if candidate and candidate.finish_reason else None
        blocked = bool(response.prompt_feedback and response.prompt_feedback.block_reason)
        if blocked or finish in REFUSAL_FINISHES:
            stop = "refusal"
        elif finish == "MAX_TOKENS":
            stop = "max_tokens"
        elif any(b.type == "tool_use" for b in blocks):
            stop = "tool_use"
        else:
            stop = "end_turn"                                     # includes a malformed call: the agent will nudge
        u = response.usage_metadata
        usage = NS(input_tokens=(u.prompt_token_count or 0) if u else 0,
                   output_tokens=((u.candidates_token_count or 0) + (u.thoughts_token_count or 0)) if u else 0,
                   cache_read_input_tokens=(u.cached_content_token_count or 0) if u else 0)
        return NS(content=blocks, stop_reason=stop, usage=usage)


def _params(schema: dict) -> dict | None:
    """A tool that takes no arguments is declared with no parameters at all."""
    cleaned = clean_schema(schema)
    return cleaned if cleaned.get("properties") else None
