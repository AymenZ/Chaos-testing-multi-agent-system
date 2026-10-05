"""Measurements produced by the runner and consumed by the validator and verdict."""

import statistics
from typing import ClassVar

from pydantic import BaseModel, ConfigDict, Field


class HostStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cpu_peak_pct: float  # whole host
    loadgen_cpu_peak_pct: float
    mem_free_mb_min: float
    swapping: bool = False


class RunMetrics(BaseModel):
    """Facts about one executed run. Plain data: no judgement in here."""

    model_config = ConfigDict(extra="forbid")

    requests: int = Field(ge=0)
    achieved_vus: float
    duration_s: float
    status_counts: dict[int, int] = Field(default_factory=dict)  # 0 = timeout / no response
    p50_ms: float = 0.0
    p95_ms: float = 0.0
    p99_ms: float = 0.0
    error_rate: float = 0.0  # 5xx plus timeouts, as a fraction of requests
    aborted: bool = False
    abort_reason: str | None = None
    tool_crashed: bool = False
    metrics_missing_frac: float = 0.0
    fault_confirmed: bool | None = None  # None when the spec injects no fault
    host: HostStats
    signals: dict[str, float] = Field(default_factory=dict)  # peak value per signal metric


class Baseline(BaseModel):
    """Normal behaviour with nothing wrong, summarised over several runs."""

    model_config = ConfigDict(extra="forbid")

    p95_ms_median: float
    p95_band_low: float
    p95_band_high: float  # a result at or below this is "inside the noise band"
    error_rate_median: float
    noisy: bool
    runs: int

    NOISY_SPREAD: ClassVar[float] = 0.25  # (max - min) / median above this marks it noisy

    @classmethod
    def from_runs(cls, runs: list[RunMetrics]) -> "Baseline":
        if len(runs) < 3:
            raise ValueError("baseline needs at least 3 runs to estimate a noise band")
        p95s = [r.p95_ms for r in runs]
        med = statistics.median(p95s)
        lo, hi = min(p95s), max(p95s)
        spread = hi - lo
        noisy = med > 0 and spread / med > cls.NOISY_SPREAD
        # Widen rather than tighten when noisy: never compensate by getting stricter.
        return cls(
            p95_ms_median=med,
            p95_band_low=lo,
            p95_band_high=hi + (spread if noisy else 0.0),
            error_rate_median=statistics.median(r.error_rate for r in runs),
            noisy=noisy,
            runs=len(runs),
        )
