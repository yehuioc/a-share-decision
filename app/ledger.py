"""Deterministic, persistent manual account ledger for the A-share workbench."""
from __future__ import annotations

import calendar as month_calendar
import math
import re
from datetime import date, datetime, time, timedelta
from decimal import Decimal, InvalidOperation
from typing import Any

from .calendar import is_trading_day
from .store import CN_TZ, ConflictError, DomainError, Store, now_iso, parse_time, stable_id


_EVENT_TYPES = {"buy", "sell", "dividend", "deposit", "withdrawal", "fee"}
_STATUSES = {"actual", "paper", "not_executed"}
_SYMBOL_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,31}")
_A_SHARE_PREFIXES = ("000", "001", "002", "003", "300", "301", "600", "601", "603", "605", "688")
_SCOPED_STOCK_ETFS = {"510300", "588290"}
_ZERO = Decimal("0")


def _decimal(value: Any, field: str, *, positive: bool = False) -> Decimal:
    if isinstance(value, bool) or value is None:
        raise DomainError(f"{field} 必须是有限数字")
    try:
        result = Decimal(str(value))
    except (InvalidOperation, ValueError, TypeError) as exc:
        raise DomainError(f"{field} 必须是有限数字") from exc
    if not result.is_finite():
        raise DomainError(f"{field} 必须是有限数字")
    if positive and result <= 0:
        raise DomainError(f"{field} 必须大于零")
    if not positive and result < 0:
        raise DomainError(f"{field} 不能为负数")
    return result


def _integer(value: Any, field: str, *, allow_zero: bool = False) -> int:
    if isinstance(value, bool):
        raise DomainError(f"{field} 必须是整数")
    try:
        result = int(value)
    except (ValueError, TypeError, OverflowError) as exc:
        raise DomainError(f"{field} 必须是整数") from exc
    try:
        exact = Decimal(str(value)) == Decimal(result)
    except (InvalidOperation, ValueError, TypeError):
        exact = False
    if not exact or result < (0 if allow_zero else 1):
        qualifier = "非负整数" if allow_zero else "正整数"
        raise DomainError(f"{field} 必须是{qualifier}")
    return result


def _number(value: Decimal | int | None) -> int | float | None:
    if value is None:
        return None
    if isinstance(value, int):
        return value
    if value == value.to_integral_value():
        return int(value)
    result = float(value)
    if not math.isfinite(result):
        raise DomainError("数字超出可保存或返回的范围")
    return result


def _symbol(value: Any) -> str:
    if not isinstance(value, str):
        raise DomainError("symbol 必须是证券代码")
    result = value.strip().upper()
    if not _SYMBOL_RE.fullmatch(result) or ".." in result:
        raise DomainError("symbol 格式不正确")
    return result


def _timestamp(value: Any, field: str = "as_of") -> str:
    if not isinstance(value, (str, datetime)):
        raise DomainError(f"{field} 必须为含时区的 ISO 8601")
    return parse_time(value).isoformat(timespec="seconds")


def _not_future(value: str, field: str) -> None:
    if parse_time(value) > parse_time(now_iso()):
        raise DomainError(f"{field}不能晚于当前时间")


def _event_key(event: dict) -> tuple[datetime, str]:
    return parse_time(event["as_of"]), event["id"]


def _requires_t_plus_one(symbol: str) -> bool:
    """Apply only rules established by this release's instrument scope."""
    return symbol in _SCOPED_STOCK_ETFS or (len(symbol) == 6 and symbol.startswith(_A_SHARE_PREFIXES))


def _valuation_key(valuation: dict) -> tuple[datetime, str]:
    return parse_time(valuation["as_of"]), valuation["id"]


class Ledger:
    """Manual ledger whose derived values are rebuilt from immutable records."""

    def __init__(self, store: Store, mode="actual"):
        if mode not in {"actual", "paper"}:
            raise DomainError("账本模式必须是 actual 或 paper")
        self.store = store
        self.mode = mode
        self.baseline_path = "ledger/baseline.json" if mode == "actual" else "ledger/paper-baseline.json"
        self.valuation_dir = "ledger/valuations" if mode == "actual" else "ledger/paper-valuations"

    def set_baseline(self, payload: dict) -> dict:
        normalized = self._normalize_baseline(payload)
        with self.store.transaction():
            existing = self.store.read(self.baseline_path)
            if existing is not None:
                if existing == normalized:
                    return existing
                if existing.get("request_id") == normalized["request_id"]:
                    raise ConflictError("相同 request_id 的初始账户内容不同")
                raise ConflictError("初始账户已经确认，不能再次建立或覆盖")
            if self.mode == "paper":
                events = [e for e in self._events() if e["status"] == "paper"]
                if any(parse_time(e["as_of"]) < parse_time(normalized["as_of"]) for e in events):
                    raise DomainError("模拟起点不能晚于已有模拟记录；请使用这些记录之前的起点")
                self._replay(normalized, events, knowledge_events=events, validate=True)
            self.store.write(self.baseline_path, normalized)
        return normalized

    def record_event(self, payload: dict) -> dict:
        if payload.get("status") == "paper" and self.mode != "paper":
            return Ledger(self.store, "paper").record_event(payload)
        event = self._normalize_event(payload)
        path = f"ledger/events/{event['id']}.json"
        with self.store.transaction():
            existing = self.store.read(path)
            if existing is not None:
                if {k: v for k, v in existing.items() if k != "recorded_at"} == event:
                    return existing
                raise ConflictError("相同 request_id 的账本事件内容不同")

            baseline = self.store.read(self.baseline_path)
            events = self._events()
            self._validate_event_against_ledger(event, baseline, events)
            from .outcomes import validate_link
            validate_link(self.store, event)
            candidate = sorted([*events, event], key=_event_key)
            if baseline is not None:
                self._replay(baseline, candidate, knowledge_events=candidate, validate=True)
            event["recorded_at"] = now_iso()
            self.store.write(path, event)
        return event

    def record_valuation(self, payload: dict) -> dict:
        valuation = self._normalize_valuation(payload)
        path = f"{self.valuation_dir}/{valuation['id']}.json"
        with self.store.transaction():
            existing = self.store.read(path)
            if existing is not None:
                if existing == valuation:
                    return existing
                raise ConflictError("相同 request_id 的估值内容不同")
            baseline = self.store.read(self.baseline_path)
            if baseline is None:
                raise DomainError("账户基线尚未确认，不能记录实际估值")
            if parse_time(valuation["as_of"]) < parse_time(baseline["as_of"]):
                raise DomainError("估值时间不能早于账户基线")
            for previous in self._valuations():
                if (parse_time(previous["as_of"]) == parse_time(valuation["as_of"])
                        and previous["prices"] != valuation["prices"]):
                    raise ConflictError("同一时点已有不同价格的估值，不能用新 request_id 覆盖")
            events = self._events()
            cutoff = parse_time(valuation["as_of"])
            known = [e for e in events if parse_time(e["as_of"]) <= cutoff]
            state = self._replay(baseline, known, knowledge_events=known, validate=True)
            required = set(state["positions"]) | {p["symbol"] for p in baseline["positions"]}
            missing = sorted(required - set(valuation["prices"]))
            if missing:
                raise DomainError("估值缺少持仓或被动对照价格：" + "、".join(missing))
            self.store.write(path, valuation)
        return valuation

    def summary(self, prices: dict | None = None, as_of: str | datetime | None = None) -> dict:
        baseline = self.store.read(self.baseline_path)
        all_events = self._events()
        all_valuations = self._valuations()
        cutoff = self._summary_cutoff(baseline, all_events, all_valuations, as_of)
        risk_budget, risk_budget_source, risk_budget_updated_at, risk_warning = (
            self._effective_risk_budget(baseline, cutoff if as_of is not None else None)
        )
        events = [e for e in all_events if cutoff is None or parse_time(e["as_of"]) <= cutoff]
        valuations = [v for v in all_valuations if cutoff is None or parse_time(v["as_of"]) <= cutoff]

        if baseline is None or (cutoff is not None and parse_time(baseline["as_of"]) > cutoff):
            return self._unconfirmed(
                all_events if as_of is None else events, valuations, risk_budget, risk_budget_source, risk_budget_updated_at,
                risk_warning,
            )

        state = self._replay(baseline, events, knowledge_events=events, validate=True)
        marks, mark_source, mark_warnings = self._marks(prices, baseline, state, events, valuations)
        actual_symbols = set(state["positions"])
        baseline_symbols = {p["symbol"] for p in baseline["positions"]}
        missing = sorted((actual_symbols | baseline_symbols) - set(marks))
        if missing:
            raise DomainError("缺少持仓或被动对照价格：" + "、".join(missing))

        market_value = sum((Decimal(p["quantity"]) * marks[s] for s, p in state["positions"].items()), _ZERO)
        net_value = state["cash"] + market_value
        net_profit = net_value - Decimal(str(baseline["initial_value"])) - state["net_inflows"]

        benchmark_value = None
        benchmark_profit = None
        if state["benchmark_complete"]:
            benchmark_cash = Decimal(str(baseline["cash"])) + state["net_inflows"] + state["benchmark_dividends"]
            benchmark_value = benchmark_cash + sum(
                Decimal(p["quantity"]) * marks[p["symbol"]] for p in baseline["positions"]
            )
            benchmark_profit = benchmark_value - Decimal(str(baseline["initial_value"])) - state["net_inflows"]

        fees_complete = not state["unknown_fee_event_ids"]
        positions = []
        unrealized = _ZERO
        for symbol in sorted(state["positions"]):
            item = state["positions"][symbol]
            quantity = item["quantity"]
            avg_cost = item["cost"] / quantity
            position_unrealized = marks[symbol] * quantity - item["cost"]
            unrealized += position_unrealized
            positions.append({
                "symbol": symbol,
                "quantity": quantity,
                "avg_cost": _number(avg_cost),
                "price": _number(marks[symbol]),
                "market_value": _number(marks[symbol] * quantity),
                "unrealized_pnl": _number(position_unrealized) if fees_complete else None,
            })

        monthly_average, complete_months, month_warnings = self._monthly(
            baseline, events, valuations, cutoff
        )
        max_drawdown, drawdown_warnings = self._drawdown(
            baseline, events, valuations, cutoff, fees_complete
        )

        warnings = [*mark_warnings, *month_warnings, *drawdown_warnings, *state["benchmark_warnings"]]
        if risk_warning:
            warnings.append(risk_warning)
        if not fees_complete:
            warnings.append(
                "存在费用未知的实际交易；现金和净值仅为按成交毛额推算，净盈利相关字段暂为 null。"
            )
        if not baseline["positions"]:
            warnings.append("初始账户没有持仓；被动对照仅保留初始现金及相同资金进出。")

        return {
            "confirmed": True,
            "as_of": cutoff.isoformat(timespec="seconds") if cutoff else baseline["as_of"],
            "baseline": baseline,
            "positions": positions,
            "cash": _number(state["cash"]),
            "cash_estimated": not fees_complete,
            "net_value": _number(net_value),
            "initial_value": baseline["initial_value"],
            "net_inflows": _number(state["net_inflows"]),
            "net_profit": _number(net_profit) if fees_complete else None,
            "realized_pnl": _number(state["realized_pnl"]) if fees_complete else None,
            "unrealized_pnl": _number(unrealized) if fees_complete else None,
            "dividends": _number(state["dividends"]),
            "fees": _number(state["fees"]) if fees_complete else None,
            "known_fees": _number(state["fees"]),
            "fees_complete": fees_complete,
            "benchmark_value": _number(benchmark_value),
            "incremental_profit": _number(net_profit - benchmark_profit)
            if fees_complete and benchmark_profit is not None else None,
            "monthly_average": monthly_average,
            "complete_months": complete_months,
            "max_drawdown_amount": max_drawdown,
            "drawdown_scope": (
                f"从基线初始净值开始，仅覆盖 {len(valuations)} 个已记录估值点"
                if valuations else "没有已记录估值点"
            ),
            "valuation_source": mark_source,
            "events": all_events if as_of is None else events,
            "valuations": valuations,
            "warnings": warnings,
            "risk_budget": risk_budget,
            "risk_budget_source": risk_budget_source,
            "risk_budget_updated_at": risk_budget_updated_at,
        }

    def _normalize_baseline(self, payload: dict) -> dict:
        self._payload(payload)
        allowed = {"request_id", "confirmed", "as_of", "cash", "positions", "risk_budget"}
        self._no_extra(payload, allowed)
        if payload.get("confirmed") is not True:
            raise DomainError("仅在用户明确确认后才能建立账户基线")
        request_id = self._request_id(payload)
        cash = _decimal(payload.get("cash"), "cash")
        raw_positions = payload.get("positions")
        if not isinstance(raw_positions, list):
            raise DomainError("positions 必须是列表")
        positions = []
        seen = set()
        for raw in raw_positions:
            if not isinstance(raw, dict):
                raise DomainError("positions 每项必须是对象")
            self._no_extra(raw, {"symbol", "quantity", "avg_cost", "price"})
            symbol = _symbol(raw.get("symbol"))
            if symbol in seen:
                raise DomainError(f"初始持仓重复：{symbol}")
            seen.add(symbol)
            quantity = _integer(raw.get("quantity"), f"{symbol} quantity")
            avg_cost = _decimal(raw.get("avg_cost"), f"{symbol} avg_cost", positive=True)
            price = _decimal(raw.get("price"), f"{symbol} price", positive=True)
            positions.append({
                "symbol": symbol,
                "quantity": quantity,
                "avg_cost": _number(avg_cost),
                "price": _number(price),
            })
        risk = payload.get("risk_budget")
        risk_budget = None if risk is None else _number(_decimal(risk, "risk_budget"))
        initial = cash + sum(Decimal(p["quantity"]) * Decimal(str(p["price"])) for p in positions)
        result = {
            "request_id": request_id,
            "confirmed": True,
            "as_of": _timestamp(payload.get("as_of")),
            "cash": _number(cash),
            "positions": sorted(positions, key=lambda p: p["symbol"]),
            "risk_budget": risk_budget,
            "initial_value": _number(initial),
        }
        _not_future(result["as_of"], "账户基线时间")
        return result

    def _normalize_event(self, payload: dict) -> dict:
        self._payload(payload)
        allowed = {
            "request_id", "as_of", "type", "status", "symbol", "quantity", "price",
            "amount", "fees", "dividend_per_share", "related_event_id", "note",
            "run_id", "plan_id",
        }
        self._no_extra(payload, allowed)
        request_id = self._request_id(payload)
        event_type = payload.get("type")
        status = payload.get("status")
        if event_type not in _EVENT_TYPES:
            raise DomainError("type 必须是 buy、sell、dividend、deposit、withdrawal 或 fee")
        if status not in _STATUSES:
            raise DomainError("status 必须是 actual、paper 或 not_executed")
        event = {
            "id": stable_id("evt", request_id),
            "request_id": request_id,
            "as_of": _timestamp(payload.get("as_of")),
            "type": event_type,
            "status": status,
            "symbol": None,
            "quantity": None,
            "price": None,
            "amount": None,
            "fees": None,
            "dividend_per_share": None,
            "related_event_id": None,
            "note": None,
        }
        if status == "actual":
            _not_future(event["as_of"], "实际事件时间")
        for field in ("run_id", "plan_id"):
            if payload.get(field) is not None:
                from .store import safe_id
                event[field] = safe_id(payload[field])
        note = payload.get("note")
        if note is not None:
            if not isinstance(note, str) or len(note) > 2000:
                raise DomainError("note 必须是长度不超过 2000 的文字")
            event["note"] = note

        if event_type in {"buy", "sell"}:
            if payload.get("dividend_per_share") is not None:
                raise DomainError(f"{event_type} 事件不能填写 dividend_per_share")
            event["symbol"] = _symbol(payload.get("symbol"))
            event["quantity"] = _integer(payload.get("quantity"), "quantity")
            event["price"] = _number(_decimal(payload.get("price"), "price", positive=True))
            gross = Decimal(event["quantity"]) * Decimal(str(event["price"]))
            if payload.get("amount") is not None and _decimal(payload["amount"], "amount") != gross:
                raise DomainError("交易 amount 必须等于 quantity × price")
            event["amount"] = _number(gross)
            if "fees" in payload and payload.get("fees") is not None:
                event["fees"] = _number(_decimal(payload["fees"], "fees"))
            if event_type == "buy" and event["quantity"] % 100:
                raise DomainError("买入数量必须是 100 股的整数倍")
            if payload.get("related_event_id") is not None:
                raise DomainError("买卖事件不能填写 related_event_id")
        elif event_type in {"deposit", "withdrawal", "dividend", "fee"}:
            per_share = payload.get("dividend_per_share")
            if event_type == "dividend" and per_share is not None:
                event["dividend_per_share"] = _number(
                    _decimal(per_share, "dividend_per_share", positive=True)
                )
                event["amount"] = _number(_decimal(payload.get("amount"), "amount"))
            else:
                # Zero is a valid confirmed fee, but never the default for an
                # unknown trade. It must refer to the specific actual trade.
                zero_fee_confirmation = event_type == "fee" and payload.get("related_event_id") is not None
                event["amount"] = _number(_decimal(
                    payload.get("amount"), "amount", positive=not zero_fee_confirmation
                ))
                if event_type != "dividend" and per_share is not None:
                    raise DomainError(f"{event_type} 事件不能填写 dividend_per_share")
            if payload.get("quantity") is not None or payload.get("price") is not None:
                raise DomainError(f"{event_type} 事件不能填写 quantity 或 price")
            if payload.get("fees") is not None:
                raise DomainError(f"{event_type} 事件不能另填 fees")
            if event_type == "dividend":
                event["symbol"] = _symbol(payload.get("symbol"))
            elif payload.get("symbol") is not None:
                raise DomainError(f"{event_type} 事件不能填写 symbol")
            related = payload.get("related_event_id")
            if event_type == "fee" and related is not None:
                if not isinstance(related, str) or not related:
                    raise DomainError("related_event_id 格式不正确")
                event["related_event_id"] = related
            elif related is not None:
                raise DomainError(f"{event_type} 事件不能填写 related_event_id")
        return event

    def _normalize_valuation(self, payload: dict) -> dict:
        self._payload(payload)
        self._no_extra(payload, {"request_id", "as_of", "prices"})
        request_id = self._request_id(payload)
        raw_prices = payload.get("prices")
        if not isinstance(raw_prices, dict):
            raise DomainError("prices 必须是证券代码到价格的对象")
        prices = {}
        for raw_symbol, raw_price in raw_prices.items():
            symbol = _symbol(raw_symbol)
            if symbol in prices:
                raise DomainError(f"估值价格重复：{symbol}")
            prices[symbol] = _number(_decimal(raw_price, f"{symbol} price", positive=True))
        result = {
            "id": stable_id("val", request_id),
            "request_id": request_id,
            "as_of": _timestamp(payload.get("as_of")),
            "prices": dict(sorted(prices.items())),
        }
        _not_future(result["as_of"], "估值时间")
        return result

    def _validate_event_against_ledger(self, event: dict, baseline: dict | None, events: list[dict]) -> None:
        if event["status"] == "actual" or (event["status"] == "paper" and baseline is not None):
            if baseline is None:
                raise DomainError("账户基线尚未确认，不能记录实际事件")
            if parse_time(event["as_of"]) < parse_time(baseline["as_of"]):
                raise DomainError("实际事件时间不能早于账户基线")
        if event["type"] in {"buy", "sell"} and event["status"] == self.mode and baseline is not None:
            if not is_trading_day(parse_time(event["as_of"]).date()):
                raise DomainError("实际买卖日期不是已核验交易日")
        if event["type"] == "sell" and event["status"] != "actual" and event["quantity"] % 100:
            raise DomainError("非实盘卖出无法核对尾数持仓，数量必须是 100 股的整数倍")
        if event["type"] == "fee" and event["related_event_id"] is not None:
            if event["status"] != self.mode:
                raise DomainError("费用补录必须与目标账本模式一致")
            targets = [e for e in events if e["id"] == event["related_event_id"]]
            if not targets:
                raise DomainError("related_event_id 未找到对应交易")
            target = targets[0]
            if target["status"] != self.mode or target["type"] not in {"buy", "sell"}:
                raise DomainError("费用只能补录到实际买卖事件")
            if target["fees"] is not None:
                raise ConflictError("对应交易费用已经明确，不能重复补录")
            if parse_time(event["as_of"]) < parse_time(target["as_of"]):
                raise DomainError("费用补录时间不能早于对应交易")
            if any(e.get("related_event_id") == target["id"] for e in events):
                raise ConflictError("对应交易费用已经补录，不能重复扣除")

    def _replay(
        self,
        baseline: dict,
        events: list[dict],
        *,
        knowledge_events: list[dict] | None = None,
        validate: bool,
    ) -> dict:
        actual = sorted((e for e in events if e["status"] == self.mode), key=_event_key)
        knowledge = actual if knowledge_events is None else [e for e in knowledge_events if e["status"] == self.mode]
        corrections = {e["related_event_id"]: Decimal(str(e["amount"])) for e in knowledge
                       if e["type"] == "fee" and e["related_event_id"] is not None}
        positions = {
            p["symbol"]: {"quantity": p["quantity"], "cost": Decimal(p["quantity"]) * Decimal(str(p["avg_cost"]))}
            for p in baseline["positions"]
        }
        baseline_quantities = {p["symbol"]: p["quantity"] for p in baseline["positions"]}
        baseline_symbols = set(baseline_quantities)
        cash = Decimal(str(baseline["cash"]))
        realized = _ZERO
        dividends = _ZERO
        benchmark_dividends = _ZERO
        benchmark_complete = True
        benchmark_warnings = []
        fees = _ZERO
        net_inflows = _ZERO
        bought_today: dict[tuple[str, date], int] = {}
        unknown = []

        for event in actual:
            event_type = event["type"]
            amount = Decimal(str(event["amount"]))
            if event_type in {"buy", "sell"}:
                symbol = event["symbol"]
                quantity = event["quantity"]
                trade_date = parse_time(event["as_of"]).date()
                own_fee = None if event["fees"] is None else Decimal(str(event["fees"]))
                corrected_fee = corrections.get(event["id"])
                effective_fee = own_fee if own_fee is not None else corrected_fee
                if effective_fee is None:
                    unknown.append(event["id"])
                    effective_fee = _ZERO
                else:
                    fees += effective_fee

                if event_type == "buy":
                    cash -= amount + effective_fee
                    current = positions.setdefault(symbol, {"quantity": 0, "cost": _ZERO})
                    current["quantity"] += quantity
                    current["cost"] += amount + effective_fee
                    if _requires_t_plus_one(symbol):
                        bought_today[(symbol, trade_date)] = bought_today.get((symbol, trade_date), 0) + quantity
                else:
                    current = positions.get(symbol)
                    held = 0 if current is None else current["quantity"]
                    available = held - bought_today.get((symbol, trade_date), 0)
                    if quantity > available:
                        raise DomainError(f"{symbol} 可卖数量不足或包含当日买入；当前可卖 {available} 股")
                    if quantity % 100 and quantity != available:
                        raise DomainError("卖出尾数必须一次卖完当前可用尾数/可用持仓")
                    average_cost = current["cost"] / held
                    allocated_cost = average_cost * quantity
                    cash += amount - effective_fee
                    realized += amount - effective_fee - allocated_cost
                    current["quantity"] -= quantity
                    current["cost"] -= allocated_cost
                    if current["quantity"] == 0:
                        positions.pop(symbol)
                if validate and cash < 0:
                    raise DomainError("现金不足，实际账户不允许透支")
            elif event_type == "deposit":
                cash += amount
                net_inflows += amount
            elif event_type == "withdrawal":
                cash -= amount
                net_inflows -= amount
                if validate and cash < 0:
                    raise DomainError("现金不足，不能完成出金")
            elif event_type == "dividend":
                cash += amount
                dividends += amount
                if event["symbol"] in baseline_symbols:
                    per_share = event.get("dividend_per_share")
                    if per_share is not None:
                        benchmark_dividends += (
                            Decimal(str(per_share)) * baseline_quantities[event["symbol"]]
                        )
                    else:
                        current_quantity = positions.get(event["symbol"], {}).get("quantity", 0)
                        if current_quantity == baseline_quantities[event["symbol"]]:
                            benchmark_dividends += amount
                        else:
                            benchmark_complete = False
                            warning = (
                                f"{event['symbol']} 分红只有实际到账总额，且实际数量已偏离初始持仓；"
                                "无法精确匹配被动对照分红。"
                            )
                            if warning not in benchmark_warnings:
                                benchmark_warnings.append(warning)
            elif event_type == "fee" and event["related_event_id"] is None:
                cash -= amount
                fees += amount
                if validate and cash < 0:
                    raise DomainError("现金不足，不能扣除费用")
            # A related fee is already applied exactly once at its original trade.

        return {
            "cash": cash,
            "positions": positions,
            "realized_pnl": realized,
            "dividends": dividends,
            "benchmark_dividends": benchmark_dividends,
            "benchmark_complete": benchmark_complete,
            "benchmark_warnings": benchmark_warnings,
            "fees": fees,
            "net_inflows": net_inflows,
            "unknown_fee_event_ids": unknown,
        }

    def _marks(self, prices, baseline, state, events, valuations):
        warnings = []
        marks = {}
        source_parts = []
        if prices is not None:
            if not isinstance(prices, dict):
                raise DomainError("prices 必须是证券代码到价格的对象")
            marks.update({_symbol(k): _decimal(v, f"{k} price", positive=True) for k, v in prices.items()})
            if marks:
                source_parts.append("provided")
        if valuations:
            latest = max(valuations, key=_valuation_key)
            added = False
            for symbol, value in latest["prices"].items():
                if symbol not in marks:
                    marks[symbol] = Decimal(str(value))
                    added = True
            if added:
                source_parts.append(f"valuation:{latest['as_of']}")
                if prices is not None:
                    warnings.append("当前行情不完整；缺失代码沿用最近已记录估值。")
        for event in sorted(events, key=_event_key, reverse=True):
            if event["status"] == self.mode and event["type"] in {"buy", "sell"} and event["symbol"] not in marks:
                marks[event["symbol"]] = Decimal(str(event["price"]))
                if "trade" not in source_parts:
                    source_parts.append("trade")
                warnings.append(f"{event['symbol']} 缺少估值价格，暂用最近成交价推算。")
        baseline_added = False
        for position in baseline["positions"]:
            if position["symbol"] not in marks:
                marks[position["symbol"]] = Decimal(str(position["price"]))
                baseline_added = True
        if baseline_added:
            source_parts.append("baseline")
            warnings.append("部分标的尚无行情、估值或成交价；暂沿用基线价格推算。")
        return marks, "+".join(source_parts) or "cash-only", warnings

    def _monthly(self, baseline, events, valuations, cutoff):
        warnings = []
        if not valuations or cutoff is None:
            return None, [], ["没有足够的完整月边界估值，月均收益为 null。"]
        by_date: dict[date, list[dict]] = {}
        for valuation in valuations:
            by_date.setdefault(parse_time(valuation["as_of"]).date(), []).append(valuation)
        months = []
        cursor = date(parse_time(baseline["as_of"]).year, parse_time(baseline["as_of"]).month, 1)
        end_cursor = date(cutoff.year, cutoff.month, 1)
        while cursor <= end_cursor:
            try:
                first, last = self._trading_month_bounds(cursor.year, cursor.month)
            except DomainError:
                warnings.append(f"{cursor:%Y-%m} 交易日历未核验，未计算该月。")
                cursor = self._next_month(cursor)
                continue
            next_month = self._next_month(cursor)
            month_start = datetime.combine(cursor, time.min, CN_TZ)
            month_end = datetime.combine(next_month, time.min, CN_TZ)
            if cutoff < month_end:
                break
            # Require a full opening boundary, never the first day's close.
            baseline_at = parse_time(baseline['as_of'])
            start = None
            if month_start <= baseline_at <= datetime.combine(first, time(9, 30), CN_TZ):
                start = {'as_of':baseline['as_of'],'prices':{p['symbol']:p['price'] for p in baseline['positions']}}
                start_cutoff = baseline_at
            else:
                previous = cursor - timedelta(days=1)
                try:
                    while not is_trading_day(previous):
                        previous -= timedelta(days=1)
                    starts = sorted([v for v in by_date.get(previous,[]) if parse_time(v['as_of']).time() >= time(15)],key=_valuation_key)
                    if starts:
                        start = starts[-1]
                        start_cutoff = month_start - timedelta(microseconds=1)
                except DomainError:
                    pass
            ends = sorted([v for v in by_date.get(last, []) if parse_time(v['as_of']).time() >= time(15)], key=_valuation_key)
            if start is not None and ends:
                end = ends[-1]
                knowledge = events
                start_events = [e for e in events if parse_time(e["as_of"]) <= start_cutoff]
                end_events = [e for e in events if parse_time(e["as_of"]) < month_end]
                start_state = self._replay(baseline, start_events, knowledge_events=knowledge, validate=True)
                end_state = self._replay(baseline, end_events, knowledge_events=knowledge, validate=True)
                missing = ((set(start_state["positions"]) - set(start["prices"]))
                           | (set(end_state["positions"]) - set(end["prices"])))
                if missing:
                    warnings.append(
                        f"{cursor:%Y-%m} 边界估值缺少补录持仓价格：{'、'.join(sorted(missing))}；未计入月均。"
                    )
                    cursor = next_month
                    continue
                start_value = self._value(start_state, start["prices"])
                end_value = self._value(end_state, end["prices"])
                flow = end_state["net_inflows"] - start_state["net_inflows"]
                month_profit = end_value - start_value - flow
                month_complete = not start_state["unknown_fee_event_ids"] and not end_state["unknown_fee_event_ids"]
                months.append({
                    "month": cursor.strftime("%Y-%m"),
                    "start_as_of": start["as_of"],
                    "end_as_of": end["as_of"],
                    "net_inflows": _number(flow),
                    "net_profit": _number(month_profit) if month_complete else None,
                })
                if not month_complete:
                    warnings.append(f"{cursor:%Y-%m} 费用未完整，净收益未知；未计入月均。")
            else:
                warnings.append(f"{cursor:%Y-%m} 缺少完整月边界估值；未计入月均。")
            cursor = next_month
        complete_values = [Decimal(str(m["net_profit"])) for m in months if m["net_profit"] is not None]
        if not complete_values:
            warnings.append("没有足够的完整月边界估值，月均收益为 null。")
            return None, months, warnings
        warnings.append(f"月均仅包含 {len(complete_values)} 个边界与费用完整的月份，不代表年度稳定性。")
        return _number(sum(complete_values, _ZERO) / len(complete_values)), months, warnings

    def _drawdown(self, baseline, events, valuations, cutoff, fees_complete):
        if not valuations:
            return None, ["没有已记录估值点，最大回撤金额为 null。"]
        if not fees_complete:
            return None, ["费用尚未完整，无法给出精确最大回撤金额。"]
        adjusted_values = [Decimal(str(baseline["initial_value"]))]
        for valuation in sorted(valuations, key=_valuation_key):
            point_events = [e for e in events if parse_time(e["as_of"]) <= parse_time(valuation["as_of"])]
            state = self._replay(baseline, point_events, knowledge_events=events, validate=True)
            missing = set(state["positions"]) - set(valuation["prices"])
            if missing:
                return None, [
                    f"{valuation['as_of']} 估值缺少补录持仓价格：{'、'.join(sorted(missing))}；"
                    "无法还原全部已记录估值点，最大回撤金额为 null。"
                ]
            adjusted_values.append(self._value(state, valuation["prices"]) - state["net_inflows"])
        peak = adjusted_values[0]
        worst = _ZERO
        for value in adjusted_values:
            peak = max(peak, value)
            worst = max(worst, peak - value)
        return _number(worst), [
            f"最大回撤金额从基线初始净值起算，并仅按 {len(valuations)} 个已记录估值点计算，未覆盖点间波动。"
        ]

    @staticmethod
    def _value(state: dict, raw_prices: dict) -> Decimal:
        prices = {s: Decimal(str(v)) for s, v in raw_prices.items()}
        missing = set(state["positions"]) - set(prices)
        if missing:
            raise DomainError("估值点缺少持仓价格：" + "、".join(sorted(missing)))
        return state["cash"] + sum(
            Decimal(item["quantity"]) * prices[symbol] for symbol, item in state["positions"].items()
        )

    @staticmethod
    def _trading_month_bounds(year: int, month: int) -> tuple[date, date]:
        days = month_calendar.monthrange(year, month)[1]
        trading = [date(year, month, day) for day in range(1, days + 1) if is_trading_day(date(year, month, day))]
        if not trading:
            raise DomainError("该月没有已核验交易日")
        return trading[0], trading[-1]

    @staticmethod
    def _next_month(value: date) -> date:
        return date(value.year + (value.month == 12), 1 if value.month == 12 else value.month + 1, 1)

    def _events(self) -> list[dict]:
        return sorted([e for e in self.store.list("ledger/events") if self.mode == "actual" or e["status"] == "paper"], key=_event_key)

    def _valuations(self) -> list[dict]:
        return sorted(self.store.list(self.valuation_dir), key=_valuation_key)

    @staticmethod
    def _summary_cutoff(baseline, events, valuations, as_of):
        if as_of is not None:
            return parse_time(as_of)
        # Future paper plans must not move an actual account's knowledge time.
        return parse_time(now_iso())

    def _effective_risk_budget(self, baseline, historical_cutoff=None):
        settings = self.store.read("settings.json")
        if isinstance(settings, dict) and settings.get("risk_budget_updated_at") is not None:
            updated_at = _timestamp(settings["risk_budget_updated_at"], "risk_budget_updated_at")
            if historical_cutoff is not None and parse_time(updated_at) > historical_cutoff:
                return (
                    None,
                    "unknown",
                    updated_at,
                    "所查历史时点早于当前 settings 风险预算版本，无法还原当时有效风险预算；已按未知处理。",
                )
            raw_budget = settings.get("risk_budget")
            budget = None if raw_budget is None else _number(_decimal(raw_budget, "risk_budget"))
            return budget, "settings", updated_at, None
        if baseline is not None:
            return baseline["risk_budget"], "baseline", None, None
        return None, None, None, None

    @staticmethod
    def _unconfirmed(
        events, valuations, risk_budget, risk_budget_source, risk_budget_updated_at, risk_warning
    ):
        warnings = ["账户基线尚未由用户确认；没有默认现金或持仓。"]
        if risk_warning:
            warnings.append(risk_warning)
        return {
            "confirmed": False,
            "baseline": None,
            "positions": [],
            "cash": None,
            "cash_estimated": False,
            "net_value": None,
            "initial_value": None,
            "net_inflows": None,
            "net_profit": None,
            "realized_pnl": None,
            "unrealized_pnl": None,
            "dividends": None,
            "fees": None,
            "known_fees": None,
            "fees_complete": False,
            "benchmark_value": None,
            "incremental_profit": None,
            "monthly_average": None,
            "complete_months": [],
            "max_drawdown_amount": None,
            "drawdown_scope": "没有已确认账户基线",
            "valuation_source": None,
            "events": events,
            "valuations": valuations,
            "warnings": warnings,
            "risk_budget": risk_budget,
            "risk_budget_source": risk_budget_source,
            "risk_budget_updated_at": risk_budget_updated_at,
        }

    @staticmethod
    def _payload(payload):
        if not isinstance(payload, dict):
            raise DomainError("请求内容必须是对象")

    @staticmethod
    def _no_extra(payload: dict, allowed: set[str]):
        extra = sorted(set(payload) - allowed)
        if extra:
            raise DomainError("请求包含未知字段：" + "、".join(extra))

    @staticmethod
    def _request_id(payload: dict) -> str:
        value = payload.get("request_id")
        if not isinstance(value, str) or not value.strip() or len(value) > 250:
            raise DomainError("缺少有效 request_id；重试时请使用原标识")
        return value
