# swarm/llm.py
"""Builds the model client the agents use, from the manifest's `llm.provider`. The agents only ever see
`client.messages.create(...)`, so adding a provider means adding a client here, not touching them."""
import os
from pathlib import Path
from typing import Any

from swarm.manifest import Manifest

KEY_VARS = {"gemini": ("GEMINI_API_KEY", "GOOGLE_API_KEY"), "anthropic": ("ANTHROPIC_API_KEY",)}


class MissingKeyError(Exception):
    pass


def make_client(m: Manifest) -> Any:
    provider = m.llm.provider
    names = KEY_VARS[provider]
    key = next((os.environ[n] for n in names if os.environ.get(n)), None)
    if not key:
        raise MissingKeyError(f"No API key found for '{provider}'. Paste it into the .env file in the project "
                              f"root as {names[0]}=... (provider is set in the manifest's llm section).")
    if provider == "gemini":
        from adapters.gemini import GeminiClient
        limits = {name: (lim.rpm, lim.rpd) for name, lim in m.llm.limits.items()}
        return GeminiClient(key, limits=limits, usage_path=Path("runs") / "llm_usage.json")
    import anthropic
    return anthropic.Anthropic(api_key=key)
