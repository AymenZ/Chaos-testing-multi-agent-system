# swarm/guardrail.py
"""Guardrail: the one place where an agent's wishes meet the manifest's hard limits. Plain code.

Takes the tester's cards (relative: "1.5x capacity for 60 s") and produces experiments with exact
numbers the runner can execute, or rejects a card with a reason.

Policy:
- Too big is clamped, not refused: rate to the manifest limits, duration to what fits the time
  budget. Every change is written on the experiment, so reports can say "asked 4x, ran at 3x".
- Refused only when structurally wrong: unrunnable endpoint, host not allowed, over the experiment budget.
- Credentials stay as {{placeholders}} in the recipe; the runner fills them in at the last moment.
"""
import math
from dataclasses import dataclass, field
from typing import Any, Literal
from urllib.parse import urlparse

from swarm.baseline import CapacityContext
from swarm.brief import Brief, Signal
from swarm.card import Card
from swarm.manifest import Manifest, Strict
from swarm.tester import runnable_endpoints

MIN_HOLD_S = 10          # shorter than this collects too few requests to say anything
MIN_RAMP_S = 1           # the load script always ramps at least 1 s so the hold starts exactly at the target rate
WARMUP_FRACTION = 0.5    # warmup runs at this fraction of capacity (never above the target rate)


class Experiment(Strict):
    """A card with every number resolved. Nothing here is relative any more."""
    id: str
    hypothesis: str
    risk_id: str | None
    endpoint: str
    method: str
    path: str
    auth_role: Literal["none", "user", "admin"]
    query: dict[str, str | int | float | bool]
    json_body: dict[str, Any] | None
    form_body: dict[str, str] | None
    target_rps: float
    warmup_rps: float
    warmup_s: int
    ramp_s: int
    hold_s: int
    min_delivered: float
    abort_error_rate: float
    signals: list[Signal]
    max_vus: int
    timeout_s: float
    adjustments: list[str] = []

    @property
    def total_s(self) -> int:
        return self.warmup_s + self.ramp_s + self.hold_s


@dataclass(frozen=True)
class Rejection:
    card_id: str
    reason: str


@dataclass
class GuardrailResult:
    experiments: list[Experiment] = field(default_factory=list)
    rejected: list[Rejection] = field(default_factory=list)


def apply_guardrail(cards: list[Card], brief: Brief, capacity: CapacityContext, m: Manifest,
                    already_planned: int = 0) -> GuardrailResult:
    """`already_planned` counts experiments from earlier rounds, so the budget holds across retries."""
    limits = m.limits
    warmup_s = m.baseline.warmup_s
    tail_s = math.ceil(limits.request_timeout_s)          # in-flight requests need time to finish
    avail_s = limits.max_duration_s - warmup_s - tail_s   # what ramp + hold may use
    if avail_s < 2 * MIN_HOLD_S:
        raise ValueError(f"manifest leaves only {avail_s}s for the experiment itself "
                         f"(max_duration_s {limits.max_duration_s} - warmup {warmup_s} - tail {tail_s})")

    runnable = runnable_endpoints(brief)
    remaining = max(m.budgets.max_experiments - already_planned, 0)
    result = GuardrailResult()

    for card in cards:
        def reject(reason: str) -> None:
            result.rejected.append(Rejection(card.id, reason))

        if len(result.experiments) >= remaining:
            reject(f"experiment budget of {m.budgets.max_experiments} is used up")
            continue
        note = runnable.get(card.endpoint)
        if note is None:
            reject(f"endpoint '{card.endpoint}' is not runnable")
            continue
        method, path = card.endpoint.split(" ", 1)
        if urlparse(str(m.target.base_url).rstrip("/") + path).hostname not in m.allowed_hosts:
            reject("target host is not on the allowlist")
            continue

        notes: list[str] = []
        multiple = min(card.load_multiple, capacity.max_multiple)
        if multiple < card.load_multiple:
            notes.append(f"load: {card.load_multiple}x -> {multiple}x capacity (limit allows {capacity.max_multiple}x)")
        target_rps = max(min(multiple * capacity.rps, limits.max_rps), 1.0)

        ramp, hold = max(card.ramp_s, MIN_RAMP_S), card.hold_s
        if hold < MIN_HOLD_S:
            notes.append(f"duration: hold {hold}s -> {MIN_HOLD_S}s (too short to collect enough requests)")
            hold = MIN_HOLD_S
        # The hold is the measured part, so it is protected: the ramp gives way first (at most half the
        # budget), and only then is the hold shortened. Both stay >= MIN_HOLD_S because avail >= 2 * MIN_HOLD_S.
        if ramp > avail_s // 2:
            notes.append(f"duration: ramp {ramp}s -> {avail_s // 2}s (a ramp may use at most half of the {avail_s}s available)")
            ramp = avail_s // 2
        if ramp + hold > avail_s:
            notes.append(f"duration: hold {hold}s -> {avail_s - ramp}s "
                         f"(only {avail_s}s fit after {warmup_s}s warmup and {tail_s}s tail)")
            hold = avail_s - ramp

        recipe = note.recipe
        result.experiments.append(Experiment(
            id=card.id, hypothesis=card.hypothesis, risk_id=card.risk_id, endpoint=card.endpoint,
            method=method, path=path, auth_role=recipe.auth_role, query=recipe.query,
            json_body=recipe.json_body, form_body=recipe.form_body,
            target_rps=target_rps, warmup_rps=min(target_rps, WARMUP_FRACTION * capacity.rps),
            warmup_s=warmup_s, ramp_s=ramp, hold_s=hold,
            min_delivered=card.min_delivered, abort_error_rate=card.abort_error_rate, signals=card.signals,
            max_vus=limits.max_vus, timeout_s=limits.request_timeout_s, adjustments=notes,
        ))
    return result
