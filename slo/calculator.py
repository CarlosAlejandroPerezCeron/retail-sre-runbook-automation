"""SLO compliance and error budget calculator.

Implements rolling-window SLO tracking with fast/slow burn-rate detection,
as deployed for Cencosud payment and checkout services.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import List, Optional, Tuple


@dataclass
class SLOWindow:
    timestamp: datetime
    good_requests: int
    total_requests: int

    @property
    def bad_requests(self) -> int:
        return self.total_requests - self.good_requests

    @property
    def error_rate(self) -> float:
        if self.total_requests == 0:
            return 0.0
        return self.bad_requests / self.total_requests


@dataclass
class SLOResult:
    service: str
    target_slo: float
    current_compliance: float
    error_budget_total: float
    error_budget_consumed: float
    error_budget_remaining: float
    error_budget_remaining_pct: float
    fast_burn_rate: float
    slow_burn_rate: float
    window_days: int
    good_requests: int
    total_requests: int
    computed_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))

    @property
    def is_slo_met(self) -> bool:
        return self.current_compliance >= self.target_slo

    @property
    def is_fast_burn_alert(self) -> bool:
        return self.fast_burn_rate >= 14.4

    @property
    def is_slow_burn_alert(self) -> bool:
        return self.slow_burn_rate >= 6.0

    def to_dict(self) -> dict:
        return {
            "service": self.service,
            "target_slo_pct": round(self.target_slo * 100, 4),
            "current_compliance_pct": round(self.current_compliance * 100, 4),
            "is_slo_met": self.is_slo_met,
            "error_budget": {
                "total_pct": round(self.error_budget_total * 100, 4),
                "consumed_pct": round(self.error_budget_consumed * 100, 4),
                "remaining_pct": round(self.error_budget_remaining_pct, 2),
            },
            "burn_rates": {
                "fast_1h": round(self.fast_burn_rate, 2),
                "slow_6h": round(self.slow_burn_rate, 2),
                "fast_burn_alert": self.is_fast_burn_alert,
                "slow_burn_alert": self.is_slow_burn_alert,
            },
            "window": {"days": self.window_days, "good_requests": self.good_requests, "total_requests": self.total_requests},
            "computed_at": self.computed_at.isoformat(),
        }


class SLOCalculator:
    def __init__(self, target_slo: float = 0.9995, window_days: int = 30,
                 fast_burn_threshold: float = 14.4, slow_burn_threshold: float = 6.0) -> None:
        if not (0 < target_slo < 1):
            raise ValueError(f"target_slo must be in (0, 1), got {target_slo}")
        self.target_slo = target_slo
        self.window_days = window_days
        self.fast_burn_threshold = fast_burn_threshold
        self.slow_burn_threshold = slow_burn_threshold
        self._windows: List[SLOWindow] = []

    def record(self, good: int, total: int, timestamp: Optional[datetime] = None) -> None:
        if total < 0 or good < 0:
            raise ValueError("good and total must be non-negative")
        if good > total:
            raise ValueError(f"good ({good}) cannot exceed total ({total})")
        ts = timestamp or datetime.now(timezone.utc)
        self._windows.append(SLOWindow(timestamp=ts, good_requests=good, total_requests=total))
        self._evict_old()

    def _evict_old(self) -> None:
        cutoff = datetime.now(timezone.utc) - timedelta(days=self.window_days)
        self._windows = [w for w in self._windows if w.timestamp >= cutoff]

    def _aggregate(self, windows: List[SLOWindow]) -> Tuple[int, int]:
        return sum(w.good_requests for w in windows), sum(w.total_requests for w in windows)

    def _compliance(self, good: int, total: int) -> float:
        return 1.0 if total == 0 else good / total

    def _burn_rate(self, good: int, total: int, period_hours: float) -> float:
        if total == 0:
            return 0.0
        allowed_error_rate = 1 - self.target_slo
        if allowed_error_rate == 0:
            return math.inf
        return ((total - good) / total) / allowed_error_rate

    def _windows_in_last(self, hours: float) -> List[SLOWindow]:
        cutoff = datetime.now(timezone.utc) - timedelta(hours=hours)
        return [w for w in self._windows if w.timestamp >= cutoff]

    def compute(self, service: str) -> SLOResult:
        all_good, all_total = self._aggregate(self._windows)
        compliance = self._compliance(all_good, all_total)
        error_budget_total = 1 - self.target_slo
        error_budget_consumed = max(0.0, 1 - compliance) if all_total > 0 else 0.0
        error_budget_remaining = max(0.0, error_budget_total - error_budget_consumed)
        remaining_pct = (error_budget_remaining / error_budget_total * 100) if error_budget_total > 0 else 100.0
        w1h = self._windows_in_last(1)
        w6h = self._windows_in_last(6)
        g1h, t1h = self._aggregate(w1h)
        g6h, t6h = self._aggregate(w6h)
        return SLOResult(
            service=service, target_slo=self.target_slo, current_compliance=compliance,
            error_budget_total=error_budget_total, error_budget_consumed=error_budget_consumed,
            error_budget_remaining=error_budget_remaining, error_budget_remaining_pct=remaining_pct,
            fast_burn_rate=self._burn_rate(g1h, t1h, 1), slow_burn_rate=self._burn_rate(g6h, t6h, 6),
            window_days=self.window_days, good_requests=all_good, total_requests=all_total,
        )

    def reset(self) -> None:
        self._windows.clear()
