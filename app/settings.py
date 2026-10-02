from .store import Store, DomainError, now_iso, safe_id, stable_id
import copy
import hashlib
import re
import math
from urllib.parse import urlsplit

DEFAULTS = {
    "watchlist": [{"symbol": "601398", "name": "工商银行"}, {"symbol": "510300", "name": "沪深300ETF"}, {"symbol": "588290", "name": "科创芯片ETF"}],
    "risk_budget": None,
    "income_goals": [100, 500, 1000],
    "stage": 1,
    "channels": {"market": None, "trading": None, "caixin_enabled": False},
}


def settings(store: Store):
    result = copy.deepcopy(DEFAULTS)
    result.update(store.read("settings.json", {}))
    result["channels"] = {**DEFAULTS["channels"], **(result.get("channels") or {})}
    return result


def normalise_channel(value, kind):
    if value is None:
        return None
    if not isinstance(value, dict):
        raise DomainError("渠道必须是对象或空值")
    name, reference = str(value.get("name", "")).strip(), str(value.get("reference", "")).strip()
    if not name or len(name) > 120 or len(reference) > 1000:
        raise DomainError("请填写明确的渠道名称，名称最多120字，地址最多1000字")
    mode = value.get("mode", "manual")
    if mode not in ("manual", "tencent") or (kind != "market" and mode != "manual"):
        raise DomainError("当前仅支持人工提供资料，或用户明确选择腾讯行情适配器")
    source_id = "tencent" if mode == "tencent" else stable_id("user-source", hashlib.sha256((name + "|" + reference).encode()).hexdigest())
    channel = {"name": name, "reference": reference, "mode": mode, "source_id": safe_id(source_id)}
    extra = value.get("additional_references", [])
    if not isinstance(extra, list) or len(extra) > 10:
        raise DomainError("同一渠道的补充来源地址须为至多10项的列表")
    if extra and (kind != "trading" or not reference):
        raise DomainError("补充来源仅用于已有主地址的交易情报渠道")
    for address in extra:
        if not isinstance(address, str) or len(address) > 1000:
            raise DomainError("补充来源地址不合法")
        try:
            parsed = urlsplit(address)
        except ValueError as exc:
            raise DomainError("补充来源地址不合法") from exc
        if parsed.scheme not in ("http", "https") or not parsed.netloc or parsed.username or parsed.password:
            raise DomainError("补充来源必须是明确的HTTP(S)地址")
    if extra:
        channel["additional_references"] = sorted(set(extra) - {reference})
    return channel


def channel_references(channel):
    return {channel.get("reference"), *channel.get("additional_references", [])} if channel else set()


def market_channel(store: Store):
    channel = settings(store)["channels"]["market"]
    if not channel:
        raise DomainError("股价渠道尚未由用户指定；请先指定渠道，不自动选择或回退到其他供应商")
    return channel


def check_market_sources(store: Store, evidence: list):
    channel = market_channel(store)
    wrong = [item["id"] for item in evidence if item.get("kind") == "market" and item.get("source_id") != channel["source_id"]]
    if wrong:
        raise DomainError("行情不来自用户当前指定渠道：" + "、".join(wrong) + "；旧证据可在历史或回放中查看")
    return channel


def update_settings(store: Store, payload: dict):
    result = settings(store)
    if "channels" in payload:
        supplied = payload["channels"]
        if not isinstance(supplied, dict) or set(supplied) - {"market", "trading", "caixin_enabled"}:
            raise DomainError("渠道设置字段不合法")
        channels = copy.deepcopy(result["channels"])
        for kind in ("market", "trading"):
            if kind in supplied:
                channels[kind] = normalise_channel(supplied[kind], kind)
        if "caixin_enabled" in supplied:
            if type(supplied["caixin_enabled"]) is not bool:
                raise DomainError("财新接入开关必须为布尔值")
            channels["caixin_enabled"] = supplied["caixin_enabled"]
        result["channels"] = channels
        result["channels_updated_at"] = now_iso()
    if "watchlist" in payload:
        rows = payload["watchlist"]
        if not isinstance(rows, list) or not 1 <= len(rows) <= 12:
            raise DomainError("观察名单应包含 1 至 12 个标的")
        seen = set()
        for row in rows:
            if not isinstance(row, dict) or not re.fullmatch(r"[056][0-9]{5}", str(row.get("symbol", ""))):
                raise DomainError("请输入沪深 A 股或股票 ETF 的六位代码")
            if row["symbol"] in seen:
                raise DomainError("观察名单存在重复代码")
            seen.add(row["symbol"])
        result["watchlist"] = [{"symbol": r["symbol"], "name": str(r.get("name", r["symbol"]))[:80]} for r in rows]
    if "risk_budget" in payload:
        value = payload["risk_budget"]
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0):
            raise DomainError("风险预算必须是非负金额或空值")
        result["risk_budget"] = value
        result["risk_budget_updated_at"] = now_iso()
    return store.write("settings.json", result, overwrite=True)
