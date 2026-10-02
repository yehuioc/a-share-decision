"""Source-bound intelligence handoff. Structural readiness is computed locally."""
from __future__ import annotations

import hashlib
import json
import re
from urllib.parse import urlsplit

from .settings import settings, channel_references
from .store import DomainError, ConflictError, now_iso, parse_time, safe_id, stable_id, PROJECT_ROOT

CATEGORIES = ("market_context", "expectations", "flows_positions", "relative_strength",
              "upstream", "company", "event_calendar", "anomalies")


def is_local_collection(reference):
    """Local producer identity, never a URL for an underlying financial fact."""
    return isinstance(reference, str) and bool(re.fullmatch(r"codex-automation:[A-Za-z0-9_-]{1,80}", reference))


def handoff_template(store):
    channel = settings(store)["channels"]["trading"] or {}
    template = {"schema": "research-packet-v2", "request_id": "", "message_id": "",
                "source_reference": channel.get("reference", ""), "as_of": "", "session": "close",
                "text": "", "market": {"quotes": []}}
    prompt = (PROJECT_ROOT / "prompts" / "intelligence-collection.md").read_text(encoding="utf-8")
    prompt += "\n\n本地当前观察名单（后续调整时以此为准）：" + "、".join(
        x["symbol"] + " " + x.get("name", "") for x in settings(store)["watchlist"])
    return {"template": template, "prompt": prompt, "source_reference": channel.get("reference")}


def _text(value, field):
    if not isinstance(value, str) or not value.strip():
        raise DomainError(f"情报包 {field} 必须为非空文字")
    return value.strip()


def _url(value):
    value = _text(value, "原始来源URL")
    parsed = urlsplit(value)
    if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
        raise DomainError("情报原始来源必须是可核对的HTTP(S)网址")
    return value


def _original_url(value):
    url = _url(value)
    host = (urlsplit(url).hostname or "").lower()
    if host in {"chatgpt.com", "chat.openai.com"} or host.endswith(".chatgpt.com"):
        raise DomainError("情报对话地址不是底层事实的原始来源")
    return url


def validate_packet(payload, at=None):
    """Readable research first; price eligibility is checked separately by symbol.

    Old v1 packages remain readable, but their coverage/TTL/account fields no longer
    define a universal permission to think. Import identity and factual time remain.
    """
    if not isinstance(payload, dict) or payload.get("schema") not in {"decision-packet-v1", "research-packet-v2"}:
        raise DomainError("情报包 schema 必须为 research-packet-v2（兼容旧decision-packet-v1）")
    at = parse_time(at or now_iso())
    as_of = parse_time(payload.get("as_of"))
    if as_of > at:
        raise DomainError("情报时点不能在未来")
    body = _text(payload.get("text"), "text")
    _text(payload.get("message_id"), "message_id")
    if not is_local_collection(payload.get("source_reference")):
        _url(payload.get("source_reference"))
    if payload.get("session") not in {"midday", "close", "non_trading"}:
        raise DomainError("session 必须为 midday/close/non_trading")
    warnings, sources = [], {}
    for url in re.findall(r'https?://[^\s<>\]）)]+', body):
        try:
            original = _original_url(url)
        except DomainError:
            continue
        sources.setdefault(original, [])
    if as_of.date() < at.date():
        warnings.append("保留原包日期阅读；旧事实不冒充今天新增或当前价格")
    # Explain old structured claims without requiring every metadata field again.
    items = [item for key in ("facts", "events") for item in
             (payload[key] if isinstance(payload.get(key), list) else [])]
    for item in items:
        if not isinstance(item, dict):
            continue
        identity = str(item.get("id") or "原包事实")
        try:
            url = _original_url(item.get("source_url"))
            sources.setdefault(url, []).append(identity)
        except DomainError:
            warnings.append(f"{identity} 缺少底层来源链接，只能作待核实线索")
        if item.get("conflict") not in (None, "none", "resolved"):
            warnings.append(f"{identity} 保留未解决冲突；不据此作确定结论")
    if not sources:
        warnings.append("正文未附可直接打开的底层链接；未核实内容只能作线索，不能当作已确认事实")
    if payload.get("upstream_ready_state") == "NOT_READY":
        warnings.append("源端未齐备标签保留作缺口提示；本地按具体标的价格与判断所需证据继续研究")
    return {"ready_state": "READY_WITH_GAPS" if warnings else "READY", "gaps": [],
            "warnings": warnings, "source_groups": sources,
            "validation_scope": "核对原文、来源身份与数据时间；辅助字段缺失不阻断研究，价格按标的另行核对",
            "validated_at": at.isoformat()}


def _import_packet_market(store, value, request_id, warnings):
    """Keep a readable packet and usable symbols when one quote is unusable."""
    from .market import import_market, number
    quotes, daily = [], {}
    raw_quotes = value.get("quotes", []) if isinstance(value, dict) else []
    raw_daily = value.get("daily", {}) if isinstance(value, dict) else {}
    if not isinstance(raw_quotes, list):
        warnings.append("包内行情格式不可读取；正文已保留")
        return None
    if not isinstance(raw_daily, dict):
        warnings.append("历史日线格式不可读取；继续采用有效主价格")
        raw_daily = {}
    counts = [q.get("symbol") for q in raw_quotes if isinstance(q, dict)]
    for raw in raw_quotes:
        if not isinstance(raw, dict):
            warnings.append("忽略无法识别的报价条目")
            continue
        symbol = raw.get("symbol")
        try:
            _original_url(raw.get("price_source"))
            if counts.count(symbol) > 1:
                raise DomainError("同一标的有重复报价，须先消除歧义")
            candidate = {"request_id": request_id, "quotes": [raw],
                         "daily": {symbol: raw_daily[symbol]} if symbol in raw_daily else {}}
            # A conflicting close is a main-price problem, not optional history.
            series = candidate["daily"].get(symbol, {})
            rows = series.get("rows", []) if isinstance(series, dict) else []
            if not isinstance(series, dict) or not isinstance(rows, list) or any(not isinstance(row, dict) for row in rows):
                warnings.append(f"{symbol} 历史日线格式不可读取；继续采用主价格")
                candidate["daily"], rows = {}, []
            for row in rows:
                if raw.get("phase") == "closed" and row.get("date") == str(raw.get("as_of", ""))[:10]:
                    try:
                        close = number(row.get("close"), True)
                    except DomainError:
                        continue
                    if abs(close - number(raw.get("price"), True)) > .001:
                        raise DomainError("主价格与同日日线收盘冲突")
            try:
                import_market(store, candidate, validate_only=True)
            except DomainError as exc:
                candidate = {"request_id": request_id, "quotes": [{k: raw[k] for k in
                    ("symbol", "name", "price", "as_of", "phase", "price_source") if k in raw}]}
                import_market(store, candidate, validate_only=True)
                warnings.append(f"{symbol} 仅采用主价格，停用不一致的辅助行情：{exc}")
            quotes.extend(candidate["quotes"])
            daily.update(candidate.get("daily", {}))
        except (DomainError, TypeError, AttributeError) as exc:
            warnings.append(f"{symbol or '未知标的'} 报价未采用：{exc}；其他标的继续")
    if not quotes:
        warnings.append("本包未取得可导入的主价格；正文仍可阅读，定量研究等待有效价格")
        return None
    return import_market(store, {"request_id": request_id, "quotes": quotes, "daily": daily}, validate_only=True)


def import_packet(store, payload):
    if not isinstance(payload, dict):
        raise DomainError("情报包必须为对象")
    channel = settings(store)["channels"]["trading"]
    if not channel or payload.get("source_reference") not in channel_references(channel):
        raise DomainError("情报包不属于用户当前绑定的交易信息来源")
    validation = validate_packet(payload)
    request_id = _text(payload.get("request_id"), "request_id")
    eid = stable_id("packet", channel["source_id"] + ":" + payload["message_id"])
    raw = json.dumps(payload, ensure_ascii=False, sort_keys=True, allow_nan=False)
    # A retry with a different request id is still the same immutable source message.
    fingerprint_body = {k: v for k, v in payload.items() if k != "request_id"}
    digest = hashlib.sha256(json.dumps(fingerprint_body, ensure_ascii=False, sort_keys=True).encode()).hexdigest()
    market = None
    if payload.get("market"):
        market_channel = settings(store)["channels"]["market"]
        if not market_channel or market_channel["mode"] != "manual" or market_channel.get("reference") != channel["reference"]:
            validation["warnings"].append("包内行情未写入：当前价格渠道与情报渠道不同或未指定")
        else:
            market = _import_packet_market(store, payload["market"], eid + "-market", validation["warnings"])
    with store.transaction():
        old = store.read(f"evidence/{eid}.json")
        if old:
            if old["metadata"]["input_sha256"] != digest:
                raise ConflictError("同一来源消息的情报包不可覆盖；修订须保留新消息ID")
            return old
        captured = now_iso()
        blob = f"source_snapshots/{eid}.json"
        store.write_text(blob, raw + "\n")
        evidence = {"id": eid, "kind": "intelligence_packet", "source_id": channel["source_id"],
                    "title": payload.get("title") or channel["name"], "url": payload["source_reference"],
                    "published_at": payload["as_of"], "captured_at": captured, "available_at": captured,
                    "status": "ready", "text": payload["text"],
                    "metadata": {"input_sha256": digest, "original_relative_path": blob,
                                 "request_id": request_id, "message_id": payload["message_id"],
                                 "packet": payload, "validation": validation, "warnings": validation["warnings"],
                                 "market_evidence_id": (market or {}).get("id"), "sync_mode": "structured_import"}}
        if market:
            store.write(f"evidence/{market['id']}.json", market)
        return store.write(f"evidence/{eid}.json", evidence)


def latest_packet(store, at=None):
    channel = settings(store)["channels"]["trading"]
    if not channel:
        return {"ready_state": "NOT_READY", "gaps": ["未指定交易信息渠道"], "packet": None}
    at = parse_time(at or now_iso())
    rows = [e for e in store.list("evidence") if e.get("kind") == "intelligence_packet" and
            e.get("source_id") == channel["source_id"] and parse_time(e["available_at"]) <= at]
    primary = [e for e in rows if e.get("url") == channel.get("reference")]
    local = is_local_collection(channel.get("reference"))
    latest = max(primary if local and primary else rows,
                 key=lambda e: (e["published_at"], e["captured_at"]), default=None)
    if not latest:
        return {"ready_state": "NOT_READY", "gaps": ["尚未收到指定来源的研究正文；同步原文与可用报价即可，无需账户或全套元数据"], "packet": None}
    report = validate_packet(latest["metadata"]["packet"], at)
    report["warnings"] = list(dict.fromkeys(report["warnings"] + latest["metadata"].get("warnings", [])))
    if local and not primary:
        report["warnings"].append("本地采集主包尚未到达；当前显示网页补充情报，价格仍按独立行情渠道核对")
    if report["warnings"]:
        report["ready_state"] = "READY_WITH_GAPS"
    return {**report, "evidence_id": latest["id"], "packet": latest["metadata"]["packet"],
            "imported_at": latest["captured_at"], "sync_mode": "structured_import",
            "as_of": latest["metadata"]["packet"]["as_of"],
            "session": latest["metadata"]["packet"]["session"], "source_reference": latest["url"],
            "source_role": "primary" if not local or latest in primary else "supplement",
            "upstream_ready_state": latest["metadata"]["packet"].get("upstream_ready_state"),
            "upstream_gaps": latest["metadata"]["packet"].get("upstream_gaps", [])}


def sync_inbox(store):
    """Import atomically completed JSON handoffs; no hidden ChatGPT/API access."""
    folder = store.path("packet_inbox")
    results = []
    if not folder.exists():
        return results
    for path in sorted(folder.glob("*.json")):
        if path.is_symlink() or not path.resolve().is_relative_to(store.root):
            raise DomainError("情报收件目录不跟随外部链接")
        data = path.read_bytes()
        key = stable_id("sync", hashlib.sha256(data).hexdigest())
        receipt = store.read(f"source_syncs/{key}.json")
        if receipt and receipt["status"] == "imported":
            continue
        try:
            value = json.loads(data.decode("utf-8-sig"))
            evidence = import_packet(store, value)
            receipt = {"id": key, "path": str(path), "status": "imported", "evidence_id": evidence["id"], "at": now_iso()}
        except (ValueError, UnicodeError, DomainError) as exc:
            receipt = {"id": key, "path": str(path), "status": "blocked", "error": str(exc), "at": now_iso()}
        store.write(f"source_syncs/{key}.json", receipt, overwrite=True)
        results.append(receipt)
    return results
