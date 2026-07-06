"""FastAPI service — SLO tracking + automated runbook execution."""
from __future__ import annotations
import json, logging, os
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional
import redis
from fastapi import FastAPI, HTTPException, Query
from fastapi.responses import JSONResponse
from prometheus_fastapi_instrumentator import Instrumentator
from pydantic import BaseModel, Field
from slo.calculator import SLOCalculator
from slo.budget_tracker import BudgetTracker
from runbook.executor import RunbookExecutor
from alerting.router import Alert, AlertRouter

logging.basicConfig(level=logging.INFO)
log = logging.getLogger(__name__)

REDIS_HOST = os.getenv("REDIS_HOST", "localhost")
REDIS_PORT = int(os.getenv("REDIS_PORT", "6379"))
SERVICE_SLOS_RAW = os.getenv("SERVICE_SLOS", "payment:0.9995,checkout:0.999,catalog:0.995,auth:0.9999")

def _parse_slos(raw: str) -> Dict[str, float]:
    result = {}
    for part in raw.split(","):
        name, _, target = part.strip().partition(":")
        if name and target: result[name.strip()] = float(target.strip())
    return result

SERVICE_SLOS = _parse_slos(SERVICE_SLOS_RAW)
state: Dict[str, Any] = {}

@asynccontextmanager
async def lifespan(app: FastAPI):
    r = redis.Redis(host=REDIS_HOST, port=REDIS_PORT, decode_responses=True)
    state["redis"] = r
    state["tracker"] = BudgetTracker(r, services=SERVICE_SLOS)
    state["executor"] = RunbookExecutor(redis_client=r)
    state["router"] = AlertRouter(redis_client=r)
    state["calculators"] = {svc: SLOCalculator(target_slo=t) for svc, t in SERVICE_SLOS.items()}
    log.info("SRE API ready. Services: %s", list(SERVICE_SLOS.keys()))
    yield
    r.close()

app = FastAPI(title="retail-sre-runbook-automation",
              description="SLO tracking + automated runbook execution for Cencosud retail services",
              version="1.0.0", lifespan=lifespan)
Instrumentator().instrument(app).expose(app)

class IncidentRequest(BaseModel):
    service: str; alert_name: str
    severity: str = Field(default="critical", pattern="^(critical|warning|info)$")
    labels: Dict[str, str] = Field(default_factory=dict)
    annotations: Dict[str, str] = Field(default_factory=dict)
    runbook_override: Optional[str] = None

class IncidentResponse(BaseModel):
    incident_id: str; alert_name: str; service: str; severity: str
    routed_to: List[str]; runbook_triggered: Optional[str]
    deduplicated: bool; inhibited: bool
    execution_success: Optional[bool]; duration_ms: Optional[float]

class MetricsIngestionRequest(BaseModel):
    service: str; good_requests: int; total_requests: int

@app.post("/incident", response_model=IncidentResponse, tags=["incidents"])
async def receive_incident(req: IncidentRequest):
    alert = Alert(name=req.alert_name, service=req.service, severity=req.severity, labels=req.labels, annotations=req.annotations)
    executor: RunbookExecutor = state["executor"]
    router: AlertRouter = state["router"]
    runbook_name = req.runbook_override or executor.find_for_alert(req.alert_name, req.service)
    routing = router.route(alert, runbook_name)
    execution_success = duration_ms = None
    if runbook_name and not routing.deduplicated and not routing.inhibited:
        try:
            exe = executor.execute(runbook_name=runbook_name, service=req.service, alert_name=req.alert_name, severity=req.severity)
            execution_success = exe.success; duration_ms = exe.total_duration_ms
        except KeyError as e:
            log.warning("Runbook not found: %s", e)
    return IncidentResponse(incident_id=alert.fingerprint, alert_name=req.alert_name, service=req.service,
                            severity=req.severity, routed_to=routing.routed_to, runbook_triggered=runbook_name,
                            deduplicated=routing.deduplicated, inhibited=routing.inhibited,
                            execution_success=execution_success, duration_ms=duration_ms)

@app.get("/slo/{service}", tags=["slo"])
async def get_slo(service: str):
    calc = state["calculators"].get(service)
    if not calc: raise HTTPException(404, f"Unknown service '{service}'")
    return JSONResponse(calc.compute(service).to_dict())

@app.get("/budget/{service}", tags=["slo"])
async def get_budget(service: str):
    status = state["tracker"].budget_status(service)
    if "error" in status: raise HTTPException(404, status["error"])
    return JSONResponse(status)

@app.post("/metrics/ingest", tags=["slo"])
async def ingest_metrics(req: MetricsIngestionRequest):
    if req.service not in SERVICE_SLOS: raise HTTPException(404, f"Unknown service: {req.service}")
    state["tracker"].record(req.service, req.good_requests, req.total_requests)
    state["calculators"][req.service].record(req.good_requests, req.total_requests)
    return {"status": "recorded", "service": req.service}

@app.get("/executions", tags=["runbooks"])
async def get_executions(limit: int = Query(default=20, le=100)):
    raw = state["redis"].lrange("sre:executions", 0, limit - 1)
    return [json.loads(e) for e in raw]

@app.get("/runbooks", tags=["runbooks"])
async def list_runbooks():
    return {"runbooks": state["executor"].list_runbooks()}

@app.get("/health", tags=["system"])
async def health():
    r = state.get("redis"); redis_ok = False
    try: redis_ok = r.ping() if r else False
    except Exception: pass
    return {"status": "healthy" if redis_ok else "degraded", "redis": redis_ok,
            "services": list(SERVICE_SLOS.keys()), "timestamp": datetime.now(timezone.utc).isoformat()}
