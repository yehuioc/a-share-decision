"""Loopback-only workbench, backed by independent resumable workers."""
from __future__ import annotations
import secrets
import uuid
from urllib.parse import urlsplit
from fastapi import FastAPI, UploadFile, File, Form, Request, Query
from fastapi.responses import JSONResponse, FileResponse
from starlette.middleware.trustedhost import TrustedHostMiddleware
from .store import Store, DomainError, ConflictError, PROJECT_ROOT, safe_id
from .settings import settings, update_settings
from .market import collect, latest_market, import_market, market_readiness
from .evidence import add_text, add_file, add_check, evidence_summaries, source_scores, MAX_FILE_BYTES
from .workflow import Workflow
from .ledger import Ledger
from .outcomes import PlanBook
from .packets import import_packet, latest_packet, sync_inbox, handoff_template
from .performance import summary as performance_summary, record_costs


def current_prices(store):
    market = latest_market(store)
    channel = settings(store)["channels"]["market"]
    if not channel or (market or {}).get("source_id") != channel["source_id"]:
        return {}
    return {q["symbol"]: q["price"] for q in (market or {}).get("metadata", {}).get("quotes", []) if not q.get("stale")}


def run_summary(run):
    summary = {key: run.get(key) for key in ('id','created_at','as_of','mode','symbols','status','warnings','channels','channel_review_required')}
    decision = run.get('decision')
    summary['decision'] = {k:v for k,v in decision.items() if k != 'input'} if decision else None
    summary['reviews'] = [{k:v for k,v in review.items() if k not in ('evidence','request')} for review in run.get('reviews',[])]
    return summary


def create_app(store=None, testing=False):
    store = store or Store()
    app = FastAPI(title="A 股决策工作台", docs_url=None, redoc_url=None)
    csrf_token = secrets.token_urlsafe(32)
    app.state.store = store
    app.state.csrf_token = csrf_token
    app.add_middleware(TrustedHostMiddleware, allowed_hosts=["127.0.0.1", "localhost"] + (["testserver"] if testing else []))

    @app.middleware("http")
    async def local_boundary(request: Request, call_next):
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != request.url.scheme or parsed.netloc != request.headers.get("host"):
                    return JSONResponse({"detail": "拒绝跨站写入，请从本地工作台操作"}, status_code=403)
            if not secrets.compare_digest(request.headers.get("x-csrf-token", ""), csrf_token):
                return JSONResponse({"detail": "页面会话已过期，请刷新后再试"}, status_code=403)
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["Content-Security-Policy"] = "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; frame-ancestors 'none'; base-uri 'none'; form-action 'self'"
        return response

    @app.exception_handler(DomainError)
    async def domain_error(request, error):
        return JSONResponse({"detail": str(error)}, status_code=409 if isinstance(error, ConflictError) else 400)

    @app.get("/api/state")
    def state():
        from .runner import status
        workflow = Workflow(store)
        runs = workflow.list_runs()
        ledger = Ledger(store).summary(current_prices(store))
        market = latest_market(store)
        current_channel = settings(store)["channels"]["market"] or {}
        research_market = market if (market or {}).get("source_id") == current_channel.get("source_id") else None
        packet_status = latest_packet(store)
        packet_summary = {k: v for k, v in packet_status.items() if k != "packet"}
        return {"settings": settings(store), "market": {"latest": market,
                "readiness": market_readiness(research_market, [x["symbol"] for x in settings(store)["watchlist"]])},
                "evidence": evidence_summaries(store), "sources": source_scores(store, runs),
                "runs": [run_summary(run) for run in runs], "roles": workflow.roles(), "ledger": ledger,
                "paper_ledger": Ledger(store, "paper").summary(current_prices(store)),
                "packet_status": packet_summary, "performance": performance_summary(store, ledger, runs),
                "automation": status(store),
                "research_profile": store.read("research_profile.json", {}),
                "system": {"csrf_token": csrf_token, "version": "0.2.0", "data_root": str(store.root),
                "analysis_mode": "本地独立后台直接调用 DeepSeek V4.1 Flash；无需 Codex"}}

    @app.post("/api/automation/start")
    def start_research(payload: dict):
        from .runner import create_job, launch
        return launch(store, create_job(store, payload.get("request_id") or uuid.uuid4().hex))

    @app.post("/api/automation/jobs/{job_id}/resume")
    def resume_research(job_id: str):
        from .runner import resume
        return resume(store, job_id)

    @app.put("/api/settings")
    def save_settings(payload: dict):
        return update_settings(store, payload)

    @app.post("/api/market/refresh")
    def refresh(payload: dict = {}):
        return collect(store, payload.get("request_id") or uuid.uuid4().hex)

    @app.post("/api/market/import")
    def market_import(payload: dict):
        return import_market(store, payload)

    @app.post("/api/packets/import")
    def packet_import(payload: dict):
        return import_packet(store, payload)

    @app.get("/api/packets/status")
    def packet_status():
        return latest_packet(store)

    @app.get("/api/packets/template")
    def packet_template():
        return handoff_template(store)

    @app.post("/api/packets/sync")
    def packet_sync(payload: dict = {}):
        return {"imports": sync_inbox(store), "status": latest_packet(store)}

    @app.post("/api/materials/text")
    def material_text(payload: dict):
        return add_text(store, payload)

    @app.get("/api/materials/search")
    def materials_search(q: str = "", source_id: str = "", category: str = "", issue: str = "",
                         status: str = "", offset: int = Query(0, ge=0), limit: int = Query(25, ge=1, le=100)):
        from .evidence import search_evidence
        return search_evidence(store, query=q, source_id=source_id, category=category,
                               issue=issue, status=status, offset=offset, limit=limit)

    @app.get("/api/sources/caixin")
    def caixin_source_status():
        from .caixin import caixin_status
        return caixin_status(store)

    @app.post("/api/sources/caixin/sync")
    def caixin_source_sync(payload: dict):
        from .caixin import sync_caixin
        return sync_caixin(store, request_id=payload.get("request_id") or uuid.uuid4().hex)

    @app.post("/api/materials/file")
    async def material_file(file: UploadFile = File(...), title: str = Form(...), source_id: str = Form("manual"),
                            url: str = Form(""), published_at: str = Form(""), request_id: str = Form("")):
        content = await file.read(MAX_FILE_BYTES + 1)
        await file.close()
        return add_file(store, {"title": title, "source_id": source_id, "url": url,
                               "published_at": published_at, "request_id": request_id or uuid.uuid4().hex}, file.filename or "file", content)

    @app.post("/api/source-checks")
    def source_check(payload: dict):
        return add_check(store, payload)

    @app.get("/api/evidence/{evidence_id}")
    def evidence_detail(evidence_id: str):
        value = store.read(f"evidence/{safe_id(evidence_id)}.json")
        if not value:
            raise DomainError("资料不存在")
        return value

    @app.get("/api/runs/{run_id}")
    def run_detail(run_id: str):
        return Workflow(store).get_run(run_id)

    @app.get("/api/runs/{run_id}/discussion")
    def run_discussion(run_id: str, role: str | None = None):
        return Workflow(store).discussion(run_id, role=role)

    @app.get("/api/runs/{run_id}/outcomes")
    def plan_outcomes(run_id: str):
        from .store import now_iso
        market = latest_market(store)
        channel = settings(store)["channels"]["market"]
        evidence = [market] if market and channel and market["source_id"] == channel["source_id"] else []
        return {"run_id": run_id, "outcomes": Workflow(store).plan_outcomes(run_id, evidence, now_iso())}

    @app.post("/api/runs/{run_id}/review")
    def review_run(run_id: str, payload: dict):
        return Workflow(store).review(run_id, payload)

    @app.post("/api/plans/events")
    def plan_event(payload: dict):
        return PlanBook(store).record(payload)

    @app.get("/api/roles/{role}")
    def role_detail(role: str):
        for item in Workflow(store).roles():
            if item["id"] == role:
                return item
        raise DomainError("角色不存在")

    @app.post("/api/ledger/baseline")
    def baseline(payload: dict):
        return Ledger(store).set_baseline(payload)

    @app.post("/api/ledger/events")
    def event(payload: dict):
        return Ledger(store).record_event(payload)

    @app.post("/api/ledger/valuations")
    def valuation(payload: dict):
        return Ledger(store).record_valuation(payload)

    @app.post("/api/ledger/paper/baseline")
    def paper_baseline(payload: dict):
        return Ledger(store, "paper").set_baseline(payload)

    @app.post("/api/ledger/paper/valuations")
    def paper_valuation(payload: dict):
        return Ledger(store, "paper").record_valuation(payload)

    @app.post("/api/project-costs")
    def project_costs(payload: dict):
        return record_costs(store, payload)

    @app.get("/api/performance")
    def performance(as_of: str | None = None):
        ledger = Ledger(store).summary(None if as_of else current_prices(store), as_of=as_of)
        return performance_summary(store, ledger, Workflow(store).list_runs())

    @app.get("/api/health")
    def health():
        return {"status": "ok", "product": "a-share-decision", "version": "0.2.0", "data_root": str(store.root),
                "automatic_trading": False}

    @app.get("/")
    def index():
        return FileResponse(PROJECT_ROOT / "web" / "index.html", media_type="text/html")

    # Windows registry associations can label .js as text/plain; with nosniff
    # the browser correctly refuses it. These exact assets have fixed types.
    @app.get("/static/app.js")
    def javascript():
        return FileResponse(PROJECT_ROOT / "web" / "app.js", media_type="text/javascript")

    @app.get("/static/style.css")
    def stylesheet():
        return FileResponse(PROJECT_ROOT / "web" / "style.css", media_type="text/css")
    return app
