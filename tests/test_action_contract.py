import pytest
from app.store import Store, DomainError
from app.workflow import Workflow
from test_workflow import clock, put_market, put_text, prepare, complete_roles, decision


def setup_run(tmp_path, cost_mode=None):
    store = Store(tmp_path)
    put_market(store, "market-1")
    put_text(store)
    workflow = Workflow(store)
    run = prepare(workflow, "action-v2", decision_contract=2,
                  research_brief={"historical_positions": [{"symbol": "601398", "quantity": 400}],
                                  "cost_model": {"mode": cost_mode}})
    complete_roles(workflow, run["id"])
    draft = decision()
    draft["plans"][0].update(action="hold", decision_basis="价格与估值支持持有，买入的费用后空间不足",
        long_term_view="保留盈利质量未恶化的长期仓位", sizing={
            "basis": "历史400股的未来机会成本，非实盘账本", "assumptions": ["数量仍为400股，费用估计10元"],
            "scenarios": [{"label": "继续持有400股", "quantity": 400, "entry_price": 10,
                           "exit_price": 11, "stop_price": 9, "estimated_fees": 10, "reasoning": "区间结构"}]})
    return workflow, run, draft


def test_v2_research_quantity_survives_unknown_account_and_freezes_brief(tmp_path):
    workflow, run, draft = setup_run(tmp_path)
    published = workflow.publish(run["id"], draft)
    plan = published["plans"][0]
    assert plan["quantity"] is None
    assert plan["sizing"]["scenarios"][0]["quantity"] == 400
    ctx = workflow.context(run["id"], "opportunity", "decision")
    assert ctx["research_brief"]["historical_positions"][0]["quantity"] == 400
    assert ctx["decision_contract"] == 2
    assert workflow.get_run(run["id"])["decision"]["plans"][0]["decision_basis"] == plan["decision_basis"]


@pytest.mark.parametrize("change", [
    {"action": "observe"}, {"decision_basis": ""}, {"long_term_view": ""}, {"sizing": None}, {"evidence_ids": []},
])
def test_v2_rejects_empty_decision_not_just_format(tmp_path, change):
    workflow, run, draft = setup_run(tmp_path)
    draft["plans"][0].update(change)
    with pytest.raises(DomainError):
        workflow.publish(run["id"], draft)


@pytest.mark.parametrize("change", [{"quantity": 1}, {"quantity": True}, {"estimated_fees": float("nan")},
    {"estimated_fees": -1}, {"entry_price": float("inf")}, {"stop_price": 12}])
def test_sizing_rejects_unusable_or_nonfinite_scenarios(tmp_path, change):
    workflow, run, draft = setup_run(tmp_path)
    draft["plans"][0]["sizing"]["scenarios"][0].update(change)
    with pytest.raises(DomainError):
        workflow.publish(run["id"], draft)


@pytest.mark.parametrize("action", ["sell", "avoid"])
def test_sell_and_avoid_are_real_choices(tmp_path, action):
    workflow, run, draft = setup_run(tmp_path)
    draft["plans"][0]["action"] = action
    assert workflow.publish(run["id"], draft)["plans"][0]["action"] == action


def test_direct_entry_defaults_to_current_decision_contract(tmp_path):
    store = Store(tmp_path)
    put_market(store, "market-1")
    put_text(store)
    workflow = Workflow(store)
    run = workflow.prepare({"request_id": "default-v2", "mode": "live", "symbols": ["601398"],
                            "evidence_ids": ["market-1", "text-1"]})
    assert run["decision_contract"] == 3
    complete_roles(workflow, run["id"])
    with pytest.raises(DomainError, match="不能用 observe"):
        workflow.publish(run["id"], decision())


def test_old_request_without_contract_can_still_retry(tmp_path):
    store = Store(tmp_path)
    put_market(store, "market-1")
    put_text(store)
    workflow = Workflow(store)
    run = prepare(workflow, "legacy-retry")
    old = store.read(f"runs/{run['id']}.json")
    old.pop("decision_contract")
    old["request"].pop("decision_contract")
    old["request"].pop("research_brief")
    store.write(f"runs/{run['id']}.json", old, overwrite=True)
    retry = workflow.prepare(old["request"])
    assert retry["id"] == run["id"]


def test_zero_friction_rejects_fees_and_preserves_frozen_preference(tmp_path):
    workflow, run, draft = setup_run(tmp_path, cost_mode="zero_friction")
    workflow.store.write("research_profile.json", {"cost_model": {"mode": "other"}})
    with pytest.raises(DomainError, match="零摩擦"):
        workflow.publish(run["id"], draft)
    draft["plans"][0]["sizing"]["scenarios"][0]["estimated_fees"] = 0
    draft["plans"][0]["decision_basis"] = "价格与估值支持持有"
    out = workflow.publish(run["id"], draft)
    assert out["plans"][0]["research_cost_mode"] == "zero_friction"
    assert workflow.get_run(run["id"])["research_brief"]["cost_model"]["mode"] == "zero_friction"


@pytest.mark.parametrize("prose", ["费用未确认前不买入", "账户现金、费用与总敞口需人工确认", "账户和费用确认后再研究", "没有扣费后的正预期证据", "若账户核对发现现金、可卖数量或费用与假设不符，数量情景作废"])
def test_zero_friction_rejects_stale_fee_prerequisites_in_prose(tmp_path, prose):
    workflow, run, draft = setup_run(tmp_path, cost_mode="zero_friction")
    draft["plans"][0]["sizing"]["scenarios"][0]["estimated_fees"] = 0
    draft["plans"][0]["decision_basis"] = "价格与估值支持持有"
    draft["plans"][0]["unknowns"] = [prose]
    with pytest.raises(DomainError, match="旧措辞"):
        workflow.publish(run["id"], draft)
    draft["plans"][0]["unknowns"] = ["手续费及佣金净收入的经营变化仍需核对"]
    assert workflow.publish(run["id"], draft)["plans"][0]["research_cost_mode"] == "zero_friction"
