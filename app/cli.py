"""Shared CLI for local data, research and account maintenance."""
from __future__ import annotations
import argparse
import json
import sys
import uuid
from pathlib import Path
from .store import Store, DomainError, now_iso, PROJECT_ROOT
from .settings import settings, update_settings
from .market import collect, latest_market, import_market
from .evidence import add_text, add_file, add_check


def payload(path):
    if not path:
        return {}
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (ValueError, OSError) as e:
        raise DomainError(f"无法读取 JSON 输入：{e}") from e


def main(argv=None):
    # JSON output must preserve source text even on Windows GBK consoles/pipes.
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description="A 股决策工作台：本地业务入口")
    parser.add_argument("--data-dir", help="隔离的数据目录；默认项目 data")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("init")
    p = commands.add_parser("collect"); p.add_argument("--symbols", nargs="+"); p.add_argument("--request-id")
    p = commands.add_parser("sync-caixin"); p.add_argument("--request-id")
    commands.add_parser("caixin-status")
    p = commands.add_parser("search"); p.add_argument("--query", default=""); p.add_argument("--source", default="")
    p.add_argument("--category", default=""); p.add_argument("--issue", default=""); p.add_argument("--status", default="")
    p.add_argument("--offset", type=int, default=0); p.add_argument("--limit", type=int, default=25)
    for name in ("import-text", "import-market", "import-packet", "plan-event", "project-costs", "source-check", "prepare", "settings"):
        p = commands.add_parser(name); p.add_argument("--input")
    p = commands.add_parser("import-file"); p.add_argument("path"); p.add_argument("--input", required=True)
    p = commands.add_parser("context"); p.add_argument("run_id"); p.add_argument("role", choices=["opportunity", "counter", "judge"]); p.add_argument("--phase", default="initial"); p.add_argument("--out")
    p = commands.add_parser("opinion"); p.add_argument("run_id"); p.add_argument("role", choices=["opportunity", "counter"]); p.add_argument("--phase", choices=["initial", "challenge"], required=True); p.add_argument("--input", required=True)
    for name in ("publish", "review"):
        p = commands.add_parser(name); p.add_argument("run_id"); p.add_argument("--input", required=name == "publish")
    p = commands.add_parser("runs"); p.add_argument("--id")
    p = commands.add_parser("discussion"); p.add_argument("run_id"); p.add_argument("--role", choices=["opportunity", "counter", "judge"])
    commands.add_parser("roles")
    commands.add_parser("packet-status")
    commands.add_parser("sync-packets")
    p = commands.add_parser("performance"); p.add_argument("--as-of")
    p = commands.add_parser("plan-outcomes"); p.add_argument("run_id")
    p = commands.add_parser("ledger"); p.add_argument("action", choices=["baseline", "event", "valuation", "summary"]); p.add_argument("--input"); p.add_argument("--mode", choices=["actual", "paper"], default="actual")
    p = commands.add_parser("serve"); p.add_argument("--port", type=int, default=8765)
    args = parser.parse_args(argv)
    store = Store(args.data_dir)
    try:
        if args.command == "init":
            store.write("settings.json", settings(store))
            result = {"data_root": str(store.root), "settings": settings(store), "account": "等待用户确认"}
        elif args.command == "settings":
            result = update_settings(store, payload(args.input)) if args.input else settings(store)
        elif args.command == "import-market":
            result = import_market(store, payload(args.input))
        elif args.command in ("import-packet", "packet-status", "sync-packets"):
            from .packets import import_packet, latest_packet, sync_inbox
            result = import_packet(store, payload(args.input)) if args.command == "import-packet" else latest_packet(store) if args.command == "packet-status" else sync_inbox(store)
        elif args.command == "plan-event":
            from .outcomes import PlanBook
            result = PlanBook(store).record(payload(args.input))
        elif args.command == "project-costs":
            from .performance import record_costs
            result = record_costs(store, payload(args.input))
        elif args.command == "performance":
            from .performance import summary
            from .ledger import Ledger
            from .workflow import Workflow
            from .server import current_prices
            result = summary(store, Ledger(store).summary(None if args.as_of else current_prices(store), as_of=args.as_of), Workflow(store).list_runs())
        elif args.command == "plan-outcomes":
            from .workflow import Workflow
            market = latest_market(store)
            channel = settings(store)["channels"]["market"]
            evidence = [market] if market and channel and market["source_id"] == channel["source_id"] else []
            result = Workflow(store).plan_outcomes(args.run_id, evidence, now_iso())
        elif args.command == "collect":
            result = collect(store, args.request_id or uuid.uuid4().hex, args.symbols)
        elif args.command == "sync-caixin":
            from .caixin import sync_caixin
            result = sync_caixin(store, request_id=args.request_id or uuid.uuid4().hex)
        elif args.command == "caixin-status":
            from .caixin import caixin_status
            result = caixin_status(store)
        elif args.command == "search":
            from .evidence import search_evidence
            result = search_evidence(store, query=args.query, source_id=args.source, category=args.category,
                                     issue=args.issue, status=args.status, offset=args.offset, limit=args.limit)
        elif args.command == "import-text":
            result = add_text(store, payload(args.input))
        elif args.command == "import-file":
            p = Path(args.path).resolve()
            result = add_file(store, payload(args.input), p.name, p.read_bytes())
        elif args.command == "source-check":
            result = add_check(store, payload(args.input))
        elif args.command == "serve":
            import uvicorn
            from .server import create_app
            uvicorn.run(create_app(store), host="127.0.0.1", port=args.port, log_level="info")
            return 0
        elif args.command == "ledger":
            from .ledger import Ledger
            ledger = Ledger(store, args.mode)
            if args.action == "summary":
                from .server import current_prices
                result = ledger.summary(current_prices(store))
            else:
                result = getattr(ledger, {"baseline": "set_baseline", "event": "record_event", "valuation": "record_valuation"}[args.action])(payload(args.input))
        else:
            from .workflow import Workflow
            workflow = Workflow(store)
            if args.command == "prepare":
                p = payload(args.input)
                p.setdefault("request_id", uuid.uuid4().hex)
                p.setdefault("mode", "live")
                p.setdefault("decision_contract", 3)
                p.setdefault("research_brief", store.read("research_profile.json", {}))
                p.setdefault("symbols", [w["symbol"] for w in settings(store)["watchlist"]])
                if "evidence_ids" not in p:
                    raise DomainError("prepare 必须显式选择本轮 evidence_ids，不能把所有材料自动塞入模型")
                if "account_snapshot" not in p:
                    from .ledger import Ledger
                    from .server import current_prices
                    p["account_snapshot"] = Ledger(store).summary(current_prices(store))
                result = workflow.prepare(p)
            elif args.command == "context":
                result = workflow.context(args.run_id, args.role, args.phase)
                if args.out:
                    out = Path(args.out).resolve()
                    if not out.is_relative_to(PROJECT_ROOT):
                        raise DomainError("上下文输出必须位于本项目内")
                    out.parent.mkdir(parents=True, exist_ok=True)
                    out.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
            elif args.command == "opinion":
                result = workflow.submit_role(args.run_id, args.role, args.phase, payload(args.input))
            elif args.command == "publish":
                result = workflow.publish(args.run_id, payload(args.input))
            elif args.command == "review":
                p = payload(args.input)
                p.setdefault("request_id", uuid.uuid4().hex)
                if not p.get("evidence_ids"):
                    raise DomainError("review 必须给出本次核对使用的新证据 evidence_ids")
                result = workflow.review(args.run_id, p)
            elif args.command == "runs":
                result = workflow.get_run(args.id) if args.id else workflow.list_runs()
            elif args.command == "discussion":
                result = workflow.discussion(args.run_id, role=args.role)
            elif args.command == "roles":
                result = workflow.roles()
        print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except (DomainError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
