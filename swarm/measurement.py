from pydantic import Field
from swarm.manifest import Strict


class Measurement(Strict):
    """Numbers from one measured window (warmup already excluded by the runner)."""
    target_rps: float = Field(gt=0)
    duration_s: float = Field(gt=0)
    requests: int = Field(ge=0)
    achieved_rps: float = Field(ge=0)
    p50_ms: float = Field(ge=0)
    p95_ms: float = Field(ge=0)
    p99_ms: float = Field(ge=0)
    error_rate: float = Field(ge=0, le=1)
    status_counts: dict[int, int] = {}
    timeouts: int = Field(0, ge=0)
    dropped_iterations: int = Field(0, ge=0)
    loadgen_cpu_max_pct: float | None = None   # the validator reads these two
    host_cpu_max_pct: float | None = None