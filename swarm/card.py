# swarm/card.py
"""The experiment card: the contract between the request tester and everything below it.

A card says WHAT to test, never how hard in absolute terms and never what counts as a pass.
- Load is a multiple of measured capacity; the Guardrail turns it into a rate and clamps it to the
  manifest limits.
- The request to send is not on the card: code attaches the brief's recipe for `endpoint`.
- Pass/fail thresholds live in the manifest, so the agent that designs the test cannot move them.
"""
from pydantic import Field

from swarm.brief import Signal
from swarm.manifest import Strict


class Card(Strict):
    id: str = Field(pattern=r"^[a-z0-9][a-z0-9_-]{2,48}$")
    hypothesis: str = Field(min_length=15, description="what we expect to see, and why it might break")
    endpoint: str = Field(description="'METHOD /path', exactly as listed in the runnable endpoints")
    risk_id: str | None = Field(default=None, description="the planner risk this card tests, if any")
    load_multiple: float = Field(gt=0, le=10, description="load as a multiple of measured capacity; 1.0 = capacity")
    hold_s: int = Field(gt=0, description="seconds held at full load")
    ramp_s: int = Field(ge=0, description="seconds spent ramping up before the hold")
    min_delivered: float = Field(default=0.9, gt=0, le=1,
                                 description="fraction of the target load that must actually be delivered for the test to count")
    signals: list[Signal] = Field(min_length=1, description="what would be observable if the hypothesis is true")
    abort_error_rate: float = Field(default=0.5, gt=0, le=1, description="stop early above this error share")


class CardSet(Strict):
    cards: list[Card] = Field(min_length=1)
