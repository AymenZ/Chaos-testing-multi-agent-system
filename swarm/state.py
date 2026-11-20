# swarm/state.py
"""What flows between the nodes. Each node returns only the keys it changes.

`results` and `rejected` only ever grow (their reducer appends); every other key is overwritten.
"""
import operator
from typing import Annotated, TypedDict

from swarm.brief import Brief
from swarm.calibration import BaselineRun
from swarm.card import Card
from swarm.guardrail import Experiment, Rejection
from swarm.measurement import Measurement
from swarm.preflight import PreflightResult
from swarm.report import ExperimentResult, Report
from swarm.tester import Feedback
from swarm.validator import Validity


class SwarmState(TypedDict, total=False):
    # --- the run as a whole -------------------------------------------------------------
    started_at: float                 # monotonic clock, for the wall-clock budget
    preflight: PreflightResult
    brief: Brief
    baseline: BaselineRun
    error: str                        # why the run stopped early (agent or calibration failure)
    truncated: bool                   # a budget cut the run short
    report: Report

    # --- the tester's work, across redesign rounds ----------------------------------------
    cards: dict[str, Card]            # every card designed so far, by id
    experiments: dict[str, Experiment]
    planned: int                      # experiments created so far, against the manifest budget
    new_cards: list[Card]             # the tester's latest output, waiting for the guardrail
    queue: list[Experiment]           # experiments waiting to run
    feedback: list[Feedback]          # cards judged unfair by design, waiting for the tester
    redesigns: dict[str, int]         # redesign rounds used per card id
    rejected: Annotated[list[Rejection], operator.add]

    # --- the experiment being run now -----------------------------------------------------
    current: Experiment | None
    attempt: int
    measurement: Measurement | None   # None when the run could not be carried out
    validity: Validity | None
    reasons: list[str]                # why the last run produced no measurement

    results: Annotated[list[ExperimentResult], operator.add]
