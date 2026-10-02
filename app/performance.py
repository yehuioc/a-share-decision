"""Project economics kept outside the brokerage ledger."""
from __future__ import annotations
from datetime import date, timedelta
from calendar import monthrange

from .store import DomainError, ConflictError, now_iso, parse_time, stable_id
from .settings import settings
from .outcomes import amount, nonempty


def record_costs(store, payload):
    """Confirm total CNY operating expense for a closed, nonoverlapping period."""
    try:
        start, end = date.fromisoformat(payload.get("start_date", "")), date.fromisoformat(payload.get("end_date", ""))
    except (ValueError, TypeError) as exc:
        raise DomainError("成本期间须为YYYY-MM-DD") from exc
    if start > end or end >= parse_time(now_iso()).date():
        raise DomainError("成本期间不能倒置，须为已经结束的日期；当天仍可能产生新费用")
    if payload.get("confirmed_complete") is not True:
        raise DomainError("须确认该期间所有模型、数据及分摊订阅成本已经计入；真实为零才填0")
    record = {"id": stable_id("cost", payload.get("request_id")), "request_id": payload.get("request_id"),
              "start_date": start.isoformat(), "end_date": end.isoformat(), "currency": "CNY",
              "amount": amount(payload.get("amount"), "实际人民币运行成本"),
              "confirmed_complete": True, "source": nonempty(payload.get("source"), "账单/成本依据")}
    if payload.get("currency", "CNY") != "CNY":
        raise DomainError("请按有依据的汇率换算为CNY再录入，不自动猜测美元账单成本")
    with store.transaction():
        old = store.read(f"project_costs/{record['id']}.json")
        if old:
            if {k: v for k, v in old.items() if k != "recorded_at"} != record:
                raise ConflictError("同一成本请求不能覆盖不同金额")
            return old
        for r in store.list("project_costs"):
            if start.isoformat() <= r["end_date"] and end.isoformat() >= r["start_date"]:
                raise ConflictError("成本期间重叠，不能重复扣费")
        record["recorded_at"] = now_iso()
        return store.write(f"project_costs/{record['id']}.json", record)


def summary(store, ledger, runs=None):
    if ledger.get("as_of") and parse_time(ledger["as_of"]) > parse_time(now_iso()):
        raise DomainError("收益核对截止时间不能晚于当前时间")
    cfg = settings(store)
    goal = cfg["income_goals"][cfg["stage"] - 1]
    costs = sorted(store.list("project_costs"), key=lambda x: x["start_date"])
    baseline = ledger.get("baseline")
    end = parse_time(ledger.get("as_of") or now_iso()).date()
    start = parse_time(baseline["as_of"]).date() if baseline else None
    covered, missing, total = False, [], 0.0
    if start:
        applicable = [r for r in costs if r["start_date"] >= start.isoformat() and r["end_date"] <= end.isoformat()]
        cursor = start
        for r in applicable:
            left, right = date.fromisoformat(r["start_date"]), date.fromisoformat(r["end_date"])
            if left > cursor:
                missing.append({"from": cursor.isoformat(), "to": (left - timedelta(days=1)).isoformat()})
            cursor = right + timedelta(days=1)
            total += r["amount"]
        if cursor <= end:
            missing.append({"from": cursor.isoformat(), "to": end.isoformat()})
        covered = not missing
    after = ledger.get("net_profit") - total if covered and ledger.get("net_profit") is not None else None
    increment = ledger.get("incremental_profit") - total if covered and ledger.get("incremental_profit") is not None else None
    monthly = []
    for m in ledger.get("complete_months", []):
        year, month = map(int, m["month"].split("-"))
        left, right = date(year, month, 1), date(year, month, monthrange(year, month)[1])
        selected = [c for c in costs if left.isoformat() <= c["start_date"] <= c["end_date"] <= right.isoformat()]
        cursor, expense = left, 0.0
        for c in selected:
            if c["start_date"] != cursor.isoformat():
                break
            cursor = date.fromisoformat(c["end_date"]) + timedelta(days=1)
            expense += c["amount"]
        complete = cursor > right
        monthly.append({"month": m["month"], "operating_costs_complete": complete,
                        "net_profit_after_operating_costs": round(m["net_profit"] - expense, 6)
                        if complete and m["net_profit"] is not None else None})
    monthly_values = [m["net_profit_after_operating_costs"] for m in monthly if m["net_profit_after_operating_costs"] is not None]
    calls = store.list("model_calls")
    tokens = sum((c.get("usage") or {}).get("total_tokens", 0) for c in calls)
    groups = {}
    for run in runs or []:
        if run.get("mode") != "live":
            continue
        for plan in (run.get("decision") or {}).get("plans", []):
            strategy = plan.get("execution", {}).get("strategy_key")
            if not strategy:
                continue
            g = groups.setdefault(strategy, {"strategy_key": strategy, "plan_count": 0, "settled_plans": 0,
                                              "ambiguous_plans": 0, "actual_linked_trades": 0, "plan_refs": []})
            g["plan_count"] += 1
            pid = plan["plan_id"]
            g["plan_refs"].append({"run_id": run["id"], "plan_id": pid, "symbol": plan["symbol"],
                                   "start": plan["valid_from"], "end": plan["valid_until"]})
            results = [o for r in run.get("reviews", []) for o in r.get("plan_outcomes", []) if o["plan_id"] == pid]
            if results:
                latest = results[-1]
                g["settled_plans"] += latest["status"] == "expired"
                g["ambiguous_plans"] += latest["status"] in {"unresolved", "data_conflict", "corporate_action_unresolved"}
                g["actual_linked_trades"] += latest.get("execution_records", {}).get("actual", {}).get("trade_count", 0)
    for g in groups.values():
        # Overlapping windows form one evidence cluster; no manufactured win rate.
        clusters = []
        for p in sorted(g["plan_refs"], key=lambda x: x["start"]):
            if clusters and p["start"] <= clusters[-1]["end"]:
                clusters[-1]["end"] = max(clusters[-1]["end"], p["end"])
                clusters[-1]["plans"].append(p["plan_id"])
            else:
                clusters.append({"start": p["start"], "end": p["end"], "plans": [p["plan_id"]]})
        g["time_clusters"] = clusters
        g["interpretation"] = "时间簇仅排除明显重叠，不证明统计独立；不把模拟触价收益当实盘胜率"
    return {"stage": cfg["stage"], "monthly_target": goal, "as_of": ledger.get("as_of"),
            "account_net_profit": ledger.get("net_profit"), "incremental_account_profit": ledger.get("incremental_profit"),
            "operating_costs_complete": covered, "known_period_operating_costs": round(total, 6), "missing_cost_periods": missing,
            "profit_after_operating_costs": None if after is None else round(after, 6),
            "incremental_profit_after_operating_costs": None if increment is None else round(increment, 6),
            "monthly_average": ledger.get("monthly_average"), "complete_months": ledger.get("complete_months", []),
            "months_after_operating_costs": monthly,
            "monthly_average_after_operating_costs": round(sum(monthly_values) / len(monthly_values), 6) if monthly_values else None,
            "goal_status": "尚未建立实际收益起点" if not baseline else "开始累计前瞻证据；短期达标不等于稳定盈利",
            "stability_proven": False, "cost_records": costs, "model_usage": {"calls": len(calls), "total_tokens": tokens,
                "actual_cny_cost": "以实际账单确认为准；token用量不等于人民币费用"},
            "strategies": list(groups.values()), "boundary": "目标不驱动强制交易或自动进阶；模拟、重放与账户净收益分列"}
