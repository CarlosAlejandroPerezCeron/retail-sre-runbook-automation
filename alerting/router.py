"""Alert routing engine — severity-based escalation to Slack/PagerDuty with deduplication."""
from __future__ import annotations
import hashlib, json, logging, os, time
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional
import httpx

log = logging.getLogger(__name__)

@dataclass
class Alert:
    name: str; service: str; severity: str
    labels: Dict[str, str] = field(default_factory=dict)
    annotations: Dict[str, str] = field(default_factory=dict)
    fingerprint: str = ""
    def __post_init__(self):
        if not self.fingerprint:
            raw = json.dumps({"name": self.name, "service": self.service, "labels": self.labels}, sort_keys=True)
            self.fingerprint = hashlib.md5(raw.encode()).hexdigest()

@dataclass
class RoutingResult:
    alert: Alert; routed_to: List[str]; runbook_triggered: Optional[str]
    deduplicated: bool = False; inhibited: bool = False

SEVERITY_CHANNELS = {
    "critical": ["#sre-incidents", "#sre-oncall"],
    "warning":  ["#sre-incidents"],
    "info":     ["#sre-monitoring"],
}
INHIBITION_PARENTS = {"PaymentServiceDown", "DatabaseClusterDown", "RegionalOutage"}

class AlertRouter:
    def __init__(self, slack_webhook_url=None, pagerduty_routing_key=None, redis_client=None, dedup_ttl=300) -> None:
        self._slack_url = slack_webhook_url or os.getenv("SLACK_WEBHOOK_URL","")
        self._pd_key = pagerduty_routing_key or os.getenv("PAGERDUTY_ROUTING_KEY","")
        self._redis = redis_client; self._dedup_ttl = dedup_ttl
        self._active_alerts: Dict[str, float] = {}

    def _is_duplicate(self, alert: Alert) -> bool:
        key = f"sre:alert:dedup:{alert.fingerprint}"
        if self._redis: return bool(self._redis.exists(key))
        fired_at = self._active_alerts.get(alert.fingerprint)
        return bool(fired_at and (time.time() - fired_at) < self._dedup_ttl)

    def _mark_fired(self, alert: Alert) -> None:
        if self._redis: self._redis.setex(f"sre:alert:dedup:{alert.fingerprint}", self._dedup_ttl, "1")
        else: self._active_alerts[alert.fingerprint] = time.time()

    def _is_inhibited(self, alert: Alert) -> bool:
        if not self._redis: return False
        return any(self._redis.exists(f"sre:alert:active:{p}") for p in INHIBITION_PARENTS if p != alert.name)

    def _mark_active(self, alert: Alert) -> None:
        if self._redis and alert.name in INHIBITION_PARENTS:
            self._redis.setex(f"sre:alert:active:{alert.name}", 3600, "1")

    def _send_slack(self, alert: Alert, channels: List[str]) -> bool:
        if not self._slack_url: return False
        emoji = {"critical": ":rotating_light:", "warning": ":warning:", "info": ":information_source:"}.get(alert.severity,"")
        text = f"{emoji} *[{alert.severity.upper()}] {alert.name}*\nService: `{alert.service}`\n" + "\n".join(f"* {k}: {v}" for k,v in (alert.annotations or {}).items())
        ok = True
        with httpx.Client(timeout=10) as c:
            for ch in channels:
                try:
                    resp = c.post(self._slack_url, json={"channel": ch, "text": text})
                    if not resp.is_success: ok = False
                except Exception as e:
                    log.error("Slack error: %s", e); ok = False
        return ok

    def _send_pagerduty(self, alert: Alert) -> bool:
        if not self._pd_key: return False
        payload = {"routing_key": self._pd_key, "event_action": "trigger", "dedup_key": alert.fingerprint,
                   "payload": {"summary": f"[{alert.severity.upper()}] {alert.name} — {alert.service}",
                               "severity": alert.severity, "source": alert.service, "custom_details": alert.labels}}
        try:
            with httpx.Client(timeout=10) as c:
                resp = c.post("https://events.pagerduty.com/v2/enqueue", json=payload)
            return resp.is_success
        except Exception as e:
            log.error("PagerDuty error: %s", e); return False

    def route(self, alert: Alert, runbook_name: Optional[str] = None) -> RoutingResult:
        result = RoutingResult(alert=alert, routed_to=[], runbook_triggered=runbook_name)
        if self._is_inhibited(alert): result.inhibited = True; return result
        if self._is_duplicate(alert): result.deduplicated = True; return result
        self._mark_fired(alert); self._mark_active(alert)
        channels = SEVERITY_CHANNELS.get(alert.severity, ["#sre-monitoring"])
        self._send_slack(alert, channels); result.routed_to.extend(channels)
        if alert.severity == "critical" and self._send_pagerduty(alert):
            result.routed_to.append("pagerduty")
        return result
