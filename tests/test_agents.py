import copy
import pytest
import app.workflow as workflow_module
from app.agents import RoleAgent, agent_config, ROLES
from app.runner import Runner, create_job, OPINION
from app.store import Store, DomainError
from app.workflow import Workflow
from test_standalone import MockModel, _prepared_replay


def test_three_agents_have_distinct_clients_but_share_api_profile(tmp_path):
    store = Store(tmp_path)
    runner = Runner(store)
    agents = [runner.role_agent({"role": role}) for role in ROLES]
    assert len({id(agent.client) for agent in agents}) == 3
    assert {agent.config["api_profile"] for agent in agents} == {"shared-deepseek"}
    assert {agent.client.store.root for agent in agents} == {store.root}
    assert len({agent.config["system_instruction"] for agent in agents}) == 3
    assert len({agent.config["memory_path"] for agent in agents}) == 3
    assert runner.role_agent({"role": "counter"}) is agents[1]


def test_pipeline_routes_each_phase_to_its_own_persistent_agent_session(tmp_path):
    store = Store(tmp_path)
    request = _prepared_replay(store, "agent-routing")
    job = create_job(store, "agent-routing", prepare=request)
    completed = Runner(store, MockModel()).execute(job["id"])
    assert completed["status"] == "completed", completed.get("error")
    run_id = completed["run_id"]
    run = Workflow(store).get_run(run_id)
    counts = {"opportunity": 3, "counter": 3, "judge": 1}
    for role in ROLES:
        root = f"agents/ashare-{role}/sessions/{run_id}"
        session = store.read(f"{root}/session.json")
        assert session["agent_id"] == f"ashare-{role}"
        calls = store.list(f"{root}/calls")
        assert len(calls) == counts[role]
        assert all(call["agent_session_id"] == session["id"] for call in calls)
    assert run["opinions"]["opportunity"]["initial"]["payload"]["agent_id"] == "ashare-opportunity"
    assert run["decision"]["input"]["agent_id"] == "ashare-judge"


def test_config_is_frozen_and_cross_role_context_is_rejected(tmp_path, monkeypatch):
    store = Store(tmp_path)
    workflow = Workflow(store)
    run = workflow.prepare(_prepared_replay(store, "freeze-agent"))
    original = agent_config("opportunity")
    changed = copy.deepcopy(original)
    changed["system_instruction"] = "不同指令"
    monkeypatch.setattr(workflow_module, "agent_config", lambda role: changed)
    context = workflow.context(run["id"], "opportunity")
    assert context["agent"] == original
    with pytest.raises(DomainError, match="其他角色"):
        RoleAgent(store, agent_config("counter"), MockModel()).complete(context, "unused", job_id="job-test", stage="initial")


def test_legacy_review_reuses_saved_session_after_default_config_changes(tmp_path, monkeypatch):
    store = Store(tmp_path)
    run = Workflow(store).prepare(_prepared_replay(store, "legacy-agent"))
    context = Workflow(store).context(run["id"], "counter", "initial")
    original = context.pop("agent")
    model = MockModel()
    original_agent = RoleAgent(store, original, model)
    original_agent.complete(context, OPINION, job_id="before", stage="initial")
    session_path = f"agents/ashare-counter/sessions/{run['id']}/session.json"
    saved = store.read(session_path)
    changed = {**original, "revision": "changed", "system_instruction": "新的默认指令"}
    monkeypatch.setattr('app.runner.agent_config', lambda role: changed)
    runner = Runner(store, model)
    assert runner.role_agent(context).config == original
    runner.role_agent(context).complete(context, OPINION, job_id="after", stage="review")
    assert store.read(session_path) == saved
    assert runner.role_agent({'role': 'counter', 'run_id': 'new-run'}).config == changed
