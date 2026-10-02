import json

import pytest

import app.workflow as workflow_module
from app.store import ConflictError, DomainError, Store
from app.workflow import ALL_ROLES, Workflow
from app.settings import update_settings


DEFAULT_AS_OF = "2026-09-28T15:30:00+08:00"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    current = [DEFAULT_AS_OF]
    monkeypatch.setattr(workflow_module, "now_iso", lambda: current[0])

    def set_time(value: str):
        current[0] = value

    return set_time


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    update_settings(store, {"channels": {"market": {"mode": "manual", "name": "测试指定渠道"}}})
    store.write(
        "evidence/text-1.json",
        {
            "id": "text-1",
            "kind": "text",
            "source_id": "exchange",
            "title": "冻结公告",
            "url": "https://example.invalid/notice",
            "published_at": "2026-09-28T09:00:00+08:00",
            "captured_at": "2026-09-28T09:30:00+08:00",
            "available_at": "2026-09-28T09:30:00+08:00",
            "status": "ready",
            "text": "可核对的公告事实",
            "metadata": {},
        },
    )
    return store, Workflow(store)


def prepare(
    workflow: Workflow,
    request_id: str,
    *,
    as_of: str = DEFAULT_AS_OF,
    mode: str = "live",
):
    return workflow.prepare(
        {
            "request_id": request_id,
            "decision_contract": 1,  # Historical opinion/decision fixtures.
            "as_of": as_of,
            "mode": mode,
            "symbols": ["601398"],
            "evidence_ids": ["text-1"],
        }
    )


def opinion(summary: str):
    return {
        "summary": summary,
        "evidence_ids": ["text-1"],
        "claims": [f"{summary} 的可核对判断"],
        "counterevidence": [f"{summary} 的反向证据"],
        "unknowns": [f"{summary} 的未知项"],
        "changes": [],
    }


def complete_roles(workflow: Workflow, run_id: str, prefix: str = "本轮"):
    workflow.submit_role(run_id, "opportunity", "initial", opinion(f"{prefix}机会初稿"))
    workflow.submit_role(run_id, "counter", "initial", opinion(f"{prefix}反证初稿"))
    workflow.submit_role(run_id, "opportunity", "challenge", opinion(f"{prefix}机会回应"))
    workflow.submit_role(run_id, "counter", "challenge", opinion(f"{prefix}反证回应"))


def decision(*, horizon: int = 2):
    return {
        "summary": "条件式裁决",
        "plans": [
            {
                "symbol": "601398",
                "action": "observe",
                "thesis": "只有触发条件满足才继续考虑",
                "evidence_ids": ["text-1"],
                "counterevidence": ["仍有下行情景"],
                "unknowns": ["风险预算"],
                "trigger": "收盘确认条件成立",
                "invalidation": "公告事实被推翻",
                "upside": "方向上行",
                "downside": "方向下行",
                "costs": "需另计真实费用",
                "horizon_days": horizon,
                "expected_direction": "uncertain",
                "quantity": None,
                "supersedes": None,
            }
        ],
        "reasoning": "综合两份独立初稿和一次交叉回应",
        "model": "test-model",
        "reasoning_effort": "high",
    }


def files_under(store: Store):
    return {
        str(path.relative_to(store.root)): path.read_bytes()
        for path in store.root.rglob("*")
        if path.is_file()
    }


def test_initial_drafts_are_private_until_both_exist_and_projection_is_read_only(setup):
    store, workflow = setup
    run = prepare(workflow, "discussion-visibility")
    run_id = run["id"]

    assert workflow.discussion(run_id)["messages"] == []
    workflow.submit_role(run_id, "opportunity", "initial", opinion("OPPORTUNITY_SECRET"))
    before_read = files_under(store)

    for reader in (None, "counter", "judge"):
        view = workflow.discussion(run_id, reader)
        assert view["messages"] == []
        assert "OPPORTUNITY_SECRET" not in json.dumps(view, ensure_ascii=False)

    author_view = workflow.discussion(run_id, "opportunity")
    assert author_view["stage"] == "initial_in_progress"
    assert author_view["shared_initials"] is False
    assert author_view["delivery"] == "pull"
    assert "实时推送" in author_view["delivery_note"]
    assert len(author_view["messages"]) == 1
    private_message = author_view["messages"][0]
    assert private_message["recipients"] == ["opportunity"]
    assert private_message["payload"] == opinion("OPPORTUNITY_SECRET")
    assert private_message["source_ref"] == (
        f"run_events/{run_id}/opinion-opportunity-initial.json"
    )
    assert set(private_message) == {
        "id", "run_id", "sender", "recipients", "phase", "created_at",
        "reply_to", "evidence_ids", "payload", "source_ref",
    }
    assert files_under(store) == before_read

    workflow.submit_role(run_id, "counter", "initial", opinion("COUNTER_SECRET"))
    shared = workflow.discussion(run_id)
    assert shared["shared_initials"] is True
    assert [item["sender"] for item in shared["messages"]] == ["opportunity", "counter"]
    assert all(item["recipients"] == list(ALL_ROLES) for item in shared["messages"])
    assert shared["messages"][0]["id"] == private_message["id"]


def test_context_enforces_stage_visibility_and_one_challenge_round(setup):
    _, workflow = setup
    run_id = prepare(workflow, "discussion-context")["id"]
    workflow.submit_role(run_id, "opportunity", "initial", opinion("机会私有初稿"))

    own_initial = workflow.context(run_id, "opportunity", "initial")
    assert [item["sender"] for item in own_initial["discussion"]["messages"]] == [
        "opportunity"
    ]
    assert "机会私有初稿" in json.dumps(own_initial, ensure_ascii=False)
    counter_initial = workflow.context(run_id, "counter", "initial")
    assert counter_initial["discussion"]["messages"] == []
    assert "机会私有初稿" not in json.dumps(counter_initial, ensure_ascii=False)
    with pytest.raises(DomainError, match="两份独立初稿"):
        workflow.context(run_id, "opportunity", "challenge")

    workflow.submit_role(run_id, "counter", "initial", opinion("反证私有初稿"))
    challenge_context = workflow.context(run_id, "opportunity", "challenge")
    assert set(challenge_context["initial_opinions"]) == {"opportunity", "counter"}
    assert {item["phase"] for item in challenge_context["discussion"]["messages"]} == {
        "initial"
    }
    with pytest.raises(DomainError, match="两份交叉回应"):
        workflow.context(run_id, "judge", "judge")
    with pytest.raises(DomainError, match="裁决尚未发布"):
        workflow.context(run_id, "judge", "decision")

    first = workflow.submit_role(
        run_id, "opportunity", "challenge", opinion("机会唯一回应")
    )
    assert workflow.submit_role(
        run_id, "opportunity", "challenge", opinion("机会唯一回应")
    ) == first
    with pytest.raises(ConflictError, match="必须新建研究轮次"):
        workflow.submit_role(run_id, "opportunity", "challenge", opinion("机会第二次回应"))
    workflow.submit_role(run_id, "counter", "challenge", opinion("反证唯一回应"))

    judged = workflow.context(run_id, "judge", "judge")
    assert set(judged["challenge_opinions"]) == {"opportunity", "counter"}
    assert {item["phase"] for item in judged["discussion"]["messages"]} == {
        "initial", "challenge"
    }


def test_challenges_reply_to_opponent_and_decision_broadcasts_to_all_prior_messages(setup):
    _, workflow = setup
    run_id = prepare(workflow, "discussion-replies")["id"]
    complete_roles(workflow, run_id)

    before = workflow.discussion(run_id)
    by_key = {(item["sender"], item["phase"]): item for item in before["messages"]}
    assert by_key[("opportunity", "challenge")]["reply_to"] == [
        by_key[("counter", "initial")]["id"]
    ]
    assert by_key[("counter", "challenge")]["reply_to"] == [
        by_key[("opportunity", "initial")]["id"]
    ]

    published = workflow.publish(run_id, decision())
    after = workflow.discussion(run_id)
    judge_message = after["messages"][-1]
    assert after["stage"] == "published"
    assert judge_message["sender"] == "judge"
    assert judge_message["phase"] == "decision"
    assert judge_message["recipients"] == list(ALL_ROLES)
    assert judge_message["reply_to"] == [item["id"] for item in before["messages"]]
    assert judge_message["source_ref"] == f"run_events/{run_id}/decision.json"
    assert judge_message["evidence_ids"] == ["text-1"]
    assert judge_message["payload"] == {
        key: value
        for key, value in published.items()
        if key not in {"input", "run_id", "published_at"}
    }
    decision_context = workflow.context(run_id, "counter", "decision")
    assert decision_context["decision"] == judge_message["payload"]
    assert decision_context["discussion"]["messages"][-1] == judge_message
    assert not ({"pushed", "read", "accepted", "free_chat"} & set(judge_message))


def test_role_history_is_frozen_role_scoped_mode_scoped_and_reloadable(setup, clock):
    store, workflow = setup

    clock("2026-09-28T11:30:00+08:00")
    full_id = prepare(
        workflow, "history-full", as_of="2026-09-28T11:00:00+08:00"
    )["id"]
    complete_roles(workflow, full_id, "历史完整轮")
    workflow.publish(full_id, decision(horizon=2))

    clock("2026-09-28T12:30:00+08:00")
    partial_id = prepare(
        workflow, "history-partial", as_of="2026-09-28T12:00:00+08:00"
    )["id"]
    workflow.submit_role(partial_id, "opportunity", "initial", opinion("历史未完机会初稿"))

    clock("2026-09-28T13:30:00+08:00")
    replay_id = prepare(
        workflow,
        "history-replay",
        as_of="2026-09-28T13:00:00+08:00",
        mode="replay",
    )["id"]
    complete_roles(workflow, replay_id, "回放轮")
    workflow.publish(replay_id, decision(horizon=2))

    store.write(
        "roles/opportunity/memory-reflection.json",
        {
            "id": "memory-reflection",
            "role": "opportunity",
            "run_id": full_id,
            "review_id": "review-history",
            "mode": "live",
            "created_at": "2026-09-28T13:10:00+08:00",
            "known_at": "2026-09-28T13:10:00+08:00",
            "outcome_as_of": "2026-09-28T13:00:00+08:00",
            "opinion_refs": [
                f"run_events/{full_id}/opinion-opportunity-initial.json",
                f"run_events/{full_id}/opinion-opportunity-challenge.json",
            ],
            "decision_ref": f"run_events/{full_id}/decision.json",
            "kind": "outcome_with_reflection",
            "facts": [],
            "fact_source_ids": [],
            "reflection": {"text": "下轮降低对单一公告的权重", "source": "review_payload"},
        },
    )

    clock("2026-09-28T14:30:00+08:00")
    current_id = prepare(
        workflow, "history-current", as_of="2026-09-28T14:00:00+08:00"
    )["id"]
    initial_context = workflow.context(current_id, "opportunity", "initial")
    frozen_history = initial_context["history"]
    assert [item["run_id"] for item in frozen_history] == [full_id, partial_id]
    assert replay_id not in {item["run_id"] for item in frozen_history}
    assert [item["phase"] for item in frozen_history[0]["opinions"]] == [
        "initial", "challenge"
    ]
    assert all(item["role"] == "opportunity" for item in frozen_history[0]["opinions"])
    assert frozen_history[0]["decision"] is not None
    assert [item["phase"] for item in frozen_history[1]["opinions"]] == ["initial"]
    assert frozen_history[1]["decision"] is None
    assert initial_context["memory"][0]["reflection"]["text"] == (
        "下轮降低对单一公告的权重"
    )

    complete_roles(workflow, current_id, "当前轮")
    judge_history = workflow.context(current_id, "judge", "judge")["history"]
    assert [item["run_id"] for item in judge_history] == [full_id]
    assert judge_history[0]["opinions"] == []
    assert judge_history[0]["decision"] is not None

    clock("2026-09-28T15:30:00+08:00")
    store.write(
        "evidence/later.json",
        {
            "id": "later",
            "kind": "text",
            "source_id": "later",
            "title": "后来资料",
            "url": None,
            "published_at": "2026-09-28T15:00:00+08:00",
            "captured_at": "2026-09-28T15:00:00+08:00",
            "available_at": "2026-09-28T15:00:00+08:00",
            "status": "ready",
            "text": "冻结之后的新事实",
            "metadata": {},
        },
    )
    workflow.submit_role(partial_id, "counter", "initial", opinion("后来反证初稿"))
    workflow.submit_role(partial_id, "opportunity", "challenge", opinion("后来机会回应"))
    workflow.submit_role(partial_id, "counter", "challenge", opinion("后来反证回应"))
    workflow.publish(partial_id, decision(horizon=3))

    reloaded = Workflow(store).context(current_id, "opportunity", "initial")
    assert reloaded["history"] == frozen_history
    assert "后来" not in json.dumps(reloaded["history"], ensure_ascii=False)
    assert reloaded["memory"] == initial_context["memory"]


def test_legacy_run_without_history_refs_remains_readable(setup):
    store, workflow = setup
    run_id = prepare(workflow, "legacy-history")["id"]
    raw = store.read(f"runs/{run_id}.json")
    for snapshot in raw["role_snapshots"].values():
        snapshot.pop("history_refs", None)
    store.write(f"runs/{run_id}.json", raw, overwrite=True)

    reloaded = Workflow(store)
    for role in ("opportunity", "counter"):
        assert reloaded.context(run_id, role, "initial")["history"] == []
    complete_roles(reloaded, run_id, "旧轮")
    assert reloaded.context(run_id, "judge", "judge")["history"] == []
    assert all(
        snapshot.get("history_refs") is None
        for snapshot in raw["role_snapshots"].values()
    )
