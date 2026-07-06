"""Error budget tracker backed by Redis."""
from __future__ import annotations
import json, time
from dataclasses import dataclass
from typing import Dict, Optional
import redis

BUCKET_TTL_SECONDS = 30 * 24 * 3600

@dataclass
class ServiceSLO:
    name: str
    target: float
    window_days: int = 30

class BudgetTracker:
    def __init__(self, redis_client: redis.Redis, services: Optional[Dict[str, float]] = None, window_days: int = 30) -> None:
        self._r = redis_client
        self._window_days = window_days
        self._services: Dict[str, ServiceSLO] = {}
        if services:
            for name, target in services.items():
                self.register_service(name, target, window_days)

    def register_service(self, name: str, target: float, window_days: int = 30) -> None:
        self._services[name] = ServiceSLO(name=name, target=target, window_days=window_days)

    def _zkey(self, service: str) -> str:
        return f"slo:budget:{service}"

    def record(self, service: str, good: int, total: int, ts: Optional[float] = None) -> None:
        if service not in self._services:
            raise KeyError(f"Unknown service '{service}'. Call register_service first.")
        now = ts or time.time()
        value = json.dumps({"good": good, "total": total})
        key = self._zkey(service)
        self._r.zadd(key, {value: now})
        self._r.expire(key, BUCKET_TTL_SECONDS)
        self._evict(service)

    def _evict(self, service: str) -> None:
        slo = self._services[service]
        cutoff = time.time() - slo.window_days * 86400
        self._r.zremrangebyscore(self._zkey(service), "-inf", cutoff)

    def _fetch_window(self, service: str, hours: Optional[float] = None) -> tuple:
        slo = self._services.get(service)
        if not slo:
            return 0, 0
        min_score = time.time() - (hours * 3600 if hours else slo.window_days * 86400)
        raw = self._r.zrangebyscore(self._zkey(service), min_score, "+inf")
        good = total = 0
        for entry in raw:
            data = json.loads(entry)
            good += data["good"]
            total += data["total"]
        return good, total

    def budget_status(self, service: str) -> dict:
        slo = self._services.get(service)
        if not slo:
            return {"error": f"Unknown service: {service}"}
        good_full, total_full = self._fetch_window(service)
        good_1h, total_1h = self._fetch_window(service, hours=1)
        good_6h, total_6h = self._fetch_window(service, hours=6)
        compliance = (good_full / total_full) if total_full > 0 else 1.0
        allowed_err = 1 - slo.target
        def burn(g, t):
            if t == 0 or allowed_err == 0: return 0.0
            return ((t - g) / t) / allowed_err
        budget_consumed = max(0.0, (1 - compliance) if total_full > 0 else 0.0)
        budget_remaining_pct = max(0.0, ((allowed_err - budget_consumed) / allowed_err * 100) if allowed_err > 0 else 100.0)
        return {
            "service": service, "target_slo_pct": round(slo.target * 100, 4),
            "compliance_pct": round(compliance * 100, 4), "budget_remaining_pct": round(budget_remaining_pct, 2),
            "burn_rate_1h": round(burn(good_1h, total_1h), 2), "burn_rate_6h": round(burn(good_6h, total_6h), 2),
            "fast_burn_alert": burn(good_1h, total_1h) >= 14.4, "slow_burn_alert": burn(good_6h, total_6h) >= 6.0,
            "totals": {"good": good_full, "total": total_full},
        }

    def all_services_status(self) -> Dict[str, dict]:
        return {svc: self.budget_status(svc) for svc in self._services}
