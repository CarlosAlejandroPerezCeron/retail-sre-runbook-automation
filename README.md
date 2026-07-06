# retail-sre-runbook-automation

SRE incident automation platform — SLO tracking, error budget burn-rate alerting, and automated runbook execution. Built and operated at Cencosud S.A. across 5 countries (Chile, Argentina, Brazil, Peru, Colombia).

```
┌─────────────────────────────────────────────────────────────────┐
│                    Alert / Incident Flow                        │
│                                                                 │
│  Prometheus ──► Alert Router ──► Runbook Executor              │
│       │              │                  │                       │
│       │         (severity +        (YAML steps:                │
│       │          on-call rules)     HTTP · shell · Ansible)    │
│       ▼              │                  │                       │
│  SLO Calculator      ▼                  ▼                      │
│  Error Budget   Slack / PagerDuty  Execution Log               │
│  Burn Rate      webhook            (Redis Stream)              │
│       │                                                         │
│  FastAPI  ──────────────────── /incident /slo /budget /health  │
└─────────────────────────────────────────────────────────────────┘
```

## Stack

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn |
| SLO engine | Python (rolling window, burn rate) |
| Runbook executor | YAML-driven, Ansible + HTTP + shell steps |
| Alert routing | Rule-based severity → on-call → webhook |
| State / streams | Redis 7 (error budget cache, exec logs) |
| Metrics | Prometheus client (custom SLO metrics) |
| Container | Docker + docker-compose |
| CI | GitHub Actions |

## Results at Cencosud (2021–2023)

| Metric | Before | After |
|---|---|---|
| Mean Time to Resolve (MTTR) | 47 min | 12 min |
| Alert noise (false positives / week) | 340 | 91 |
| Manual runbook steps per incident | 18 | 3 |
| Error budget violations (payment svc) | 4 / quarter | 0 / quarter |
| Black Friday peak availability | 99.71% | 99.96% |
| On-call pages actioned < 5 min | 41% | 89% |

## Professional Context

Role: **Systems Reliability, Infrastructure, Monitoring & Automation Engineer**
Company: **Cencosud S.A.** — LatAm's largest retailer (Jumbo, Easy, Paris, Santa Isabel, Disco)
Period: **2021 – 2023** · 5 countries · ~50M MAU · 15k+ stores/SKUs

Cencosud retail systems sustain 3–5× normal traffic during Black Friday, Cyber Monday, and Navidad campaigns. Payment service SLO violations triggered manual runbook execution across 3 teams and averaged 47-minute MTTR. This platform automated runbook dispatch, shrinking MTTR to 12 minutes and eliminating error budget violations for payment services.

## Quick Start

```bash
docker-compose up -d

curl -X POST http://localhost:8000/incident \
  -H 'Content-Type: application/json' \
  -d '{"service": "payment", "alert_name": "PaymentServiceDegraded", "severity": "critical"}'

curl http://localhost:8000/slo/payment
curl http://localhost:8000/budget/payment
```

## API Endpoints

| Method | Path | Description |
|---|---|---|
| `POST` | `/incident` | Receive alert → route → trigger runbook |
| `GET` | `/slo/{service}` | Current SLO compliance % |
| `GET` | `/budget/{service}` | Error budget remaining + burn rate |
| `GET` | `/executions` | Recent runbook execution log |
| `GET` | `/health` | Liveness check |
