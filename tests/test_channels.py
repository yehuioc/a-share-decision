import copy
import json

import pytest
from fastapi.testclient import TestClient

from app.caixin import sync_caixin
from app.cli import main
from app.market import collect, import_market
from app.server import create_app, current_prices
from app.settings import settings, update_settings
from app.store import Store, DomainError, ConflictError
from app.workflow import Workflow
from test_workflow import put_market, put_text, prepare, complete_roles, decision


NOW = "2026-09-28T15:30:00+08:00"


@pytest.fixture(autouse=True)
def fixed_clock(monkeypatch):
    monkeypatch.setattr("app.market.now_iso", lambda: NOW)
    monkeypatch.setattr("app.workflow.now_iso", lambda: NOW)


def choose(store, name="用户指定的券商行情"):
    return update_settings(store, {"channels": {"market": {"mode": "manual", "name": name},
                                                 "trading": {"name": "用户提供交割单"}}})


def prices():
    return {"request_id": "user-prices", "quotes": [{"symbol": "601398", "price": 8.1,
             "as_of": "2026-09-28T15:00:00+08:00", "phase": "closed", "previous_close": 8,
             "amount": 8100, "amount_unit": "CNY", "data_timing": "closing_snapshot"}],
            "daily": {"601398": {"adjustment": "none", "volume_unit": "shares", "rows": [
                {"date": "2026-09-28", "open": 8.0, "close": 8.1, "low": 7.9, "high": 8.2, "volume": 1000}]}}}


def test_unassigned_routes_and_cli_make_no_network_call(tmp_path, monkeypatch, capsys):
    store = Store(tmp_path)
    def forbidden(*args, **kwargs):
        pytest.fail("未指定渠道时不应联网")
    monkeypatch.setattr("app.market.requests.get", forbidden)
    assert settings(store)["channels"] == {"market": None, "trading": None, "caixin_enabled": False}
    with pytest.raises(DomainError, match="用户指定"):
        collect(store, "blocked")
    with pytest.raises(DomainError, match="暂停"):
        sync_caixin(store, root=tmp_path / "does-not-exist")
    with TestClient(create_app(store, testing=True)) as client:
        state = client.get("/api/state").json()
        headers = {"x-csrf-token": state["system"]["csrf_token"]}
        assert client.post("/api/market/refresh", json={}, headers=headers).status_code == 400
        assert client.post("/api/sources/caixin/sync", json={"request_id":"x"}, headers=headers).status_code == 400
        assert client.post("/api/market/import", json=prices(), headers=headers).status_code == 400
    assert main(["--data-dir", str(tmp_path), "collect", "--request-id", "blocked-cli"]) != 0
    capsys.readouterr()
    assert store.list("evidence") == []


def test_custom_channel_never_falls_back_to_tencent_and_import_freezes_source(tmp_path, monkeypatch):
    store = Store(tmp_path)
    choose(store)
    def forbidden(*args, **kwargs):
        pytest.fail("人工指定渠道不应回退联网")
    monkeypatch.setattr("app.market.requests.get", forbidden)
    with pytest.raises(DomainError, match="不会自动转用"):
        collect(store, "no-fallback")
    evidence = import_market(store, prices())
    assert evidence["source_id"] == settings(store)["channels"]["market"]["source_id"]
    assert evidence == import_market(store, prices())
    assert evidence["metadata"]["daily"]["601398"]["volume_unit"] == "shares"
    changed = prices(); changed["quotes"][0]["price"] = 8.11; changed["daily"] = {}
    with pytest.raises(ConflictError):
        import_market(store, changed)
    update_settings(store, {"channels": {"market": None}})
    assert current_prices(store) == {}
    assert store.read(f"evidence/{evidence['id']}.json") == evidence


@pytest.mark.parametrize("change", ["future", "holiday", "false-close", "duplicate", "ohlc", "adjusted", "quote-mismatch"])
def test_user_supplied_market_data_is_not_accepted_blindly(tmp_path, change):
    store = Store(tmp_path); choose(store); payload = prices()
    if change == "future": payload["quotes"][0]["as_of"] = "2026-09-29T15:00:00+08:00"
    if change == "holiday": payload["quotes"][0]["as_of"] = "2026-09-25T15:00:00+08:00"
    if change == "false-close": payload["quotes"][0]["as_of"] = "2026-09-28T10:00:00+08:00"
    if change == "duplicate": payload["quotes"].append(copy.deepcopy(payload["quotes"][0]))
    if change == "ohlc": payload["daily"]["601398"]["rows"][0]["low"] = 9
    if change == "adjusted": payload["daily"]["601398"]["adjustment"] = "qfq"
    if change == "quote-mismatch": payload["quotes"][0]["price"] = 8.15
    with pytest.raises(DomainError): import_market(store, payload)
    assert store.list("evidence") == []


def test_new_channel_blocks_old_evidence_but_not_history_and_freezes_common_method(tmp_path):
    store = Store(tmp_path); put_market(store, "market-1"); put_text(store)
    workflow = Workflow(store)
    run = prepare(workflow, "old-channel")
    complete_roles(workflow, run["id"])
    old = workflow.publish(run["id"], decision())
    frozen = store.path(f"run_events/{run['id']}/decision.json").read_bytes()
    choose(store)
    assert workflow.get_run(run["id"])["channel_review_required"] is True
    assert workflow.publish(run["id"], decision()) == old
    with pytest.raises(DomainError, match="当前指定渠道"):
        prepare(workflow, "reject-old")
    source = import_market(store, prices())
    profile = {"historical_positions": [{"symbol":"601398", "quantity":400, "broker_display_cost":7.5}]}
    store.write("research_profile.json", profile)
    new = workflow.prepare({"request_id":"new-channel", "mode":"live", "symbols":["601398"], "evidence_ids":[source["id"]]})
    ctx = workflow.context(new["id"], "opportunity")
    assert ctx["research_brief"] == profile
    assert ctx["channels"]["market"]["name"] == "用户指定的券商行情"
    assert "风险预算内只说明承受得起" in ctx["method"]
    assert "不能重复当作几条独立利好" in ctx["method"]
    store.write("research_profile.json", {"changed":True}, overwrite=True)
    assert workflow.context(new["id"], "opportunity")["research_brief"] == profile
    assert store.path(f"run_events/{run['id']}/decision.json").read_bytes() == frozen
    assert store.list("ledger/events") == []


def test_reported_sale_updates_new_context_without_rewriting_old_run(tmp_path):
    store = Store(tmp_path); put_market(store, "market-1"); put_text(store)
    workflow = Workflow(store)
    original = {"historical_positions": [{"symbol": "601398", "quantity": 400}]}
    store.write("research_profile.json", original)
    old = prepare(workflow, "before-sale")
    frozen = store.path(f"runs/{old['id']}.json").read_bytes()
    updated = {**original, "current_positions": [{"symbol": "601398", "quantity": 200}],
               "reported_trades": [{"symbol": "601398", "side": "sell", "quantity": 200,
                                     "price": 8.29, "date": None}]}
    store.write("research_profile.json", updated, overwrite=True)
    new = prepare(workflow, "after-sale")
    for role in ("opportunity", "counter"):
        context = workflow.context(new["id"], role)
        assert context["research_brief"] == updated
        assert "研究数量优先于" in context["method"]
    complete_roles(workflow, new["id"])
    assert workflow.context(new["id"], "judge", "judge")["research_brief"] == updated
    assert workflow.context(old["id"], "opportunity")["research_brief"] == original
    assert store.path(f"runs/{old['id']}.json").read_bytes() == frozen
    assert store.read("ledger/baseline.json") is None
    assert store.list("ledger/events") == []


def test_revoking_channel_after_prepare_blocks_new_publication(tmp_path):
    store = Store(tmp_path); put_market(store, "market-1"); put_text(store)
    workflow = Workflow(store); run = prepare(workflow, "pending"); complete_roles(workflow, run["id"])
    update_settings(store, {"channels": {"market":None}})
    with pytest.raises(DomainError, match="用户指定"):
        workflow.publish(run["id"], decision())
    assert workflow.get_run(run["id"])["decision"] is None


def test_channel_and_price_http_end_to_end(tmp_path):
    store = Store(tmp_path)
    with TestClient(create_app(store, testing=True)) as client:
        headers = {"x-csrf-token":client.get("/api/state").json()["system"]["csrf_token"]}
        configured = client.put("/api/settings", headers=headers, json={"channels":{"market":{"name":"我的行情文件", "mode":"manual"}}})
        assert configured.status_code == 200
        imported = client.post("/api/market/import", headers=headers, json=prices())
        assert imported.status_code == 200
        state = client.get("/api/state").json()
        assert state["market"]["latest"]["source_id"] == configured.json()["channels"]["market"]["source_id"]
        assert state["ledger"]["confirmed"] is False
        assert client.post("/api/market/refresh", headers=headers, json={}).status_code == 400
