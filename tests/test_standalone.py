import copy
import json
import multiprocessing

import pytest
import requests

import app.model as model_module
import app.runner as runner_module
import app.workflow as workflow_module
import app.ledger as ledger_module
from app.model import DeepSeek, MODEL, ModelFormatError, public_config
from app.runner import (
    InputBlocked,
    Runner,
    create_job,
    execution_lock,
    scheduled_request,
    status,
)
from app.store import ConflictError, DomainError, Store
from app.settings import update_settings
from app.workflow import Workflow
from test_workflow import RUN_AS_OF, complete_roles, decision, opinion, prepare, put_market, put_text


def _sse_chunk(*, content="", finish=None, usage=None, request_id="provider-1"):
    value = {
        "id": request_id,
        "model": "deepseek-flash",
        "choices": [{"delta": {"content": content}, "finish_reason": finish}],
    }
    if usage is not None:
        value["usage"] = usage
    return b"data: " + json.dumps(value).encode("utf-8")


class FakeResponse:
    def __init__(self, lines, status_code=200, json_body=None):
        self.lines = lines
        self.status_code = status_code
        self.json_body = json_body

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def iter_lines(self):
        for line in self.lines:
            if isinstance(line, BaseException):
                raise line
            yield line

    def json(self):
        if self.json_body is None:
            raise ValueError("response is not JSON")
        return self.json_body


class FakeTransport:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        return self.responses.pop(0)


def _v2_decision(evidence_ids, supersedes=None):
    body = decision(horizon=2)
    body["plans"][0].update(
        action="hold",
        evidence_ids=list(evidence_ids),
        decision_basis="冻结证据支持继续持有，同时保留失效条件",
        long_term_view="长期价值仍需后续经营事实验证",
        sizing={
            "basis": "只做研究情景比较，不替用户核定实盘数量",
            "assumptions": ["费用暂按十元估算"],
            "scenarios": [
                {
                    "label": "持有一手",
                    "quantity": 100,
                    "entry_price": 10,
                    "exit_price": 11,
                    "stop_price": 9,
                    "estimated_fees": 10,
                    "reasoning": "比较触发与失效后的人民币得失",
                }
            ],
        },
        supersedes=supersedes,
    )
    return body


class MockModel:
    """Contract-aware model replacement; outputs still pass the real workflow validators."""

    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def complete(self, context, contract, **_kwargs):
        context = copy.deepcopy(context)
        self.calls.append({"context": context, "contract": contract})
        number = len(self.calls)
        if number == self.fail_on:
            raise DomainError(f"mock failure at call {number}")
        evidence_ids = [item["id"] for item in context.get("evidence", [])]
        if "reflection:string" in contract:
            body = {"reflection": f"{context['role']} 按到期价格事实复盘，不把未成交写成盈利。"}
        elif "summary:string,evidence_ids" in contract:
            body = opinion(f"{context['role']} {context['phase']} 意见", evidence_ids)
        elif "summary:string,reasoning:string" in contract:
            pending = context.get("pending_plans", [])
            body = _v2_decision(evidence_ids, pending[0]["run_id"] if pending else None)
        elif "acknowledgement:string" in contract:
            body = {"acknowledgement": "已读取共同裁决", "remaining_disagreements": ["仍保留条件分歧"]}
        else:
            raise AssertionError(f"unexpected contract: {contract}")
        return body, {"id": f"mock-call-{number}", "model": "deepseek-flash"}


class FormatThenValidModel:
    def __init__(self):
        self.calls = []

    def complete(self, context, contract, **_kwargs):
        self.calls.append(copy.deepcopy(context))
        if len(self.calls) == 1:
            raise ModelFormatError("模型 JSON 格式错误：Extra data", '{}{"second":true}')
        return (
            {"acknowledgement": "已读取共同裁决", "remaining_disagreements": []},
            {"id": "format-repair-2", "model": "deepseek-flash"},
        )


class NonFormatFailureThenValidModel:
    def __init__(self, message):
        self.message = message
        self.calls = []

    def complete(self, context, contract, **_kwargs):
        self.calls.append(copy.deepcopy(context))
        if len(self.calls) == 1:
            raise DomainError(self.message)
        return opinion("不应被调用的自动重试", ["market-1", "text-1"]), {
            "id": "unexpected-retry",
            "model": "deepseek-flash",
        }


def _prepared_replay(store, request_id="standalone-seven"):
    put_market(store, "market-1")
    put_text(store)
    return {
        "request_id": request_id,
        "mode": "replay",
        "as_of": RUN_AS_OF,
        "symbols": ["601398"],
        "evidence_ids": ["market-1", "text-1"],
        "decision_contract": 2,
    }


def _hold_runner_lock(root, ready, release):
    with execution_lock(Store(root)):
        ready.set()
        release.wait(10)


@pytest.mark.parametrize(
    "lines,error_fragment",
    [
        (
            [
                _sse_chunk(content='{"partial":true}', finish="length", usage={"total_tokens": 12}),
                b"data: [DONE]",
            ],
            "length",
        ),
        (
            [_sse_chunk(content='{"partial":', finish=None), requests.ConnectionError("wire dropped")],
            "wire dropped",
        ),
    ],
)
def test_sse_truncation_or_disconnect_is_failed_and_max_parameters_are_really_sent(
    tmp_path, lines, error_fragment
):
    store = Store(tmp_path)
    store.write("model-secret.json", {"api_key": "standalone-test-secret"})
    transport = FakeTransport([FakeResponse(lines)])

    with pytest.raises(DomainError, match=error_fragment):
        DeepSeek(store, transport).complete({"frozen": "context"}, "返回JSON对象", call_id="cut-off")

    _, request = transport.calls[0]
    assert request["json"]["model"] == "deepseek-flash"
    assert request["json"]["thinking"] == {"type": "enabled"}
    assert request["json"]["reasoning_effort"] == "max"
    assert request["json"]["max_tokens"] == 393216 == MODEL["max_tokens"]
    assert request["json"]["stream"] is True
    assert request["json"]["stream_options"] == {"include_usage": True}
    audit = store.read("model_calls/cut-off.json")
    assert audit["status"] == "failed"
    assert error_fragment in audit["error"]
    assert "result" not in audit


def test_secret_never_appears_in_public_state_or_model_call_records(tmp_path):
    secret = "sk-state-leak-sentinel"
    store = Store(tmp_path)
    store.write("model-secret.json", {"api_key": secret})
    response = FakeResponse(
        [
            _sse_chunk(content='{"ok":', finish=None),
            _sse_chunk(content="true}", finish="stop", usage={"prompt_tokens": 1, "completion_tokens": 2}),
            b"data: [DONE]",
        ]
    )
    result, _ = DeepSeek(store, FakeTransport([response])).complete({}, "返回JSON对象", call_id="safe-state")
    assert result == {"ok": True}

    exposed = {"config": public_config(store), "status": status(store)}
    assert secret not in json.dumps(exposed, ensure_ascii=False)
    for path in store.root.rglob("*"):
        if path.is_file() and path.name != "model-secret.json":
            assert secret not in path.read_text(encoding="utf-8", errors="replace")


def test_two_json_objects_raise_format_error_and_preserve_exact_raw_output(tmp_path):
    store = Store(tmp_path)
    store.write("model-secret.json", {"api_key": "format-error-secret"})
    first = '{"acknowledgement":"已读取","remaining_disagreements":[]}'
    second = '{"extra":"unexpected second object"}'
    response = FakeResponse(
        [
            _sse_chunk(content=first, finish=None),
            _sse_chunk(content=second, finish="stop", usage={"prompt_tokens": 10, "completion_tokens": 20}),
            b"data: [DONE]",
        ]
    )

    with pytest.raises(ModelFormatError, match="Extra data") as caught:
        DeepSeek(store, FakeTransport([response])).complete({}, "返回一个JSON对象", call_id="two-objects")

    assert caught.value.output == first + second
    audit = store.read("model_calls/two-objects.json")
    assert audit["status"] == "failed"
    assert audit["raw_output"] == first + second
    assert "Extra data" in audit["error"]
    assert "result" not in audit


def test_provider_context_rejection_keeps_full_input_and_does_not_create_result(tmp_path):
    secret = "provider-limit-secret"
    store = Store(tmp_path)
    store.write("model-secret.json", {"api_key": secret})
    full_text = "资" * 1_100_000
    response = FakeResponse(
        [],
        status_code=400,
        json_body={"error": {"message": "Input length exceeds the 1048576 token context capacity"}},
    )
    transport = FakeTransport([response])

    with pytest.raises(DomainError, match="1048576 token context capacity"):
        DeepSeek(store, transport).complete({"selected_full_text": full_text}, "返回一个JSON对象", call_id="too-large")

    sent_context = json.loads(transport.calls[0][1]["json"]["messages"][1]["content"])
    assert sent_context["selected_full_text"] == full_text
    audit = store.read("model_calls/too-large.json")
    assert audit["status"] == "failed"
    assert "1048576 token context capacity" in audit["error"]
    assert secret not in audit["error"]
    assert "result" not in audit


def test_mock_runner_completes_all_seven_stages_and_keeps_initial_drafts_isolated(tmp_path):
    store = Store(tmp_path)
    request = _prepared_replay(store)
    job = create_job(store, request["request_id"], source="replay", prepare=request)
    client = MockModel()

    completed = Runner(store, client).execute(job["id"])

    assert completed["status"] == "completed"
    assert len(client.calls) == 7
    step_names = {path.stem for path in store.path(f"automation/steps/{job['id']}").glob("*.json")}
    assert step_names == {
        "initial-opportunity",
        "initial-counter",
        "challenge-opportunity",
        "challenge-counter",
        "judge",
        "decision-opportunity",
        "decision-counter",
    }
    initial_contexts = [call["context"] for call in client.calls if call["context"].get("phase") == "initial"]
    assert [ctx["role"] for ctx in initial_contexts] == ["opportunity", "counter"]
    assert "counter initial 意见" not in json.dumps(initial_contexts[0], ensure_ascii=False)
    assert "opportunity initial 意见" not in json.dumps(initial_contexts[1], ensure_ascii=False)
    run = Workflow(store).get_run(completed["run_id"])
    assert run["decision"]["plans"][0]["action"] == "hold"


def test_failed_job_resumes_without_recalling_any_completed_stage(tmp_path):
    store = Store(tmp_path)
    request = _prepared_replay(store, "resume-seven")
    job = create_job(store, request["request_id"], source="replay", prepare=request)
    first_client = MockModel(fail_on=7)

    failed = Runner(store, first_client).execute(job["id"])
    assert failed["status"] == "failed"
    assert len(first_client.calls) == 7
    assert len(list(store.path(f"automation/steps/{job['id']}").glob("*.json"))) == 6

    resumed_client = MockModel()
    completed = Runner(store, resumed_client).execute(job["id"])
    assert completed["status"] == "completed"
    assert len(resumed_client.calls) == 1
    assert "acknowledgement:string" in resumed_client.calls[0]["contract"]
    assert len(list(store.path(f"automation/steps/{job['id']}").glob("*.json"))) == 7


def test_runner_gives_one_same_stage_repair_for_model_format_error_then_caches_valid_result(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "format-repair")
    client = FormatThenValidModel()
    runner = Runner(store, client)
    runner.job = job

    result = runner.call(
        "decision-counter",
        {"role": "counter", "phase": "decision"},
        "返回JSON {acknowledgement:string,remaining_disagreements:[string]}",
    )

    assert len(client.calls) == 2
    assert "validation_feedback" not in client.calls[0]
    feedback = client.calls[1]["validation_feedback"]
    assert "Extra data" in feedback["error"]
    assert feedback["previous_output"] == '{}{"second":true}'
    assert "一个JSON对象" in feedback["instruction"]
    assert result["acknowledgement"] == "已读取共同裁决"
    assert store.read(f"automation/steps/{job['id']}/decision-counter.json")["call_id"] == "format-repair-2"
    assert runner.call("decision-counter", {}, "unused") == result
    assert len(client.calls) == 2


@pytest.mark.parametrize(
    "message",
    [
        "模型未完整结束（length）；不把截断内容发布为裁决",
        "DeepSeek HTTP 400：Input length exceeds context capacity；未切换模型或截断输入",
    ],
)
def test_runner_does_not_auto_retry_truncation_or_http_errors(tmp_path, message):
    store = Store(tmp_path)
    request = _prepared_replay(store, "non-format-no-retry-" + str(abs(hash(message))))
    job = create_job(store, request["request_id"], source="replay", prepare=request)
    client = NonFormatFailureThenValidModel(message)

    failed = Runner(store, client).execute(job["id"])

    assert failed["status"] == "failed"
    assert message in failed["error"]
    assert len(client.calls) == 1
    run = Workflow(store).get_run(failed["run_id"])
    assert run["opinions"] == {}
    assert run["decision"] is None


def test_job_request_is_idempotent_and_runner_lock_rejects_process_overlap(tmp_path):
    store = Store(tmp_path)
    first = create_job(store, "same-request")
    assert create_job(store, "same-request") == first
    with pytest.raises(ConflictError, match="排队|正在运行"):
        create_job(store, "different-request")

    context = multiprocessing.get_context("spawn")
    ready, release = context.Event(), context.Event()
    process = context.Process(target=_hold_runner_lock, args=(str(store.root), ready, release))
    process.start()
    try:
        assert ready.wait(10), "child process did not acquire runner lock"
        with pytest.raises(ConflictError, match="不重复调用模型"):
            with execution_lock(store):
                pass
    finally:
        release.set()
        process.join(10)
        if process.is_alive():
            process.terminate()
            process.join(5)
    assert process.exitcode == 0


def test_missing_channels_block_before_any_model_call(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "no-channels")
    client = MockModel()

    result = Runner(store, client).execute(job["id"])

    assert result["status"] == "blocked"
    assert "渠道" in result["error"]
    assert client.calls == []
    assert store.list("runs") == []
    assert not store.path(f"automation/steps/{job['id']}").exists()


def test_cached_stage_is_revalidated_against_the_current_boundary(tmp_path):
    store = Store(tmp_path)
    job = create_job(store, "stale-cache")
    runner = Runner(store, MockModel())
    runner.job = job
    store.write(
        f"automation/steps/{job['id']}/select-evidence.json",
        {"payload": {"evidence_ids": ["formerly-allowed"], "reasoning": "旧边界"}},
    )

    def current_validator(body):
        if set(body["evidence_ids"]) - {"currently-allowed"}:
            raise DomainError("缓存越过当前允许资料边界")

    with pytest.raises(DomainError, match="缓存越过"):
        runner.call("select-evidence", {}, "unused", current_validator)
    assert runner.client.calls == []


class SelectionModel:
    def __init__(self, selected_id, invalid_first=False):
        self.selected_id = selected_id
        self.invalid_first = invalid_first
        self.calls = []

    def complete(self, context, contract, **_kwargs):
        self.calls.append(copy.deepcopy(context))
        if self.invalid_first and len(self.calls) == 1:
            body = {"evidence_ids": ["outside-current-catalog"], "reasoning": "越界尝试"}
        else:
            body = {"evidence_ids": [self.selected_id], "reasoning": "与观察标的直接相关"}
        return body, {"id": f"selection-{len(self.calls)}", "model": "deepseek-flash"}


class SelectEverythingModel:
    def __init__(self, evidence_ids):
        self.evidence_ids = evidence_ids
        self.calls = []

    def complete(self, context, contract, **_kwargs):
        self.calls.append(copy.deepcopy(context))
        return (
            {"evidence_ids": list(self.evidence_ids), "reasoning": "全部纳入"},
            {"id": f"select-all-{len(self.calls)}", "model": "deepseek-flash"},
        )


def _manual_input_store(store):
    market = put_market(
        store,
        "manual-market",
        available_at="2026-09-24T16:00:00+08:00",
        quote_as_of="2026-09-24T15:00:00+08:00",
        rows=[{"date": "2026-09-24", "open": 9.8, "close": 10, "high": 10.1, "low": 9.7, "volume": 1000}],
    )
    cfg = update_settings(
        store,
        {
            "watchlist": [{"symbol": "601398", "name": "工商银行"}],
            "channels": {
                "market": {"name": "人工行情", "reference": "本地文件", "mode": "manual"},
                "trading": {"name": "交易资料", "reference": "本地文件", "mode": "manual"},
            },
        },
    )
    market["source_id"] = cfg["channels"]["market"]["source_id"]
    store.write("evidence/manual-market.json", market, overwrite=True)
    return cfg


def test_selection_sees_catalog_only_rejects_outside_ids_then_freezes_full_selected_text(
    monkeypatch, tmp_path
):
    now = "2026-09-24T16:30:00+08:00"
    monkeypatch.setattr(runner_module, "now_iso", lambda: now)
    monkeypatch.setattr(workflow_module, "now_iso", lambda: now)
    monkeypatch.setattr(ledger_module, "now_iso", lambda: now)
    store = Store(tmp_path)
    cfg = _manual_input_store(store)
    full_text = "FULL_TEXT_SENTINEL_" + ("正文" * 20000)
    article = {
        "id": "allowed-article",
        "kind": "text",
        "source_id": cfg["channels"]["trading"]["source_id"],
        "title": "工商银行经营公告",
        "url": "https://example.invalid/allowed",
        "published_at": "2026-09-24T10:00:00+08:00",
        "captured_at": "2026-09-24T11:00:00+08:00",
        "available_at": "2026-09-24T11:00:00+08:00",
        "status": "ready",
        "text": full_text,
        "metadata": {},
    }
    store.write("evidence/allowed-article.json", article)
    store.write("evidence/excluded-article.json", {**article, "id": "excluded-article", "source_id": "not-allowed"})
    job = create_job(store, "catalog-selection")
    client = SelectionModel("allowed-article", invalid_first=True)
    runner = Runner(store, client)
    runner.job = job

    request = runner.inputs()

    assert request["evidence_ids"] == ["manual-market", "allowed-article"]
    assert len(client.calls) == 2
    first_call = json.dumps(client.calls[0], ensure_ascii=False)
    assert "FULL_TEXT_SENTINEL" not in first_call
    assert {item["id"] for item in client.calls[0]["catalog"]} == {"allowed-article"}
    assert set(client.calls[0]["catalog"][0]) == {"id", "title", "published_at", "source_id", "characters"}
    assert client.calls[1]["validation_feedback"]["previous_output"]["evidence_ids"] == [
        "outside-current-catalog"
    ]
    frozen = Workflow(store).prepare(request)
    selected = next(item for item in frozen["evidence"] if item["id"] == "allowed-article")
    assert selected["text"] == full_text


def test_no_optional_allowed_material_adds_no_selection_model_call(monkeypatch, tmp_path):
    now = "2026-09-24T16:30:00+08:00"
    monkeypatch.setattr(runner_module, "now_iso", lambda: now)
    monkeypatch.setattr(ledger_module, "now_iso", lambda: now)
    store = Store(tmp_path)
    _manual_input_store(store)
    store.write(
        "evidence/unrelated.json",
        {
            "id": "unrelated",
            "kind": "text",
            "source_id": "not-allowed",
            "title": "未授权资料",
            "available_at": "2026-09-24T11:00:00+08:00",
            "status": "ready",
            "text": "不能触发模型选择",
            "metadata": {},
        },
    )
    job = create_job(store, "no-selection-needed")
    client = SelectionModel("unused")
    runner = Runner(store, client)
    runner.job = job

    request = runner.inputs()

    assert request["evidence_ids"] == ["manual-market"]
    assert client.calls == []


def test_selection_does_not_invent_a_local_character_limit_without_provider_tokenization(monkeypatch, tmp_path):
    now = "2026-09-24T16:30:00+08:00"
    monkeypatch.setattr(runner_module, "now_iso", lambda: now)
    monkeypatch.setattr(ledger_module, "now_iso", lambda: now)
    store = Store(tmp_path)
    cfg = _manual_input_store(store)
    ids = []
    for index in range(3):
        evidence_id = f"oversized-{index}"
        ids.append(evidence_id)
        store.write(
            f"evidence/{evidence_id}.json",
            {
                "id": evidence_id,
                "kind": "text",
                "source_id": cfg["channels"]["trading"]["source_id"],
                "title": f"超长资料 {index}",
                "url": f"https://example.invalid/{index}",
                "published_at": "2026-09-24T10:00:00+08:00",
                "captured_at": "2026-09-24T11:00:00+08:00",
                "available_at": "2026-09-24T11:00:00+08:00",
                "status": "ready",
                "text": "资" * 600_000,
                "metadata": {},
            },
        )
    job = create_job(store, "oversized-selection")
    client = SelectEverythingModel(ids)
    runner = Runner(store, client)
    runner.job = job

    request = runner.inputs()

    assert request["evidence_ids"] == ["manual-market", *ids]
    assert len(client.calls) == 1
    assert {item["id"] for item in client.calls[0]["catalog"]} == set(ids)


@pytest.mark.parametrize(
    "instant,expected",
    [
        ("2026-09-27T04:59:59+00:00", "scheduled-20260926-1900"),
        ("2026-09-27T05:00:00+00:00", "scheduled-20260927-1300"),
        ("2026-09-27T10:59:59+00:00", "scheduled-20260927-1300"),
        ("2026-09-27T11:00:00+00:00", "scheduled-20260927-1900"),
    ],
)
def test_scheduled_request_uses_beijing_slots(instant, expected):
    assert scheduled_request(instant) == expected


def test_due_reflections_are_saved_before_next_round_snapshot(monkeypatch, tmp_path):
    now = "2026-09-30T16:30:00+08:00"
    monkeypatch.setattr(runner_module, "now_iso", lambda: now)
    monkeypatch.setattr(workflow_module, "now_iso", lambda: now)
    monkeypatch.setattr(model_module, "now_iso", lambda: now)
    store = Store(tmp_path)
    put_market(store, "market-1")
    put_text(store)
    workflow = Workflow(store)
    old_run = prepare(workflow, "old-round", decision_contract=1)
    complete_roles(workflow, old_run["id"])
    workflow.publish(old_run["id"], decision(horizon=2))
    put_market(
        store,
        "market-current",
        available_at="2026-09-30T16:00:00+08:00",
        quote_as_of="2026-09-30T15:00:00+08:00",
        quote_price=11,
        rows=[
            {"date": "2026-09-28", "open": 10, "close": 10, "high": 10.1, "low": 9.9, "volume": 1000},
            {"date": "2026-09-29", "open": 10, "close": 10.5, "high": 10.6, "low": 9.9, "volume": 1000},
            {"date": "2026-09-30", "open": 10.5, "close": 11, "high": 11.1, "low": 10.4, "volume": 2000},
        ],
    )
    request = {
        "request_id": "next-round-after-review",
        "mode": "live",
        "as_of": now,
        "symbols": ["601398"],
        "evidence_ids": ["market-current"],
        "decision_contract": 2,
    }
    job = create_job(store, request["request_id"], source="manual", prepare=request)
    client = MockModel()

    completed = Runner(store, client).execute(job["id"])

    assert completed["status"] == "completed"
    assert completed["reviews_done"] is True
    assert completed["prepared_request"]["as_of"] == now
    review_calls = [call for call in client.calls if "new_observations" in call["context"]]
    assert [call["context"]["role"] for call in review_calls] == ["opportunity", "counter", "judge"]
    initial_calls = [
        call for call in client.calls
        if call["context"].get("phase") == "initial" and "new_observations" not in call["context"]
    ]
    assert len(initial_calls) == 2
    for call in initial_calls:
        role = call["context"]["role"]
        assert call["context"]["memory"], f"{role} did not receive the just-saved review memory"
        assert call["context"]["memory"][0]["reflection"]["text"].startswith(role)
    assert store.list("roles/judge"), "judge reflection was not persisted for later rounds"
