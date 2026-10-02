"""Executable research plans and manual execution attribution. Never places orders."""
from __future__ import annotations

import copy
import math
from datetime import date, time, timedelta
from decimal import Decimal, InvalidOperation

from .calendar import is_trading_day, next_trading_day
from .store import DomainError, ConflictError, now_iso, parse_time, safe_id, stable_id


def plan_id(run_id, symbol):
    return stable_id("plan", f"{run_id}:{symbol}")


def amount(value, label, *, minimum=0, nullable=False):
    if value is None and nullable:
        return None
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainError(f"{label} 必须是有限数字") from exc
    if isinstance(value, bool) or not result.is_finite() or result < minimum:
        raise DomainError(f"{label} 必须是大于等于 {minimum} 的有限数字")
    converted = float(result)
    if not math.isfinite(converted):
        raise DomainError(f"{label} 超出可保存数字范围")
    return converted


def nonempty(value, label):
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"{label} 必须是非空文字")
    return value.strip()


def first_session(at):
    at = parse_time(at)
    if is_trading_day(at.date()) and at.time() < time(9, 30):
        return at.date()
    # Daily bars cannot reconstruct the path following an intraday publication.
    return next_trading_day(at.date())


def normalize_execution(value, plan):
    if not isinstance(value, dict):
        raise DomainError("每条新计划必须给出 execution，不能只用自然语言价格方向复盘")
    legs = value.get("legs")
    if not isinstance(legs, list) or not 1 <= len(legs) <= 12:
        raise DomainError("execution.legs 须有1至12个互不重复占用数量的研究分仓")
    out = {"strategy_key": nonempty(value.get("strategy_key"), "execution.strategy_key"),
           "assumptions": value.get("assumptions"), "legs": []}
    if not isinstance(out["assumptions"], list) or any(not isinstance(x, str) for x in out["assumptions"]):
        raise DomainError("execution.assumptions 必须为文字列表")
    for i, raw in enumerate(legs):
        if not isinstance(raw, dict):
            raise DomainError("研究分仓必须为对象")
        action = raw.get("action")
        allowed = {"reduce": {"hold", "sell"}}.get(plan["action"], {plan["action"]})
        if action not in allowed:
            raise DomainError("execution 分仓动作必须与当前计划一致；reduce 分为 hold 和 sell")
        q = raw.get("quantity")
        if type(q) is not int or q <= 0 or q % 100:
            raise DomainError("研究分仓 quantity 必须为100的正整数倍")
        trigger = raw.get("entry")
        if not isinstance(trigger, dict) or trigger.get("kind") not in {
            "next_open", "limit", "stop", "close_above", "close_below", "manual", "none"
        }:
            raise DomainError("entry.kind 不受支持")
        if action in {"hold", "avoid"} and trigger["kind"] != "none":
            raise DomainError("hold/avoid 的 entry.kind 应为 none")
        if action in {"buy", "sell"} and trigger["kind"] == "none":
            raise DomainError("buy/sell 必须说明进入或卖出条件")
        price_required = trigger["kind"] in {"limit", "stop", "close_above", "close_below"}
        price = amount(trigger.get("price"), "entry.price", minimum=0.000001, nullable=not price_required)
        if not price_required and price is not None:
            raise DomainError("无价格条件的 entry.price 必须为 null")
        target = amount(raw.get("take_profit"), "take_profit", minimum=0.000001, nullable=True)
        stop = amount(raw.get("stop_loss"), "stop_loss", minimum=0.000001, nullable=True)
        if action in {"sell", "avoid"} and (target is not None or stop is not None):
            raise DomainError("sell/avoid 不得暗含卖空或二次卖出保护单")
        reference = price or plan["baseline_price"]
        if reference is None:
            raise DomainError("计划缺少绝对基准价格")
        if (target is not None and target <= reference) or (stop is not None and stop >= reference):
            raise DomainError("保护价须满足 stop_loss < 参考买入价 < take_profit")
        basis = raw.get("exit_basis", "intraday")
        if basis not in {"intraday", "close"}:
            raise DomainError("exit_basis 必须为 intraday 或 close")
        conditions = raw.get("conditions", [])
        if not isinstance(conditions, list) or any(not isinstance(x, str) or not x.strip() for x in conditions):
            raise DomainError("conditions 必须列出需人工确认的非价格条件")
        out["legs"].append({"id": f"leg-{i + 1}", "action": action, "quantity": q,
            "entry": {"kind": trigger["kind"], "price": price}, "take_profit": target,
            "stop_loss": stop, "exit_basis": basis, "conditions": conditions,
            "buy_fee": amount(raw.get("buy_fee"), "buy_fee", nullable=True),
            "sell_fee": amount(raw.get("sell_fee"), "sell_fee", nullable=True),
            "slippage_bps": amount(raw.get("slippage_bps", 0), "slippage_bps"),
            "reasoning": nonempty(raw.get("reasoning"), "分仓 reasoning")})
    if plan["action"] == "reduce" and {x["action"] for x in out["legs"]} != {"sell", "hold"}:
        raise DomainError("reduce 必须同时说明减持数量与保留数量")
    if any(x["slippage_bps"] >= 10000 for x in out["legs"]):
        raise DomainError("slippage_bps 必须小于10000，不能使成交价为零或负数")
    return out


def normalize_edge(value, evidence_ids):
    if not isinstance(value, dict):
        raise DomainError("新计划须说明 edge：获利机制、竞争解释与概率依据")
    out = {k: nonempty(value.get(k), "edge." + k) for k in
           ("mechanism", "alternative_explanation", "priced_in", "falsifier")}
    basis = value.get("probability_basis")
    if basis not in {"unknown", "subjective", "empirical"}:
        raise DomainError("probability_basis 必须为 unknown/subjective/empirical")
    p = amount(value.get("win_probability"), "win_probability", nullable=True)
    if (p is not None and p > 1) or (basis == "unknown" and p is not None) or (basis != "unknown" and p is None):
        raise DomainError("概率须在0至1之间；unknown必须为null，其他依据须提供概率")
    refs = value.get("sample_refs", [])
    if not isinstance(refs, list) or any(not isinstance(x, str) for x in refs):
        raise DomainError("sample_refs 必须为证据ID列表")
    if set(refs) - set(evidence_ids):
        raise DomainError("概率样本只能引用已冻结证据")
    count = value.get("independent_samples")
    if count is not None and (type(count) is not int or count < 0):
        raise DomainError("independent_samples 必须为非负整数或null")
    if basis == "empirical" and (not refs or not count):
        raise DomainError("经验概率必须给出样本证据及去重后的独立样本数量")
    out.update(probability_basis=basis, win_probability=p, sample_refs=refs,
               independent_samples=count, calibration="待样本外结果验证；样本声明不是程序核定的胜率")
    return out


def scenario_metrics(plan):
    rows = []
    probability = plan.get("edge", {}).get("win_probability")
    for row in plan.get("sizing", {}).get("scenarios", []):
        q, buy, sell, stop, fee = (Decimal(str(row[k])) for k in
                                 ("quantity", "entry_price", "exit_price", "stop_price", "estimated_fees"))
        gain, loss = q * (sell - buy), q * (buy - stop)
        total = gain + loss
        rows.append({"label": row["label"], "gross_gain": float(gain), "gross_loss": float(loss),
                     "estimated_net_gain": float(gain - fee), "estimated_net_loss": float(loss + fee),
                     "breakeven_probability_zero_cost": float(loss / total),
                     "breakeven_probability_with_cost": float((loss + fee) / total),
                     "estimated_ev": None if probability is None else float(Decimal(str(probability)) * total - loss - fee),
                     "probability_basis": plan.get("edge", {}).get("probability_basis", "unknown"),
                     "interpretation": "仅为二分支敏感性，未触发、跳空、到期和分红另行影响结果；不是经验证的收益预测"})
    return rows


def _sessions(start, end):
    current = start
    while current <= end:
        yield current.isoformat()
        if current == end:
            break
        current = next_trading_day(current)


def _trigger(leg, row):
    kind, p = leg["entry"]["kind"], leg["entry"]["price"]
    side = leg["action"]
    if kind == "next_open":
        return row["open"], "open"
    if kind == "limit":
        if side == "buy" and row["low"] <= p:
            return min(p, row["open"]), "open" if row["open"] <= p else "intraday"
        if side == "sell" and row["high"] >= p:
            return max(p, row["open"]), "open" if row["open"] >= p else "intraday"
    if kind == "stop":
        if side == "buy" and row["high"] >= p:
            return max(p, row["open"]), "open" if row["open"] >= p else "intraday"
        if side == "sell" and row["low"] <= p:
            return min(p, row["open"]), "open" if row["open"] <= p else "intraday"
    if (kind == "close_above" and row["close"] >= p) or (kind == "close_below" and row["close"] <= p):
        return None, "next_open"
    return None, None


def _simulate_leg(leg, rows, reference, expired):
    q, side = leg["quantity"], leg["action"]
    result = {"leg_id": leg["id"], "action": side, "quantity": q, "status": "not_triggered",
              "fills": [], "gross_pnl": None, "estimated_net_pnl": None,
              "benchmark_gross_pnl": None, "incremental_estimated_pnl": None, "issues": []}
    if leg["conditions"] or leg["entry"]["kind"] == "manual":
        result.update(status="needs_manual_confirmation", issues=["非价格条件或人工入场不可由日线推定；请记录触发、未执行或成交"])
        return result
    held = side in {"hold", "sell"}
    cash = 0.0
    start_value = q * reference if held else 0.0
    entry_date = None
    queued = None
    fee_sum = 0.0
    unknown_fee = False
    slip = leg["slippage_bps"] / 10000

    def fill(action, day, price, reason):
        nonlocal held, cash, fee_sum, unknown_fee, entry_date
        px = price * (1 + slip if action == "buy" else 1 - slip)
        fee = leg["buy_fee"] if action == "buy" else leg["sell_fee"]
        cash += (-1 if action == "buy" else 1) * q * px
        fee_sum += fee or 0
        unknown_fee |= fee is None
        held = action == "buy"
        if held:
            entry_date = day
        result["fills"].append({"side": action, "date": day, "price": round(px, 6),
                                "quantity": q, "assumed_fee": fee, "reason": reason})

    for row in rows:
        day = row["date"]
        if row.get("volume", 0) == 0:
            continue
        # An OHLC touch cannot establish queue priority on a locked-price session.
        locked = row["high"] == row["low"]
        if queued:
            if locked:
                result.update(status="fill_unknown", issues=["单一成交价交易日无法确定排队成交"])
                return result
            fill(queued, day, row["open"], "前收盘条件成立后，下一交易日开盘假设成交")
            queued = None
        elif not result["fills"] and side in {"buy", "sell"}:
            px, when = _trigger(leg, row)
            if when == "next_open":
                queued = side
            elif px is not None:
                if locked:
                    result.update(status="fill_unknown", issues=["触价但单一成交价，不能确认可成交"])
                    return result
                fill(side, day, px, "按事前价格条件模拟；不代表实际成交")
        if not held or side in {"sell", "avoid"}:
            continue
        target, stop = leg["take_profit"], leg["stop_loss"]
        if entry_date == day:
            # The supported A-share / domestic equity ETF scope is T+1.
            if ((stop is not None and row["low"] <= stop) or
                    (target is not None and row["high"] >= target)):
                result["issues"].append(f"{day} 买入当日触及退出价，T+1不能卖出；后续重新判断")
            continue
        if leg["exit_basis"] == "close":
            if ((stop is not None and row["close"] <= stop) or
                    (target is not None and row["close"] >= target)):
                queued = "sell"
            continue
        hit_stop = stop is not None and row["low"] <= stop
        hit_target = target is not None and row["high"] >= target
        if hit_stop or hit_target:
            if locked:
                result.update(status="fill_unknown", issues=result["issues"] + ["退出条件触发但单一成交价，不能确认可卖出"])
                return result
            if hit_stop and row["open"] <= stop:
                px, why = row["open"], "跳空跌破止损，按更差的开盘价"
            elif hit_target and row["open"] >= target:
                px, why = row["open"], "开盘已超过目标价"
            elif hit_stop and hit_target:
                result.update(status="path_ambiguous", issues=result["issues"] + ["同日日线覆盖止盈和止损，先后未知；不计为确定盈利样本"])
                return result
            else:
                px, why = (stop, "止损触发") if hit_stop else (target, "止盈触发")
            fill("sell", day, px, why)
    mark = rows[-1]["close"]
    gross = cash + (q * mark if held else 0) - start_value
    # The comparison is specified before observing the result.
    benchmark = q * (mark - reference) if side in {"hold", "sell"} else 0.0
    if side == "avoid":
        benchmark = q * (mark - rows[0]["open"])
    status = "held" if held else "closed" if result["fills"] else "not_triggered"
    if side == "avoid":
        status = "stayed_in_cash"
    if queued:
        status = "expired_pending_unfilled" if expired else "triggered_pending_next_open"
    elif expired and held:
        status = "expired_marked"
    elif expired and status == "not_triggered":
        status = "expired_untriggered"
    result.update(status=status, mark_price=mark, gross_pnl=round(gross, 6),
                  estimated_net_pnl=None if unknown_fee else round(gross - fee_sum, 6),
                  benchmark_gross_pnl=round(benchmark, 6),
                  incremental_estimated_pnl=None if unknown_fee else round(gross - fee_sum - benchmark, 6))
    return result


def evaluate_plan(plan, decision, evidence, as_of):
    """Evaluate the predeclared path; daily prices never establish actual fills."""
    at = parse_time(as_of)
    execution = plan.get("execution")
    result = {"plan_id": plan.get("plan_id") or plan_id(decision["run_id"], plan["symbol"]),
              "symbol": plan["symbol"], "as_of": at.isoformat(), "status": "pending",
              "legs": [], "gross_pnl": None, "estimated_net_pnl": None,
              "incremental_estimated_pnl": None, "source_ids": [],
              "basis": "日线条件回放；模拟成交、价格损益与估计费用，不含分红/公司行动及项目运行成本，不是实盘收益"}
    result["research_cost_mode"] = plan.get("research_cost_mode")
    if result["research_cost_mode"] == "zero_friction":
        result["basis"] = "日线条件回放；按用户指定的零摩擦口径计算模拟价格损益，不含分红/公司行动，不是实盘收益"
    if not execution:
        result.update(status="legacy_unstructured", reason="旧计划未事前固定可执行结构；保留原文与方向复核，不事后补规则制造历史成绩")
        return result
    start = date.fromisoformat(plan["valid_from"])
    end = min(at.date(), date.fromisoformat(plan["valid_until"]))
    if end == at.date() and at.time() < time(15):
        end -= timedelta(days=1)
        while not is_trading_day(end):
            end -= timedelta(days=1)
    if end < start:
        result["reason"] = "尚无计划生效后的完整交易日"
        return result
    needed = list(_sessions(start, end))
    rows, sources = {}, set()
    for e in evidence:
        if e.get("kind") != "market" or e.get("status") != "ready" or parse_time(e["available_at"]) > at:
            continue
        series = e.get("metadata", {}).get("daily", {}).get(plan["symbol"], {})
        if series.get("adjustment") != "none":
            continue
        for r in series.get("rows", []):
            if r.get("date") not in needed:
                continue
            if parse_time(e["available_at"]) < parse_time(r["date"] + "T15:00:00+08:00"):
                continue
            keys = ("open", "high", "low", "close", "volume")
            if any(r.get(k) is None for k in keys):
                continue
            normalized = {k: amount(r[k], k, minimum=0 if k == "volume" else .000001) for k in keys}
            normalized["date"] = r["date"]
            if not normalized["low"] <= min(normalized["open"], normalized["close"]) <= max(normalized["open"], normalized["close"]) <= normalized["high"]:
                raise DomainError("条件复盘日线高低价矛盾")
            if r["date"] in rows and rows[r["date"]] != normalized:
                result.update(status="data_conflict", reason=f"{r['date']} 的 OHLC/成交量冲突")
                return result
            rows[r["date"]] = normalized
            sources.add(e["id"])
    missing = sorted(set(needed) - set(rows))
    result["source_ids"] = sorted(sources)
    if missing:
        result.update(reason="缺少生效至复核期间的完整日线，不能跳过缺日推定触发顺序", missing_dates=missing)
        return result
    ordered = [rows[d] for d in needed]
    previous = plan["baseline_price"]
    for r in ordered:
        if previous and not .65 <= r["open"] / previous <= 1.5:
            result.update(status="corporate_action_unresolved", reason="存在价格断点，需核实除权/份额折算后才能计算可比收益")
            return result
        previous = r["close"]
    expired = end >= date.fromisoformat(plan["valid_until"])
    results = [_simulate_leg(leg, ordered, plan["baseline_price"], expired) for leg in execution["legs"]]
    incomplete = [x for x in results if x["gross_pnl"] is None]
    result.update(legs=results, evaluated_through=needed[-1],
                  status="unresolved" if incomplete else "expired" if expired else "evaluated")
    if not incomplete:
        for key in ("gross_pnl", "estimated_net_pnl", "incremental_estimated_pnl"):
            values = [x[key] for x in results]
            result[key] = None if any(x is None for x in values) else round(sum(values), 6)
    return result


def resolve_plan(store, run_id, pid):
    run_id, pid = safe_id(run_id), safe_id(pid)
    run = store.read(f"runs/{run_id}.json")
    decision = store.read(f"run_events/{run_id}/decision.json")
    if not run or not decision:
        raise DomainError("关联研究尚未发布计划")
    for p in decision["plans"]:
        if (p.get("plan_id") or plan_id(run_id, p["symbol"])) == pid:
            return run, decision, p
    raise DomainError("plan_id 不属于该研究轮次")


def validate_link(store, event):
    rid, pid = event.get("run_id"), event.get("plan_id")
    if not rid and not pid:
        return
    if not rid or not pid:
        raise DomainError("run_id 与 plan_id 必须一起填写")
    run, decision, plan = resolve_plan(store, rid, pid)
    if event["type"] not in {"buy", "sell", "dividend"}:
        raise DomainError("仅交易和分红关联计划；资金进出不是计划收益，补录费用通过原交易关联")
    if plan["symbol"] != event["symbol"]:
        raise DomainError("成交标的与计划标的不一致")
    if run.get("mode") == "replay" and event["status"] == "actual":
        raise DomainError("历史回放计划不能关联实盘收益")
    effective = run["as_of"] if run.get("mode") == "replay" else decision["published_at"]
    if parse_time(event["as_of"]) < parse_time(effective):
        raise DomainError("成交早于计划发布，不能事后归为该计划贡献")


class PlanBook:
    def __init__(self, store):
        self.store = store

    def record(self, payload):
        rid, pid = payload.get("run_id"), payload.get("plan_id")
        run, decision, plan = resolve_plan(self.store, rid, pid)
        kind = payload.get("kind")
        if kind not in {"triggered", "not_executed", "invalidated", "expired"}:
            raise DomainError("人工计划记录 kind 必须为 triggered/not_executed/invalidated/expired")
        when = parse_time(payload.get("as_of"))
        effective = run["as_of"] if run.get("mode") == "replay" else decision["published_at"]
        if when < parse_time(effective) or when > parse_time(now_iso()):
            raise DomainError("计划记录不能早于发布或晚于当前时间")
        if kind == "expired" and plan.get("valid_until") and when < parse_time(plan["valid_until"] + "T15:00:00+08:00"):
            raise DomainError("尚未到事前有效期；提前结束请记录 invalidated 并说明原因")
        record = {"id": stable_id("plan-event", payload.get("request_id")), "request_id": payload.get("request_id"),
                  "run_id": rid, "plan_id": pid, "symbol": plan["symbol"], "kind": kind,
                  "as_of": when.isoformat(timespec="seconds"), "note": nonempty(payload.get("note"), "事件说明"),
                  "source": nonempty(payload.get("source"), "人工核对依据")}
        rel = f"plan_events/{record['id']}.json"
        with self.store.transaction():
            old = self.store.read(rel)
            if old:
                if {k: v for k, v in old.items() if k != "recorded_at"} != record:
                    raise ConflictError("同一计划事件请求对应不同内容")
                return old
            record["recorded_at"] = now_iso()
            return self.store.write(rel, record)

    def attribution(self, run_id, pid, mark=None, as_of=None):
        run, decision, plan = resolve_plan(self.store, run_id, pid)
        cutoff = parse_time(as_of or now_iso())
        events = [e for e in self.store.list("ledger/events") if parse_time(e["as_of"]) <= cutoff and
                  parse_time(e.get("recorded_at", e["as_of"])) <= cutoff]
        linked = [e for e in events if e.get("run_id") == run_id and e.get("plan_id") == pid]
        corrections = {e["related_event_id"]: e["amount"] for e in events if e["type"] == "fee" and e.get("related_event_id")}
        result = {"run_id": run_id, "plan_id": pid, "symbol": plan["symbol"], "mark_price": mark,
                  "as_of": cutoff.isoformat(), "actual": {}, "paper": {},
                  "not_executed": [e for e in linked if e["status"] == "not_executed"],
                  "events": [e for e in self.store.list("plan_events") if e["plan_id"] == pid and
                             parse_time(e["as_of"]) <= cutoff and parse_time(e["recorded_at"]) <= cutoff]}
        for mode in ("actual", "paper"):
            group = sorted([e for e in linked if e["status"] == mode], key=lambda x: (x["as_of"], x["id"]))
            trades = [e for e in group if e["type"] in {"buy", "sell"}]
            fees = [e["fees"] if e["fees"] is not None else corrections.get(e["id"]) for e in trades]
            delta = sum(e["quantity"] * (1 if e["type"] == "buy" else -1) for e in trades)
            cashflow = sum(e["quantity"] * e["price"] * (-1 if e["type"] == "buy" else 1) for e in trades)
            fees_complete = all(x is not None for x in fees)
            effect = cashflow + delta * (mark or 0) - sum(fees) if trades and fees_complete and (delta == 0 or mark is not None) else None
            # This measures only the linked trading decisions, not historical position gains.
            result[mode] = {"event_ids": [e["id"] for e in group], "trade_count": len(trades),
                "quantity_change": delta, "trade_cashflow_before_fees": round(cashflow, 6),
                "fees_complete": fees_complete, "known_fees": round(sum(x or 0 for x in fees), 6),
                "incremental_trade_pnl": None if effect is None else round(effect, 6),
                "closed_roundtrip_net": round(cashflow - sum(fees), 6) if trades and delta == 0 and fees_complete else None,
                "outside_plan_window": [e["id"] for e in trades if plan.get("valid_until") and
                    (parse_time(e["as_of"]) > parse_time(plan["valid_until"] + "T15:00:00+08:00") or
                     parse_time(e["as_of"]) < parse_time(plan["valid_from"] + "T09:30:00+08:00"))],
                "scope": "相对不执行这些关联成交的价格损益差额，扣实际填写费用；不含未关联交易、分红差额或项目成本，不等于全账户利润"}
        return result
