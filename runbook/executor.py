"""YAML-driven runbook executor with retry + Redis audit log."""
from __future__ import annotations
import json, logging, os, subprocess, time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional
import httpx, yaml

log = logging.getLogger(__name__)

@dataclass
class StepResult:
    step_name: str; step_type: str; success: bool
    output: str = ""; error: str = ""; duration_ms: float = 0.0; retries: int = 0

@dataclass
class RunbookExecution:
    runbook_name: str; service: str; alert_name: str; severity: str
    triggered_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    steps: List[StepResult] = field(default_factory=list)
    success: bool = False; total_duration_ms: float = 0.0

    def to_dict(self) -> dict:
        return {"runbook": self.runbook_name, "service": self.service, "alert": self.alert_name,
                "severity": self.severity, "triggered_at": self.triggered_at.isoformat(),
                "success": self.success, "duration_ms": round(self.total_duration_ms, 1),
                "steps": [{"name": s.step_name, "type": s.step_type, "success": s.success,
                            "output": s.output[:500], "error": s.error[:500],
                            "duration_ms": round(s.duration_ms, 1), "retries": s.retries} for s in self.steps]}

def _exec_http(step: dict) -> StepResult:
    name = step["name"]
    try:
        with httpx.Client(timeout=step.get("timeout", 30)) as c:
            resp = c.request(step.get("method","GET"), step["url"], json=step.get("body"), headers=step.get("headers",{}))
        return StepResult(name, "http", resp.is_success, output=f"HTTP {resp.status_code}: {resp.text[:300]}", error="" if resp.is_success else f"Non-2xx: {resp.status_code}")
    except Exception as e:
        return StepResult(name, "http", False, error=str(e))

def _exec_shell(step: dict) -> StepResult:
    name = step["name"]
    try:
        r = subprocess.run(step["command"], shell=True, capture_output=True, text=True,
                           timeout=step.get("timeout",60), env={**os.environ, **step.get("env",{})})
        return StepResult(name, "shell", r.returncode==0, output=r.stdout[:500], error=r.stderr[:500] if r.returncode!=0 else "")
    except subprocess.TimeoutExpired:
        return StepResult(name, "shell", False, error="Timeout")
    except Exception as e:
        return StepResult(name, "shell", False, error=str(e))

def _exec_ansible(step: dict) -> StepResult:
    ev = " ".join(f"{k}={v}" for k,v in step.get("extra_vars",{}).items())
    cmd = f"ansible-playbook {step['playbook']} -i {step.get('inventory','inventory/production')}"
    if ev: cmd += f" -e '{ev}'"
    return _exec_shell({"name": step["name"], "command": cmd, "timeout": step.get("timeout",300)})

def _exec_slack(step: dict) -> StepResult:
    url = step.get("webhook_url") or os.getenv("SLACK_WEBHOOK_URL","")
    if not url:
        return StepResult(step["name"], "slack", False, error="SLACK_WEBHOOK_URL not configured")
    try:
        with httpx.Client(timeout=10) as c:
            resp = c.post(url, json={"channel": step.get("channel","#sre-incidents"), "text": step.get("message","")})
        return StepResult(step["name"], "slack", resp.is_success, output=resp.text[:200])
    except Exception as e:
        return StepResult(step["name"], "slack", False, error=str(e))

STEP_HANDLERS = {"http": _exec_http, "shell": _exec_shell, "ansible": _exec_ansible, "slack": _exec_slack}

class RunbookExecutor:
    def __init__(self, runbook_dir="runbook/runbooks", redis_client=None, max_retries=2, backoff_base=2.0) -> None:
        self._dir = Path(runbook_dir); self._redis = redis_client
        self._max_retries = max_retries; self._backoff_base = backoff_base
        self._catalog: Dict[str, dict] = {}
        self._load_catalog()

    def _load_catalog(self) -> None:
        if not self._dir.exists(): return
        for path in self._dir.glob("*.yml"):
            try:
                rb = yaml.safe_load(path.open()); name = rb.get("name") or path.stem
                self._catalog[name] = rb
            except Exception as e:
                log.error("Failed to load runbook %s: %s", path, e)

    def list_runbooks(self) -> List[str]:
        return list(self._catalog.keys())

    def find_for_alert(self, alert_name: str, service: str) -> Optional[str]:
        for name, rb in self._catalog.items():
            if alert_name in rb.get("triggers",[]) or service in rb.get("triggers",[]): return name
        for name in self._catalog:
            if alert_name.lower() in name.lower() or service.lower() in name.lower(): return name
        return None

    def execute(self, runbook_name: str, service: str, alert_name: str, severity: str = "critical") -> RunbookExecution:
        rb = self._catalog.get(runbook_name)
        if not rb: raise KeyError(f"Runbook '{runbook_name}' not found")
        exe = RunbookExecution(runbook_name=runbook_name, service=service, alert_name=alert_name, severity=severity)
        t0 = time.perf_counter()
        for step_def in rb.get("steps",[]):
            handler = STEP_HANDLERS.get(step_def.get("type","shell"))
            if not handler:
                exe.steps.append(StepResult(step_def.get("name","?"), step_def.get("type","?"), False, error="Unknown type")); continue
            result = None
            for attempt in range(self._max_retries+1):
                st = time.perf_counter(); result = handler(step_def)
                result.duration_ms = (time.perf_counter()-st)*1000; result.retries = attempt
                if result.success: break
                if attempt < self._max_retries: time.sleep(self._backoff_base**attempt)
            exe.steps.append(result)
            if not result.success and step_def.get("on_failure","continue") == "abort": break
        exe.total_duration_ms = (time.perf_counter()-t0)*1000
        exe.success = all(s.success for s in exe.steps)
        self._persist(exe); return exe

    def _persist(self, exe: RunbookExecution) -> None:
        if self._redis:
            try:
                self._redis.lpush("sre:executions", json.dumps(exe.to_dict()))
                self._redis.ltrim("sre:executions", 0, 999)
            except Exception as e:
                log.warning("Failed to persist: %s", e)
