import json
from pathlib import Path

import pytest

import app.workflow as workflow_module
from app.store import ConflictError, DomainError, Store
from app.workflow import Workflow
from app.settings import update_settings


RUN_AS_OF = "2026-09-28T15:30:00+08:00"


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """All workflow times are explicit simulation; no test advances real data."""
    current = [RUN_AS_OF]
    monkeypatch.setattr(workflow_module, "now_iso", lambda: current[0])

    def set_time(value):
        current[0] = value

    return set_time


def put_market(
    store: Store,
    evidence_id: str,
    *,
    available_at: str = "2026-09-28T15:20:00+08:00",
    quote_price=10,
    quote_as_of: str = "2026-09-28T15:00:00+08:00",
    quote_phase: str = "close",
    stale: bool = False,
    rows=None,
):
    update_settings(store, {"channels": {"market": {"mode": "tencent", "name": "腾讯行情测试替身"}}})
    rows = rows if rows is not None else [
        {"date": "2026-09-28", "open": 9.8, "close": 10, "high": 10.1, "low": 9.7, "volume": 1000}
    ]
    item = {
        "id": evidence_id,
        "kind": "market",
        "source_id": "tencent",
        "title": "601398 原始行情",
        "url": "https://example.invalid/market",
        "published_at": None,
        "captured_at": available_at,
        "available_at": available_at,
        "status": "ready",
        "text": "公开行情快照",
        "metadata": {
            "quotes": [
                {
                    "symbol": "601398",
                    "name": "工商银行",
                    "price": quote_price,
                    "previous_close": 9.9,
                    "open": 9.8,
                    "high": 10.1,
                    "low": 9.7,
                    "volume": 1000,
                    "amount": 10000,
                    "volume_unit": "shares",
                    "amount_unit": "CNY",
                    "price_as_of": quote_as_of,
                    "price_source": "https://example.invalid/market",
                    "data_timing": "closing_snapshot" if quote_phase in {"closed", "close"} else "delayed",
                    "as_of": quote_as_of,
                    "trade_date": quote_as_of[:10],
                    "phase": quote_phase,
                    "stale": stale,
                }
            ],
            "daily": {"601398": {"adjustment": "none", "rows": rows}},
        },
    }
    matching = next((r for r in rows if r['date'] == quote_as_of[:10]), None)
    if matching and all(k in matching for k in ('open','high','low')):
        q = item['metadata']['quotes'][0]
        q.update(open=matching['open'], high=max(matching['high'], quote_price), low=min(matching['low'], quote_price))
    store.write(f"evidence/{evidence_id}.json", item)
    return item


def put_text(store: Store, evidence_id="text-1", available_at="2026-09-28T12:00:00+08:00"):
    item = {
        "id": evidence_id,
        "kind": "text",
        "source_id": "exchange",
        "title": "公告",
        "url": "https://example.invalid/notice",
        "published_at": "2026-09-28T10:00:00+08:00",
        "captured_at": available_at,
        "available_at": available_at,
        "status": "ready",
        "text": "测试公告事实",
        "metadata": {},
    }
    store.write(f"evidence/{evidence_id}.json", item)
    return item


def prepare(workflow: Workflow, request_id: str, evidence_ids=None, **overrides):
    payload = {
        "request_id": request_id,
        "decision_contract": 1,  # Legacy fixtures exercise backwards compatibility.
        "as_of": RUN_AS_OF,
        "mode": "live",
        "symbols": ["601398"],
        "evidence_ids": evidence_ids or ["market-1", "text-1"],
    }
    payload.update(overrides)
    return workflow.prepare(payload)


def opinion(summary: str, evidence_ids=None):
    return {
        "summary": summary,
        "evidence_ids": evidence_ids or ["market-1", "text-1"],
        "claims": ["一项可核对判断"],
        "counterevidence": ["一项反向证据"],
        "unknowns": ["一项未知"],
        "changes": [],
        "forecasts": [{"symbol": "601398", "horizon_days": h, "expected_direction": "up",
                       "rationale": "测试假说", "falsifier": "测试反证"} for h in (1, 5, 20)],
    }


def complete_roles(workflow: Workflow, run_id: str):
    workflow.submit_role(run_id, "opportunity", "initial", opinion("机会独立初稿"))
    workflow.submit_role(run_id, "counter", "initial", opinion("反证独立初稿"))
    workflow.submit_role(run_id, "opportunity", "challenge", opinion("机会交叉回应"))
    workflow.submit_role(run_id, "counter", "challenge", opinion("反证交叉回应"))


def decision(horizon=2, *, supersedes=None, quantity=None):
    return {
        "summary": "条件式裁决",
        "plans": [
            {
                "symbol": "601398",
                "action": "observe",
                "thesis": "只有触发条件满足才继续考虑",
                "evidence_ids": ["market-1", "text-1"],
                "counterevidence": ["仍有下行情景"],
                "unknowns": ["风险预算"],
                "trigger": "收盘确认条件成立",
                "invalidation": "公告事实被推翻",
                "upside": "方向上行",
                "downside": "方向下行",
                "costs": "需另计真实费用",
                "horizon_days": horizon,
                "expected_direction": "up",
                "quantity": quantity,
                "supersedes": supersedes,
            }
        ],
        "reasoning": "综合独立初稿和一次交叉回应",
        "model": "test-model",
        "reasoning_effort": "high",
    }


@pytest.fixture
def setup(tmp_path):
    store = Store(tmp_path)
    put_market(store, "market-1")
    put_text(store)
    return store, Workflow(store)


def test_prepare_rejects_future_availability_and_future_market_rows(tmp_path):
    store = Store(tmp_path)
    put_text(store, "future-text", "2026-09-28T16:00:00+08:00")
    workflow = Workflow(store)
    with pytest.raises(DomainError, match="尚不可知"):
        prepare(workflow, "future-available", ["future-text"])

    put_market(
        store,
        "future-row",
        rows=[
            {"date": "2026-09-29", "open": 10, "close": 11, "high": 11, "low": 10, "volume": 1}
        ],
    )
    with pytest.raises(DomainError, match="尚不可知的日线"):
        prepare(workflow, "future-row", ["future-row"])


def test_prepare_rejects_future_account_snapshot_and_excludes_future_memory(setup):
    store, workflow = setup
    with pytest.raises(DomainError, match="账户快照时间不能晚于"):
        prepare(
            workflow,
            "future-account",
            account_snapshot={
                "confirmed": True,
                "risk_budget": 100,
                "as_of": "2026-09-29T09:00:00+08:00",
            },
        )
    store.write(
        "roles/opportunity/future-memory.json",
        {
            "id": "future-memory",
            "role": "opportunity",
            "run_id": "run-future",
            "review_id": "review-future",
            "known_at": "2026-09-29T09:00:00+08:00",
            "kind": "fact_receipt",
            "facts": [],
            "fact_source_ids": [],
        },
    )
    run = prepare(workflow, "memory-cutoff")
    assert workflow.context(run["id"], "opportunity", "initial")["memory"] == []


def test_initial_context_isolates_other_role_draft(setup):
    _, workflow = setup
    run = prepare(workflow, "isolated-initial")
    workflow.submit_role(run["id"], "opportunity", "initial", opinion("OPPORTUNITY_SECRET"))
    counter_context = workflow.context(run["id"], "counter", "initial")
    assert "OPPORTUNITY_SECRET" not in json.dumps(counter_context, ensure_ascii=False)
    with pytest.raises(DomainError, match="两份独立初稿"):
        workflow.context(run["id"], "counter", "challenge")


def test_role_phase_is_append_only_and_idempotent(setup):
    _, workflow = setup
    run = prepare(workflow, "append-only")
    first = workflow.submit_role(run["id"], "opportunity", "initial", opinion("初稿 A"))
    assert workflow.submit_role(run["id"], "opportunity", "initial", opinion("初稿 A")) == first
    with pytest.raises(ConflictError, match="必须新建研究轮次"):
        workflow.submit_role(run["id"], "opportunity", "initial", opinion("初稿 B"))


def test_publish_requires_all_four_role_phases(setup):
    _, workflow = setup
    run = prepare(workflow, "missing-phase")
    workflow.submit_role(run["id"], "opportunity", "initial", opinion("机会初稿"))
    workflow.submit_role(run["id"], "counter", "initial", opinion("反证初稿"))
    workflow.submit_role(run["id"], "opportunity", "challenge", opinion("机会回应"))
    with pytest.raises(DomainError, match="counter.challenge"):
        workflow.publish(run["id"], decision())


def test_unknown_risk_budget_forces_null_quantity(setup):
    _, workflow = setup
    run = prepare(workflow, "quantity-block")
    complete_roles(workflow, run["id"])
    with pytest.raises(DomainError, match="quantity 必须为 null"):
        workflow.publish(run["id"], decision(quantity=100))
    published = workflow.publish(run["id"], decision(quantity=None))
    assert published["plans"][0]["quantity"] is None


def test_baseline_skips_stale_quote_and_rejects_closed_quote_daily_conflict(tmp_path):
    stale_store = Store(tmp_path / "stale")
    put_market(stale_store, "market-1", quote_price=99, stale=True)
    put_text(stale_store)
    stale_workflow = Workflow(stale_store)
    stale_run = prepare(stale_workflow, "stale-baseline")
    complete_roles(stale_workflow, stale_run["id"])
    stale_plan = stale_workflow.publish(stale_run["id"], decision())["plans"][0]
    assert stale_plan["baseline_price"] == 10
    assert stale_plan["baseline_at"] == "2026-09-28"

    conflict_store = Store(tmp_path / "conflict")
    put_market(conflict_store, "market-1", quote_price=11, quote_phase="closed")
    put_text(conflict_store)
    conflict_workflow = Workflow(conflict_store)
    conflict_run = prepare(conflict_workflow, "conflict-baseline")
    complete_roles(conflict_workflow, conflict_run["id"])
    with pytest.raises(DomainError, match="已收盘报价与未复权日线"):
        conflict_workflow.publish(conflict_run["id"], decision())


def test_unconfirmed_account_forces_null_quantity_even_with_budget(setup):
    _, workflow = setup
    run = prepare(
        workflow,
        "unconfirmed-quantity",
        account_snapshot={"confirmed": False, "risk_budget": 500},
    )
    complete_roles(workflow, run["id"])
    with pytest.raises(DomainError, match="账户未确认"):
        workflow.publish(run["id"], decision(quantity=100))
    assert workflow.publish(run["id"], decision(quantity=None))["plans"][0]["quantity"] is None


def test_revision_is_scoped_by_symbol_and_horizon_and_preserves_old_file(setup):
    store, workflow = setup
    first = prepare(workflow, "revision-first")
    complete_roles(workflow, first["id"])
    workflow.publish(first["id"], decision(horizon=2))
    old_path = store.path(f"run_events/{first['id']}/decision.json")
    old_bytes = old_path.read_bytes()

    other_horizon = prepare(workflow, "revision-other-horizon")
    complete_roles(workflow, other_horizon["id"])
    workflow.publish(other_horizon["id"], decision(horizon=10))

    revision = prepare(workflow, "revision-same-horizon")
    complete_roles(workflow, revision["id"])
    with pytest.raises(DomainError, match="supersedes 必须"):
        workflow.publish(revision["id"], decision(horizon=2))
    workflow.publish(revision["id"], decision(horizon=2, supersedes=first["id"]))

    assert old_path.read_bytes() == old_bytes
    old_view = workflow.get_run(first["id"])["decision"]["plans"][0]
    other_view = workflow.get_run(other_horizon["id"])["decision"]["plans"][0]
    assert old_view["superseded_by"] == revision["id"]
    assert old_view["active"] is False
    assert old_view["lifecycle_status"] == "superseded"
    assert other_view["superseded_by"] is None
    assert other_view["active"] is True
    assert other_view["lifecycle_status"] == "active"


def test_plan_view_expires_only_after_last_review_date(setup, monkeypatch):
    _, workflow = setup
    run = prepare(workflow, "expired-view")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision(horizon=2))

    monkeypatch.setattr(workflow_module, "now_iso", lambda: "2026-10-13T16:00:00+08:00")
    plan = workflow.get_run(run["id"])["decision"]["plans"][0]
    assert max(point["review_date"] for point in plan["review_points"]) == "2026-10-12"
    assert plan["expired"] is True
    assert plan["active"] is False
    assert plan["lifecycle_status"] == "expired"

    later = prepare(
        workflow,
        "after-expiry",
        as_of="2026-10-13T16:00:00+08:00",
    )
    assert workflow.context(later["id"], "opportunity", "initial")["pending_plans"] == []
    complete_roles(workflow, later["id"])
    replacement_without_supersedes = workflow.publish(later["id"], decision(horizon=2))
    assert replacement_without_supersedes["plans"][0]["supersedes"] is None


def test_review_uses_exact_raw_date_and_pending_is_not_memory(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "pending-review")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision(horizon=2))

    put_market(
        store,
        "review-only-sep30",
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        quote_price=11,
        rows=[
            {"date": "2026-09-30", "open": 10.5, "close": 11, "high": 11.1, "low": 10.4, "volume": 2000}
        ],
    )
    clock("2026-09-30T16:30:00+08:00")
    review = workflow.review(
        run["id"],
        {
            "request_id": "review-exact-date",
            "as_of": "2026-09-30T16:30:00+08:00",
            "evidence_ids": ["review-only-sep30"],
            "reflections": {},
        },
    )
    by_date = {item["review_date"]: item for item in review["observations"]}
    assert by_date["2026-09-29"]["status"] == "pending"
    assert by_date["2026-09-29"]["result_price"] is None
    assert by_date["2026-09-30"]["status"] == "completed"
    for role in ("opportunity", "counter", "judge"):
        memory = store.list(f"roles/{role}")
        assert len(memory) == 1
        assert memory[0]["kind"] == "fact_receipt"
        assert "reflection" not in memory[0]
        assert all(item["status"] == "completed" for item in memory[0]["facts"])
        mirror = store.path(f"roles/{role}/{memory[0]['id']}.md").read_text(encoding="utf-8")
        assert "唯一权威结构记录" in mirror
        assert run["id"] in mirror and review["id"] in mirror
        assert "本条没有角色反思，只记录" in mirror


def test_weekend_review_dates_and_intraday_daily_stays_pending(tmp_path):
    store = Store(tmp_path)
    put_market(
        store,
        "market-1",
        available_at="2026-09-27T12:00:00+08:00",
        quote_as_of="2026-09-24T16:00:00+08:00",
        quote_phase="closed",
        rows=[
            {"date": "2026-09-24", "open": 9.8, "close": 10, "high": 10.1, "low": 9.7, "volume": 1000}
        ],
    )
    put_text(store, available_at="2026-09-27T12:00:00+08:00")
    workflow = Workflow(store)
    run = prepare(workflow, "weekend-base", as_of="2026-09-27T13:00:00+08:00")
    complete_roles(workflow, run["id"])
    published = workflow.publish(run["id"], decision(horizon=5))
    assert [point["review_date"] for point in published["plans"][0]["review_points"]] == [
        "2026-09-28",
        "2026-10-09",
    ]

    put_market(
        store,
        "intraday-review",
        available_at="2026-09-28T10:30:00+08:00",
        quote_as_of="2026-09-28T10:30:00+08:00",
        quote_phase="trading",
        quote_price=10.5,
        rows=[
            {"date": "2026-09-28", "open": 10, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 500}
        ],
    )
    review = workflow.review(
        run["id"],
        {
            "request_id": "intraday-must-wait",
            "as_of": "2026-09-28T10:30:00+08:00",
            "evidence_ids": ["intraday-review"],
        },
    )
    first_point = next(item for item in review["observations"] if item["review_date"] == "2026-09-28")
    assert first_point["status"] == "pending"
    assert "盘中日线不能提前结算" in first_point["pending_reason"]
    assert all(store.list(f"roles/{role}") == [] for role in ("opportunity", "counter", "judge"))


def test_not_due_review_creates_no_role_knowledge(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "not-due")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision(horizon=2))
    clock("2026-09-28T16:00:00+08:00")
    review = workflow.review(
        run["id"],
        {
            "request_id": "review-too-early",
            "as_of": "2026-09-28T16:00:00+08:00",
            "evidence_ids": ["market-1"],
        },
    )
    assert review["observations"]
    assert {item["status"] for item in review["observations"]} == {"pending"}
    assert all(store.list(f"roles/{role}") == [] for role in ("opportunity", "counter", "judge"))
    assert store.list("source_results") == []


def test_duplicate_review_does_not_duplicate_scoring_or_memory(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "dedupe-review")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision(horizon=2))
    put_market(
        store,
        "review-market",
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        quote_price=11,
        rows=[
            {"date": "2026-09-29", "open": 10, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 1000},
            {"date": "2026-09-30", "open": 10.5, "close": 11, "high": 11.1, "low": 10.4, "volume": 2000},
        ],
    )
    payload = {
        "request_id": "dedupe-review-request",
        "as_of": "2026-09-30T16:30:00+08:00",
        "evidence_ids": ["review-market"],
        "reflections": {"opportunity": "结果支持当时方向，但不能证明来源有因果作用。"},
    }
    clock("2026-09-30T16:30:00+08:00")
    first = workflow.review(run["id"], payload)
    assert workflow.review(run["id"], payload) == first
    opportunity_memory = store.list("roles/opportunity")[0]
    opportunity_mirror = store.path(
        f"roles/opportunity/{opportunity_memory['id']}.md"
    ).read_text(encoding="utf-8")
    assert "来源：`review_payload`" in opportunity_mirror
    assert "不能证明来源有因果作用" in opportunity_mirror
    memory_counts = {role: len(store.list(f"roles/{role}")) for role in ("opportunity", "counter", "judge")}
    source_results_before = list(store.path("source_results").rglob("*.json"))

    second = workflow.review(
        run["id"],
        {
            **payload,
            "request_id": "dedupe-review-second-request",
            "reflections": {},
        },
    )
    assert second["observations"]
    assert {item["status"] for item in second["observations"]} == {"pending"}
    assert len(second["already_completed"]) == 2
    assert memory_counts == {
        role: len(store.list(f"roles/{role}")) for role in ("opportunity", "counter", "judge")
    }
    assert source_results_before == list(store.path("source_results").rglob("*.json"))


def test_new_store_reloads_same_role_memory_into_later_context(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "memory-first")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision(horizon=2))
    put_market(
        store,
        "memory-review",
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        quote_price=11,
        rows=[
            {"date": "2026-09-29", "open": 10, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 1000},
            {"date": "2026-09-30", "open": 10.5, "close": 11, "high": 11.1, "low": 10.4, "volume": 2000},
        ],
    )
    clock("2026-09-30T16:30:00+08:00")
    workflow.review(
        run["id"],
        {
            "request_id": "memory-review",
            "as_of": "2026-09-30T16:30:00+08:00",
            "evidence_ids": ["memory-review"],
            "reflections": {"counter": "当时的下行情景在两个到期点尚未发生。"},
        },
    )

    put_text(store, "text-later", "2026-10-08T12:00:00+08:00")
    reloaded = Workflow(Store(store.root))
    clock("2026-10-08T16:00:00+08:00")
    later = prepare(
        reloaded,
        "memory-later",
        ["text-later"],
        as_of="2026-10-08T16:00:00+08:00",
    )
    counter_context = reloaded.context(later["id"], "counter", "initial")
    opportunity_context = reloaded.context(later["id"], "opportunity", "initial")
    assert len(counter_context["memory"]) == 1
    assert counter_context["memory"][0]["reflection"]["text"].startswith("当时的下行情景")
    assert "reflection" not in opportunity_context["memory"][0]
    assert counter_context["pending_plans"]


def put_outcome_market(store, evidence_id="outcome-market"):
    return put_market(
        store,
        evidence_id,
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        quote_price=11,
        rows=[
            {"date": "2026-09-29", "open": 10, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 1000},
            {"date": "2026-09-30", "open": 10.5, "close": 11, "high": 11.1, "low": 10.4, "volume": 2000},
        ],
    )


def test_saved_fresh_flag_does_not_make_old_quote_a_current_baseline(tmp_path):
    store = Store(tmp_path)
    put_market(
        store, "market-1",
        available_at="2026-09-24T16:20:00+08:00",
        quote_as_of="2026-09-24T16:00:00+08:00",
        rows=[{"date": "2026-09-24", "close": 10}],
    )
    put_text(store)
    workflow = Workflow(store)
    run = prepare(workflow, "saved-quote-age")
    assert run["evidence"][0]["metadata"]["quotes"][0]["stale"] is False
    complete_roles(workflow, run["id"])
    plan = workflow.publish(run["id"], decision())["plans"][0]
    assert plan["baseline_price"] is None
    assert plan["baseline_at"] is None
    assert plan["baseline_source_ids"] == []


@pytest.mark.parametrize("quote_state,expected_price", [("fresh", 10.2), ("stale", None), ("missing", None)])
def test_intraday_day_close_never_replaces_realtime_baseline(tmp_path, quote_state, expected_price):
    store = Store(tmp_path)
    item = put_market(
        store, "market-1",
        available_at="2026-09-28T10:30:00+08:00",
        quote_as_of="2026-09-28T10:30:00+08:00",
        quote_price=10.2, quote_phase="trading", stale=quote_state == "stale",
        rows=[{"date": "2026-09-28", "close": 11}],
    )
    if quote_state == "missing":
        item["metadata"]["quotes"] = []
        store.write("evidence/market-1.json", item, overwrite=True)
    put_text(store, available_at="2026-09-28T10:00:00+08:00")
    workflow = Workflow(store)
    run = prepare(workflow, "intraday-base", as_of="2026-09-28T10:30:00+08:00")
    complete_roles(workflow, run["id"])
    plan = workflow.publish(run["id"], decision())["plans"][0]
    assert plan["baseline_price"] == expected_price


def test_intraday_candle_does_not_become_closed_by_waiting(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "unclosed-snapshot")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    put_market(
        store, "intraday-outcome",
        available_at="2026-09-29T10:30:00+08:00",
        quote_as_of="2026-09-29T10:30:00+08:00",
        quote_price=11, quote_phase="trading",
        rows=[{"date": "2026-09-29", "close": 11}],
    )
    clock("2026-09-30T16:30:00+08:00")
    result = workflow.review(run["id"], {
        "request_id": "late-use-of-intraday",
        "as_of": "2026-09-30T16:30:00+08:00",
        "evidence_ids": ["intraday-outcome"],
    })
    assert all(item["status"] == "pending" for item in result["observations"])
    assert store.list("roles/opportunity") == []


def test_live_prepare_and_review_reject_future_time_without_writes(setup):
    store, workflow = setup
    with pytest.raises(DomainError, match="live.*冻结时点不能晚于当前时间"):
        prepare(workflow, "live-future", as_of="2026-09-28T15:30:01+08:00")
    assert store.list("runs") == []
    run = prepare(workflow, "live-now")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    with pytest.raises(DomainError, match="live.*复核时点不能晚于当前时间"):
        workflow.review(run["id"], {
            "request_id": "future-review", "as_of": "2026-09-30T16:30:00+08:00",
            "evidence_ids": ["market-1"],
        })
    assert store.list(f"run_events/{run['id']}/reviews") == []
    assert store.list("roles/opportunity") == []


@pytest.mark.parametrize("mode,created_at,expected_known", [
    ("live", "2026-10-08T16:00:00+08:00", "2026-10-08T16:00:00+08:00"),
    ("replay", RUN_AS_OF, "2026-09-30T16:30:00+08:00"),
])
def test_reflection_known_time_never_backfills_and_keeps_original_refs(
    setup, clock, mode, created_at, expected_known
):
    store, workflow = setup
    run = prepare(workflow, "reflection-original", mode=mode)
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    immutable_paths = [store.path(f"runs/{run['id']}.json"), store.path(f"run_events/{run['id']}/decision.json")]
    original_bytes = [path.read_bytes() for path in immutable_paths]
    put_outcome_market(store)
    clock(created_at)
    request = {
        "request_id": "backdated-reflection", "as_of": "2026-09-30T16:30:00+08:00",
        "evidence_ids": ["outcome-market"], "reflections": {"counter": "这是后来才形成的反思。"},
    }
    review = workflow.review(run["id"], request)
    memory = store.list("roles/counter")[0]
    assert memory["mode"] == mode
    assert memory["known_at"] == expected_known
    assert memory["created_at"] == created_at
    assert memory["outcome_as_of"] == request["as_of"]
    assert {item["outcome_at"] for item in memory["facts"]} == {
        "2026-09-29T15:00:00+08:00", "2026-09-30T15:00:00+08:00"
    }
    assert all(store.read(ref) is not None for ref in memory["opinion_refs"])
    assert store.read(memory["decision_ref"])["run_id"] == run["id"]
    assert "payload" not in memory and "decision" not in memory
    mirror = store.path(f"roles/counter/{memory['id']}.md").read_text(encoding="utf-8")
    assert expected_known in mirror and "不代表反思当时已形成" in mirror

    # Same review request stays immutable even after wall time advances.
    clock("2026-10-08T17:00:00+08:00")
    assert workflow.review(run["id"], request) == review
    assert store.list("roles/counter") == [memory]
    reloaded = Workflow(Store(store.root))
    before_cutoff = "2026-10-08T15:59:59+08:00" if mode == "live" else "2026-09-30T16:29:59+08:00"
    before = prepare(reloaded, "before-reflection", mode=mode, as_of=before_cutoff)
    assert reloaded.context(before["id"], "counter")["memory"] == []
    after = prepare(reloaded, "after-reflection", mode=mode, as_of=expected_known)
    assert reloaded.context(after["id"], "counter")["memory"] == [memory]
    assert "reflection" not in reloaded.context(after["id"], "opportunity")["memory"][0]
    assert original_bytes == [path.read_bytes() for path in immutable_paths]


@pytest.mark.parametrize("first_mode,other_mode", [("replay", "live"), ("live", "replay")])
def test_modes_isolate_active_plans_and_supersedes(setup, first_mode, other_mode):
    store, workflow = setup
    first = prepare(workflow, "mode-first", mode=first_mode)
    complete_roles(workflow, first["id"])
    workflow.publish(first["id"], decision())
    other = prepare(workflow, "mode-other", mode=other_mode)
    assert workflow.context(other["id"], "counter")["pending_plans"] == []
    complete_roles(workflow, other["id"])
    with pytest.raises(DomainError, match="没有可替代"):
        workflow.publish(other["id"], decision(supersedes=first["id"]))
    workflow.publish(other["id"], decision())
    other_bytes = store.path(f"run_events/{other['id']}/decision.json").read_bytes()

    revision = prepare(workflow, "mode-revision", mode=first_mode)
    pending = workflow.context(revision["id"], "counter")["pending_plans"]
    assert [item["run_id"] for item in pending] == [first["id"]]
    complete_roles(workflow, revision["id"])
    with pytest.raises(DomainError, match="supersedes 必须"):
        workflow.publish(revision["id"], decision(supersedes=other["id"]))
    workflow.publish(revision["id"], decision(supersedes=first["id"]))
    assert workflow.get_run(first["id"])["decision"]["plans"][0]["superseded_by"] == revision["id"]
    assert workflow.get_run(other["id"])["decision"]["plans"][0]["active"] is True
    assert workflow.get_run(other["id"])["decision"]["plans"][0]["superseded_by"] is None
    assert store.path(f"run_events/{other['id']}/decision.json").read_bytes() == other_bytes


@pytest.mark.parametrize("first_mode,other_mode", [("replay", "live"), ("live", "replay")])
def test_modes_isolate_actual_generated_memories_and_reload(setup, clock, first_mode, other_mode):
    store, workflow = setup
    run = prepare(workflow, "mode-memory-source", mode=first_mode)
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    put_outcome_market(store)
    clock("2026-09-30T16:30:00+08:00")
    workflow.review(run["id"], {
        "request_id": "mode-memory-review", "evidence_ids": ["outcome-market"],
        "reflections": {"opportunity": "仅供本模式的方向核对。"},
    })
    memory = store.list("roles/opportunity")[0]
    assert memory["mode"] == first_mode
    clock("2026-10-08T16:00:00+08:00")
    reloaded = Workflow(Store(store.root))
    same = prepare(reloaded, "mode-memory-same", mode=first_mode, as_of="2026-10-08T16:00:00+08:00")
    other = prepare(reloaded, "mode-memory-other", mode=other_mode, as_of="2026-10-08T16:00:00+08:00")
    assert reloaded.context(same["id"], "opportunity")["memory"] == [memory]
    other_context = reloaded.context(other["id"], "opportunity")
    assert other_context["memory"] == [] and other_context["pending_plans"] == []


def test_legacy_memory_without_mode_defaults_to_live_and_obeys_created_time(setup, clock):
    store, workflow = setup
    memory = {
        "id": "legacy-live", "role": "counter", "known_at": RUN_AS_OF,
        "created_at": "2026-09-29T16:00:00+08:00", "facts": [], "kind": "fact_receipt",
    }
    store.write("roles/counter/legacy-live.json", memory)
    early = prepare(workflow, "legacy-before-created")
    assert workflow.context(early["id"], "counter")["memory"] == []
    clock("2026-09-29T16:00:00+08:00")
    live = prepare(workflow, "legacy-live-later", as_of="2026-09-29T16:00:00+08:00")
    replay = prepare(workflow, "legacy-replay-later", mode="replay", as_of="2026-09-29T16:00:00+08:00")
    assert workflow.context(live["id"], "counter")["memory"] == [memory]
    assert workflow.context(replay["id"], "counter")["memory"] == []
    assert store.read("roles/counter/legacy-live.json") == memory


@pytest.mark.parametrize("second_horizon", [2, 5])
def test_duplicate_symbol_rejected_even_with_distinct_horizon(setup, second_horizon):
    store, workflow = setup
    run = prepare(workflow, "duplicate-symbol")
    complete_roles(workflow, run["id"])
    payload = decision()
    payload["plans"].extend(decision(horizon=second_horizon)["plans"])
    payload["plans"][1]["expected_direction"] = "down"
    with pytest.raises(DomainError, match="相同标的.*一个 horizon"):
        workflow.publish(run["id"], payload)
    assert store.read(f"run_events/{run['id']}/decision.json") is None
    published = workflow.publish(run["id"], decision())
    assert {label for point in published["plans"][0]["review_points"] for label in point["labels"]} == {
        "next", "day5", "horizon"
    }


def test_opinion_keeps_model_identity_across_reload_and_conflicts(setup):
    store, workflow = setup
    run = prepare(workflow, "opinion-identity")
    payload = {
        **opinion("独立意见"), "model": "test-sol", "reasoning_effort": "high", "agent_id": "agent-opportunity-1"
    }
    event = workflow.submit_role(run["id"], "opportunity", "initial", payload)
    assert event["payload"] == payload
    reloaded = Workflow(Store(store.root))
    assert reloaded.submit_role(run["id"], "opportunity", "initial", payload) == event
    with pytest.raises(ConflictError):
        reloaded.submit_role(run["id"], "opportunity", "initial", {**payload, "agent_id": "someone-else"})
    reloaded.submit_role(run["id"], "counter", "initial", opinion("反证初稿"))
    assert reloaded.context(run["id"], "counter", "challenge")["initial_opinions"]["opportunity"]["payload"] == payload


@pytest.mark.parametrize("result_price,status,direction_hit", [
    (11, "completed", True), (9, "completed", False),
    (10 / 3, "pending", None), (16, "pending", None),
])
def test_review_price_discontinuities_stay_pending_without_false_losses(
    setup, clock, result_price, status, direction_hit
):
    store, workflow = setup
    run = prepare(workflow, "price-action-risk")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    put_market(
        store, "price-action-evidence", quote_price=result_price,
        available_at="2026-09-29T16:00:00+08:00",
        quote_as_of="2026-09-29T15:00:00+08:00",
        rows=[{"date": "2026-09-28", "close": 10}, {"date": "2026-09-29", "close": result_price}],
    )
    clock("2026-09-29T16:00:00+08:00")
    reviewed = workflow.review(run["id"], {
        "request_id": "review-price-action-risk", "evidence_ids": ["price-action-evidence"]
    })
    observation = next(item for item in reviewed["observations"] if item["review_date"] == "2026-09-29")
    assert observation["status"] == status and observation["direction_hit"] is direction_hit
    if status == "pending":
        assert observation["return_pct"] is None and observation["result_price"] is None
        assert "核验折算/公司行动" in observation["pending_reason"]
        assert observation["price_discontinuities"][0]["ratio"] == pytest.approx(result_price / 10)
        assert observation["source_ids"] == ["price-action-evidence"]
        assert all(store.list(f"roles/{role}") == [] for role in workflow_module.ALL_ROLES)
        assert not store.path("source_results").exists()
    else:
        assert observation["return_pct"] == pytest.approx((result_price / 10 - 1) * 100)
        assert len(store.list("roles/opportunity")) == 1


def test_intermediate_split_cannot_be_hidden_by_matching_final_price(setup, clock):
    store, workflow = setup
    run = prepare(workflow, "intermediate-price-break")
    complete_roles(workflow, run["id"])
    workflow.publish(run["id"], decision())
    put_market(
        store, "intermediate-break-evidence", quote_price=10,
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        rows=[
            {"date": "2026-09-28", "close": 10},
            {"date": "2026-09-29", "close": 10 / 3},
            {"date": "2026-09-30", "close": 10},
        ],
    )
    clock("2026-09-30T16:00:00+08:00")
    review = workflow.review(run["id"], {
        "request_id": "review-intermediate-break", "evidence_ids": ["intermediate-break-evidence"]
    })
    assert all(item["status"] == "pending" for item in review["observations"])
    final = next(item for item in review["observations"] if item["review_date"] == "2026-09-30")
    assert len(final["price_discontinuities"]) == 2
    assert final["direction_hit"] is None and final["return_pct"] is None
    assert store.list("roles/opportunity") == []
