"""Validated runtime guardrails, independent from personal facts and secrets."""

from dataclasses import asdict, dataclass, fields

import yaml

from applypilot import config


@dataclass(frozen=True)
class RuntimePolicy:
    """Conservative configurable defaults for a newly installed system."""

    min_score: int = 7
    max_per_run: int = 1
    max_per_day: int = 10
    workers: int = 1
    max_attempts: int = 3
    retry_delay_seconds: int = 60
    max_retry_delay_seconds: int = 3600
    lease_seconds: int = 900
    max_per_company: int = 2
    company_period_days: int = 7
    apply_timeout_seconds: int = 600
    poll_interval_seconds: int = 60
    discovery_interval_seconds: int = 1800
    apply_max_steps: int = 40
    apply_max_output_tokens: int = 2000

    def __post_init__(self) -> None:
        for field in fields(self):
            value = getattr(self, field.name)
            if not isinstance(value, int) or isinstance(value, bool) or value < 1:
                raise ValueError(f"runtime.{field.name} must be a positive integer")
        if self.min_score > 10:
            raise ValueError("min_score must be between 1 and 10")
        if self.lease_seconds <= self.apply_timeout_seconds:
            raise ValueError("lease_seconds must exceed apply_timeout_seconds")


def load_policy() -> RuntimePolicy:
    """Read runtime.yaml; reject typos rather than silently ignoring guardrails."""
    path = config.APP_DIR / "runtime.yaml"
    if not path.exists():
        return RuntimePolicy()
    values = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(values, dict):
        raise TypeError("runtime.yaml must contain a mapping")
    unknown = set(values) - set(asdict(RuntimePolicy()))
    if unknown:
        raise ValueError(f"Unknown runtime settings: {sorted(unknown)}")
    return RuntimePolicy(**values)
