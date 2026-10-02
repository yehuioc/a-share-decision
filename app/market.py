"""Read-only Tencent quote and unadjusted daily adapter."""
from __future__ import annotations
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta
from decimal import Decimal, InvalidOperation
import hashlib
import json
import re
import requests
from .store import Store, DomainError, ConflictError, stable_id, now_iso, parse_time, CN_TZ
from .calendar import expected_market_date, calendar_status
from .calendar import is_trading_day
from .settings import settings, market_channel

QUOTE_URL = "https://qt.gtimg.cn/q="
DAILY_URL = "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"


def vendor_symbol(symbol):
    if not isinstance(symbol, str) or not re.fullmatch(r"[056][0-9]{5}", symbol):
        raise DomainError("标的必须是沪深六位股票或 ETF 代码")
    return ("sh" if symbol.startswith(("5", "6")) else "sz") + symbol


def number(value, positive=False):
    if isinstance(value, bool):
        raise DomainError("行情数字不能为布尔值")
    try:
        n = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DomainError("行情出现非法数字")
    if not n.is_finite() or n < 0 or (positive and n <= 0):
        raise DomainError("行情数字超出有效范围")
    return float(n)


def decode_response(response):
    # The provider normally serves GBK; accept UTF-8 if transport transcodes it.
    try:
        return response.content.decode("utf-8-sig")
    except UnicodeDecodeError:
        return response.content.decode("gb18030", errors="strict")


def parse_quotes(text, symbols, at=None):
    at = parse_time(at or now_iso())
    expected = expected_market_date(at)
    quotes, errors = [], []
    for symbol in symbols:
        match = re.search(r'v_' + vendor_symbol(symbol) + r'="([^"]*)"', text)
        if not match:
            errors.append(f"{symbol} 报价缺失")
            continue
        parts = match.group(1).split("~")
        try:
            if len(parts) < 35 or parts[2] != symbol:
                raise DomainError("行情字段不完整或代码错配")
            dt = datetime.strptime(parts[30], "%Y%m%d%H%M%S").replace(tzinfo=CN_TZ)
            if dt > at + timedelta(minutes=3):
                raise DomainError("报价时点晚于本次获取时间")
            if dt.date() > expected:
                raise DomainError("报价交易日超出当前可用交易时段，需核查来源日期")
            quotes.append({"symbol": symbol, "name": parts[1], "price": number(parts[3], True),
                "previous_close": number(parts[4]), "open": number(parts[5]),
                "high": number(parts[33]), "low": number(parts[34]), "volume": number(parts[6]) * 100,
                "volume_unit": "shares", "amount": number(parts[37]) * 10000 if len(parts) > 37 and parts[37] else None,
                "amount_unit": "CNY", "price_source": QUOTE_URL + vendor_symbol(symbol),
                "price_as_of": dt.isoformat(), "data_timing": "closing_snapshot" if dt.hour >= 15 else "delayed_or_realtime_unconfirmed",
                "as_of": dt.isoformat(), "trade_date": dt.date().isoformat(),
                "phase": "closed" if dt.hour >= 15 else "intraday",
                "stale": dt.date() < expected, "expected_trade_date": expected.isoformat()})
        except (ValueError, IndexError, DomainError) as exc:
            errors.append(f"{symbol} 报价不可用：{exc}")
    return quotes, errors


def fetch_daily(symbol, expected):
    code = vendor_symbol(symbol)
    response = requests.get(DAILY_URL, params={"param": f"{code},day,,,320,"}, timeout=(5, 15))
    response.raise_for_status()
    payload = response.json()
    if payload.get("code", 0) != 0:
        raise DomainError("日线接口返回业务失败")
    raw = payload.get("data", {}).get(code, {}).get("day")
    if not isinstance(raw, list) or not raw:
        raise DomainError("缺少未复权 day 数据，不用 qfqday 代替")
    rows = []
    for row in raw:
        if len(row) < 6 or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", str(row[0])):
            raise DomainError("日线格式异常")
        if row[0] > expected:
            raise DomainError("日线包含未来交易日")
        rows.append({"date": row[0], "open": number(row[1], True), "close": number(row[2], True),
                     "high": number(row[3], True), "low": number(row[4], True), "volume": number(row[5])})
    if len({x["date"] for x in rows}) != len(rows):
        raise DomainError("日线存在重复交易日")
    rows.sort(key=lambda x: x["date"])
    return {"adjustment": "none", "volume_unit": "lots", "rows": rows, "url": response.url,
            "sha256": hashlib.sha256(response.content).hexdigest(), "raw": payload}


def collect(store: Store, request_id: str, symbols=None):
    channel = market_channel(store)
    if channel["mode"] != "tencent":
        raise DomainError(f"用户指定的{channel['name']}采用人工资料输入；不会自动转用腾讯或其他渠道")
    symbols = symbols or [x["symbol"] for x in settings(store)["watchlist"]]
    if not symbols or len(set(symbols)) != len(symbols) or len(symbols) > 12:
        raise DomainError("行情标的列表为空、重复或过长")
    codes = [vendor_symbol(s) for s in symbols]
    evidence_id = stable_id("market", request_id)
    old = store.read(f"evidence/{evidence_id}.json")
    if old:
        if old["metadata"].get("symbols") != symbols:
            raise ConflictError("同一行情请求标识不能用于不同标的")
        return old
    started = now_iso()
    expected = expected_market_date(started).isoformat()
    started_at = parse_time(started)
    completed_through = (expected_market_date(started_at.replace(hour=0, minute=0)).isoformat()
                         if is_trading_day(started_at.date()) and started_at.hour < 15 else expected)
    warnings, daily, raw_quote, quotes = [], {}, "", []
    try:
        response = requests.get(QUOTE_URL + ",".join(codes), timeout=(5, 15))
        response.raise_for_status()
        raw_quote = decode_response(response)
        quotes, errors = parse_quotes(raw_quote, symbols, now_iso())
        warnings.extend(errors)
    except (requests.RequestException, UnicodeError, DomainError) as exc:
        warnings.append(f"报价获取失败：{exc}")
    with ThreadPoolExecutor(max_workers=min(4, len(symbols))) as pool:
        futures = {pool.submit(fetch_daily, s, expected): s for s in symbols}
        for future in as_completed(futures):
            symbol = futures[future]
            try:
                series = future.result()
                series["rows"] = [row for row in series["rows"] if row["date"] <= completed_through]
                if not series["rows"]:
                    raise DomainError("尚无已完成交易日日线")
                daily[symbol] = series
                if daily[symbol]["rows"][-1]["date"] != completed_through:
                    warnings.append(f"{symbol} 日线未更新至已完成交易日 {completed_through}")
                rows = daily[symbol]['rows']
                breaks = [b['date'] for a, b in zip(rows, rows[1:]) if b['close'] / a['close'] < .65 or b['close'] / a['close'] > 1.5]
                if breaks:
                    warnings.append(f"{symbol} 未复权日线出现明显价格断点（{', '.join(breaks)}），可能涉及份额折算或公司行动；跨断点涨跌幅不能直接作为投资收益")
            except Exception as exc:
                warnings.append(f"{symbol} 日线获取失败：{exc}")
    for q in quotes:
        if q["stale"]:
            warnings.append(f"{q['symbol']} 报价陈旧，实际 {q['trade_date']}，应为 {expected}")
        rows = daily.get(q["symbol"], {}).get("rows", [])
        if q["phase"] == "closed" and rows and rows[-1]["date"] == q["trade_date"] and abs(rows[-1]["close"] - q["price"]) > 0.011:
            warnings.append(f"{q['symbol']} 同一供应商报价与日线收盘不一致，须人工核查")
        enrich_quote(q, daily.get(q["symbol"], {}))
    captured = now_iso()
    lines = [f"{q['symbol']} {q['name']}：{q['price']} 元，报价时间 {q['as_of']}，状态 {q['phase']}，陈旧={q['stale']}。" for q in quotes]
    for symbol, series in daily.items():
        lines.append(f"{symbol} 未复权日线 {series['rows'][0]['date']} 至 {series['rows'][-1]['date']}，{len(series['rows'])} 行。")
    evidence = {"id": evidence_id, "kind": "market", "source_id": "tencent", "title": "观察名单行情与未复权日线",
                "url": QUOTE_URL + ",".join(codes), "published_at": max([q["as_of"] for q in quotes], default=None),
                "captured_at": captured, "available_at": captured, "status": "ready" if quotes or daily else "error",
                "text": "\n".join(lines), "metadata": {"symbols": symbols, "quotes": quotes, "daily": daily,
                "warnings": warnings, "raw_quotes": raw_quote, "raw_quote_sha256": hashlib.sha256(raw_quote.encode("utf-8")).hexdigest(),
                "calendar": calendar_status(expected), "expected_trade_date": expected, "started_at": started}}
    return store.write(f"evidence/{evidence_id}.json", evidence)


def latest_market(store, at=None):
    items = [e for e in store.list("evidence") if e.get("kind") == "market"]
    latest = max(items, key=lambda e: e["captured_at"], default=None)
    if latest is None:
        return None
    latest.setdefault('metadata', {}).setdefault('warnings', [])
    # Freshness is a viewing-time property; saved evidence remains immutable.
    now = parse_time(at or now_iso())
    try:
        expected = expected_market_date(now).isoformat()
    except DomainError as exc:
        latest['metadata']['warnings'].append(str(exc))
        expected = None
    for quote in latest.get('metadata', {}).get('quotes', []):
        stale = expected is None or quote.get('trade_date') != expected
        if not stale and quote.get('phase') == 'intraday':
            lunch = morning_research_basis(parse_time(quote['as_of']), now)
            stale = now.hour >= 15 or (not lunch and (now - parse_time(quote['as_of'])).total_seconds() > 15 * 60)
        quote['stale'] = stale
        if stale:
            latest['metadata']['warnings'].append(f"{quote['symbol']} 保存的报价已不满足当前时点，请刷新行情后再使用")
    return latest


def import_market(store: Store, payload: dict, *, validate_only=False):
    """Import user-supplied prices under the explicitly selected source, without IO to that source."""
    channel = market_channel(store)
    request_id = payload.get("request_id")
    if not isinstance(request_id, str) or not request_id.strip():
        raise DomainError("行情导入需要 request_id")
    captured = now_iso()
    at = parse_time(captured)
    expected = expected_market_date(at).isoformat()
    quotes, daily, symbols = [], {}, set()
    raw_quotes = payload.get("quotes", [])
    raw_daily = payload.get("daily", {})
    if not isinstance(raw_quotes, list) or not isinstance(raw_daily, dict):
        raise DomainError("quotes 应为列表，daily 应为对象")
    for raw in raw_quotes:
        if not isinstance(raw, dict):
            raise DomainError("报价必须是对象")
        symbol = raw.get("symbol")
        vendor_symbol(symbol)
        if symbol in symbols:
            raise DomainError("报价不能重复证券代码")
        when = parse_time(raw.get("price_as_of") or raw.get("as_of"))
        if raw.get("price_as_of") and raw.get("as_of") and parse_time(raw["price_as_of"]) != parse_time(raw["as_of"]):
            raise DomainError("as_of 与 price_as_of 不一致")
        trade_date = when.date().isoformat()
        phase = raw.get("phase")
        if when > at or not is_trading_day(when.date()) or trade_date > expected:
            raise DomainError("报价时间为未来或非交易日")
        if phase not in ("closed", "intraday") or (phase == "closed" and when.hour < 15):
            raise DomainError("必须区分盘中与收盘，15点前不能标成收盘价")
        symbols.add(symbol)
        quotes.append({"symbol": symbol, "name": str(raw.get("name", symbol))[:80],
                       "price": number(raw.get("price"), True), "as_of": when.isoformat(),
                       "trade_date": trade_date, "phase": phase, "stale": trade_date != expected,
                       "expected_trade_date": expected})
        quote = quotes[-1]
        for key in ("previous_close", "open", "high", "low", "volume", "amount"):
            quote[key] = number(raw[key], key not in ("volume", "amount")) if raw.get(key) is not None else None
        for key in ("volume_unit", "amount_unit", "data_timing"):
            quote[key] = str(raw.get(key) or "unknown")
        quote["price_source"] = str(raw.get("price_source") or channel["reference"] or channel["name"])
        quote["price_as_of"] = when.isoformat()
        if all(quote[k] is not None for k in ("open", "high", "low")) and not (
            quote["low"] <= min(quote["open"], quote["price"]) <= max(quote["open"], quote["price"]) <= quote["high"]
        ):
            raise DomainError("报价高低价与开盘/当前价矛盾")
        for key in ("change", "change_pct"):
            if raw.get(key) is not None and quote["previous_close"]:
                expected_change = quote["price"] - quote["previous_close"]
                expected_value = expected_change if key == "change" else expected_change / quote["previous_close"] * 100
                try:
                    reported = Decimal(str(raw[key]))
                except InvalidOperation as exc:
                    raise DomainError("涨跌字段不是有限数字") from exc
                if not reported.is_finite() or abs(float(reported) - expected_value) > (.0011 if key == "change" else .011):
                    raise DomainError("涨跌额/幅与价格前收不一致")
    for symbol, series in raw_daily.items():
        vendor_symbol(symbol)
        if not isinstance(series, dict) or series.get("adjustment") != "none":
            raise DomainError("研究日线须明确 adjustment:none，不能混入复权序列")
        rows = series.get("rows")
        if not isinstance(rows, list) or not rows or len(rows) > 5000:
            raise DomainError("日线应包含1至5000条记录")
        out, dates = [], set()
        for row in rows:
            if not isinstance(row, dict):
                raise DomainError("日线必须是对象")
            day = str(row.get("date", ""))
            try:
                parsed = datetime.strptime(day, "%Y-%m-%d").date()
            except ValueError as exc:
                raise DomainError("日线日期格式错误") from exc
            if day in dates or day > expected or not is_trading_day(parsed):
                raise DomainError("日线日期重复、未来或非交易日")
            if day == at.date().isoformat() and at.hour < 15:
                raise DomainError("当日尚未收盘，不能导入当日完整日线")
            dates.add(day)
            point = {key: number(row.get(key), True) for key in ("open", "close", "high", "low")}
            point.update(date=day, volume=number(row["volume"]) if row.get("volume") is not None else None)
            if row.get("amount") is not None:
                point["amount"] = number(row["amount"])
            if not point["low"] <= min(point["open"], point["close"]) <= max(point["open"], point["close"]) <= point["high"]:
                raise DomainError("日线高低价与开收盘价矛盾")
            out.append(point)
        symbols.add(symbol)
        daily[symbol] = {"adjustment": "none", "rows": sorted(out, key=lambda x: x["date"]),
                         "volume_unit": str(series.get("volume_unit", "unknown"))}
    if not symbols or len(symbols) > 12:
        raise DomainError("一次行情导入须包含1至12个标的")
    for quote in quotes:
        rows = daily.get(quote["symbol"], {}).get("rows", [])
        matching = [r for r in rows if r["date"] == quote["trade_date"]]
        if quote["phase"] == "closed" and matching and abs(matching[0]["close"] - quote["price"]) > .001:
            raise DomainError("同日收盘报价与日线不一致，请先核对指定渠道原件")
        if quote["phase"] == "closed" and matching:
            for key in ("open", "high", "low", "amount"):
                if quote.get(key) is None:
                    quote[key] = matching[0].get(key)
                elif matching[0].get(key) is not None and abs(quote[key] - matching[0][key]) > .001:
                    raise DomainError("报价与日线的开高低/成交额不一致")
            if quote.get("volume") is None:
                quote["volume"] = matching[0]["volume"]
                quote["volume_unit"] = daily[quote["symbol"]]["volume_unit"]
        enrich_quote(quote, daily.get(quote["symbol"], {}))
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    fingerprint = hashlib.sha256((channel["source_id"] + raw).encode("utf-8")).hexdigest()
    eid = stable_id("market-import", request_id)
    with store.transaction():
        old = store.read(f"evidence/{eid}.json")
        if old:
            if old["metadata"].get("input_sha256") != fingerprint:
                raise ConflictError("同一行情导入请求对应了不同内容或渠道")
            return old
        evidence = {"id": eid, "kind": "market", "source_id": channel["source_id"],
                    "title": channel["name"] + "用户提供行情", "url": channel["reference"],
                    "published_at": max((q["as_of"] for q in quotes), default=None),
                    "captured_at": captured, "available_at": captured, "status": "ready",
                    "text": "用户按指定渠道提供的行情；未联网抓取。",
                    "metadata": {"symbols": sorted(symbols), "quotes": quotes, "daily": daily,
                                 "warnings": [], "expected_trade_date": expected, "channel": channel,
                                 "input_sha256": fingerprint, "original_input": payload}}
        return evidence if validate_only else store.write(f"evidence/{eid}.json", evidence)


def enrich_quote(quote, series):
    rows = sorted([r for r in series.get("rows", []) if r["date"] < quote["trade_date"]], key=lambda r: r["date"])
    from .calendar import next_trading_day
    if not quote.get("previous_close") and rows and next_trading_day(rows[-1]["date"]).isoformat() == quote["trade_date"]:
        quote["previous_close"] = rows[-1]["close"]
    previous = quote.get("previous_close")
    quote["change"] = round(quote["price"] - previous, 6) if previous else None
    quote["change_pct"] = round(quote["change"] / previous * 100, 6) if previous else None
    performance = {"1d": quote["change_pct"], "5d": None, "20d": None}
    warnings = []
    for h in (5, 20):
        if len(rows) < h:
            warnings.append(f"{h}日表现缺少足够的原始交易日历史")
            continue
        window = rows[-h:]
        prices = [r["close"] for r in window] + [quote["price"]]
        if any(not .65 <= b / a <= 1.5 for a, b in zip(prices, prices[1:])):
            warnings.append(f"{h}日窗口存在价格断点，不能将未复权变动当成收益")
            continue
        expected_dates = list(_market_sessions(window[0]["date"], quote["trade_date"]))
        if len(expected_dates) != h + 1 or [r["date"] for r in window] != expected_dates[:-1]:
            warnings.append(f"{h}日窗口交易日不连续")
            continue
        performance[f"{h}d"] = round((quote["price"] / window[0]["close"] - 1) * 100, 6)
    quote["performance_pct"] = performance
    quote["performance_basis"] = "未复权价格变动，非含分红总回报"
    quote["performance_warnings"] = warnings
    quote["missing_fields"] = [k for k in ("price", "previous_close", "open", "high", "low", "volume", "amount", "price_as_of", "price_source") if quote.get(k) is None or quote.get(k) == ""]
    if quote.get("volume_unit") not in ("shares", "lots"):
        quote["missing_fields"].append("volume_unit")
    if quote.get("amount_unit") != "CNY":
        quote["missing_fields"].append("amount_unit")
    if quote.get("data_timing") not in ("realtime", "delayed", "closing_snapshot", "delayed_or_realtime_unconfirmed"):
        quote["missing_fields"].append("data_timing")


def _market_sessions(start, end):
    from .calendar import next_trading_day
    day = datetime.strptime(start, "%Y-%m-%d").date()
    end = datetime.strptime(end, "%Y-%m-%d").date()
    while day <= end:
        yield day.isoformat()
        if day == end:
            break
        day = next_trading_day(day)


def market_readiness(evidence, symbols, at=None):
    """Only a usable dated price is a prerequisite for quantitative research."""
    at = parse_time(at or now_iso())
    expected = expected_market_date(at).isoformat()
    quotes = {q["symbol"]: q for q in (evidence or {}).get("metadata", {}).get("quotes", [])}
    usable, unavailable, warnings = [], [], []
    for symbol in symbols:
        q = quotes.get(symbol)
        issues = []
        if not q:
            issues.append("缺少可核对的主价格")
        else:
            try:
                number(q.get("price"), True)
                when = parse_time(q.get("as_of"))
                if when > at or q.get("trade_date") != expected:
                    issues.append("价格日期不符合本轮时点")
                phase = q.get("phase")
                if phase not in {"intraday", "closed", "close"}:
                    issues.append("未区分盘中与收盘价格")
                elif phase == "intraday":
                    morning_basis = morning_research_basis(when, at)
                    if (at.date().isoformat() != expected or at.hour >= 15 or
                        (not morning_basis and (at-when).total_seconds() > 900)):
                        issues.append("需要刷新价格，不能把旧盘中价当当前或收盘价")
                elif when.hour < 15:
                    issues.append("收盘前的价格不能冒充正式收盘")
            except DomainError:
                issues.append("主价格或价格时间无效")
            if not q.get("price_source"):
                issues.append("主价格缺少可识别来源")
            if not issues:
                enrich_quote(q, (evidence or {}).get("metadata", {}).get("daily", {}).get(symbol, {}))
                labels = {"previous_close": "前收", "open": "开盘价", "high": "最高价",
                          "low": "最低价", "volume": "成交量", "amount": "成交额"}
                optional = [labels[k] for k in q["missing_fields"] if k in labels]
                if optional:
                    warnings.append(f"{symbol} 未取得{'、'.join(optional)}；不使用对应论据，价格情景仍可研究")
                warnings.extend(f"{symbol} {w}；不妨碍当前价格研究" for w in q["performance_warnings"])
        if issues:
            unavailable.append({"symbol": symbol, "reasons": issues})
        else:
            usable.append(symbol)
    skipped = [f"{item['symbol']} 暂不作定量计划：{'；'.join(item['reasons'])}" for item in unavailable]
    if usable:
        warnings.extend(skipped)
    return {"ready_state": "NOT_READY" if not usable else "READY_WITH_GAPS" if warnings else "READY",
            "gaps": skipped if not usable else [], "warnings": warnings,
            "usable_symbols": usable, "unavailable_symbols": unavailable}


def morning_research_basis(when, at):
    # The 13:00 research uses the morning snapshot; it is not a live execution quote.
    return (when.date() == at.date() and (11, 25) <= (when.hour, when.minute) <= (13, 0)
            and (11, 30) <= (at.hour, at.minute) <= (13, 30))
