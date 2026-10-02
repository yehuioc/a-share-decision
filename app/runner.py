"""Durable, resumable research jobs runnable from Windows or the local page."""
from __future__ import annotations
import argparse
from contextlib import contextmanager
from datetime import timedelta
import json
import os
import subprocess
import sys
import uuid
from .store import Store, DomainError, ConflictError, PROJECT_ROOT, now_iso, parse_time, safe_id, stable_id
from .settings import settings, market_channel
from .model import DeepSeek, ModelFormatError, public_config
from .workflow import Workflow
from .agents import RoleAgent, agent_config, agent_status, ROLES

OPINION = '''返回JSON字段 summary:string,evidence_ids:[本轮证据id],claims:[string],counterevidence:[string],unknowns:[string],changes:[string]。所有列表元素为字符串。独立初稿不猜对方观点，回应阶段仅回应一次。解释价格与经营的因果链、替代解释与账户机会成本；区分历史事实与当前假设，不拿风险预算充当收益依据。'''
OPINION += '''另给forecasts:[{symbol,horizon_days,expected_direction,rationale,falsifier}]，每个冻结标的分别声明1、5、20交易日判断。direction可用up/down/flat/uncertain，未知仍须解释竞争情景与区分证据；不强迫确定方向。初稿与回应分别保存和复核，不能借最终裁决的成败替代自己的判断。'''
OPINION += '''上述字段名是expected_direction，不是direction。forecasts仅包括完整的1、5、20交易日，其他期限放文字说明。事实只能引用已冻结来源，资料有效期不自动延长；原始事实是否正确与其预测价值分别判断。'''
DECISION = '''返回JSON {summary:string,reasoning:string,plans:[plan]}。每个冻结标的恰好一个plan：symbol，action(buy/hold/reduce/sell/avoid)，thesis，evidence_ids:[id]，counterevidence:[string]，unknowns:[string]，trigger，invalidation，upside，downside，costs，horizon_days:1至250整数，expected_direction(up/down/flat/uncertain)，quantity:null，supersedes:旧run_id或null，decision_basis，long_term_view，sizing:{basis:string,assumptions:[string],scenarios:[{label,quantity:100的正整数倍,entry_price:正数,exit_price:正数,stop_price:正数,estimated_fees:非负数,reasoning:string}]}。除标明列表数字/null的字段外均为非空字符串。情景价格必须stop_price<entry_price<exit_price。即使建议减仓或不买也给比较继续持有的人民币得失测算；注明情景依据，禁止捏造目标概率。quantity始终null，研究数量仅位于sizing，不替用户核定执行量。同标的同horizon已存在计划时supersedes必须为pending_plans对应的run_id。说明旧条件、新证据与动作差异。比较全部账户的现金、持有与调整选择，不能只复述阈值。'''
DECISION += '''合同3每个plan另须包含：
edge:{mechanism:string,alternative_explanation:string,priced_in:string,falsifier:string,probability_basis:unknown/subjective/empirical,win_probability:null或0至1,sample_refs:[冻结证据ID],independent_samples:null或非负整数}。没有概率证据可用unknown/null，程序仍计算盈亏平衡概率。empirical须有样本引用与独立样本数；同簇日期不算独立样本，不能用模型共识制造胜率。
execution:{strategy_key:稳定的策略假说名称,assumptions:[string],legs:[{action:buy/hold/sell/avoid,quantity:100的正整数倍,entry:{kind:next_open/limit/stop/close_above/close_below/manual/none,price:正数或null},take_profit:正数或null,stop_loss:正数或null,exit_basis:intraday/close,conditions:[需人工确认的非价格条件],buy_fee:非负数或null,sell_fee:非负数或null,slippage_bps:非负数,reasoning:string}]}。
各分仓数量不可重复占用。分仓action须同plan.action，只有reduce应拆为hold与sell。hold/avoid的entry用none/null；buy/sell必须给入场/卖出规则。next_open/manual/none的price用null。sell/avoid的take_profit/stop_loss必须null；buy/hold保护价满足stop<入场参考价<target。分批目标用多个互不重叠分仓。费用与滑点遵守冻结research_brief.cost_model；mode为zero_friction时，estimated_fees、buy_fee、sell_fee、slippage_bps全部明确填0，costs只写用户指定零摩擦口径，不用费用或税费否决动作，也不列费用敏感性。conditions非空或manual需要人工路径，日线不替你判定新闻条件为真。价格规则最早从发布后的首个完整交易日评估，盘中发布从次一交易日开始；收盘触发最早次日开盘成交，股票/本研究股票ETF遵守T+1。到期剩余仓位只估值，不虚构卖出。旧计划不能事后重写。
顶层另给argument_resolution:{opportunity:string,counter:string}，逐个说明该角色哪些论据被采纳、拒绝或保留，为什么；不把角色人数当作概率。'''
DECISION += '''合同3中同一标的只能有一条当前有效计划，不因改变horizon而保留旧的冲突计划；如pending_plans中存在该标的，supersedes必须引用它的run_id。execution数量是研究假设，不是实盘核定量。sizing情景与execution分仓需对应，特别是hold的止盈止损以本轮baseline_price为参考；条件文字与结构规则必须一致，非价格条件进入conditions。必须解释收益来自什么可重复机制、为何现在可能存在、什么证据会推翻；不能以风险预算覆盖损失来证明正期望。'''
DECISION += '''零摩擦模式必须同时检查正文：summary、reasoning、decision_basis、unknowns、conditions、invalidation与argument_resolution均不得沿用历史“费用未确认”“账户和费用确认后”“扣费后”的前提。只保留现金、持仓、可卖数量和市场证据等实际影响动作的条件；费用口径只在costs说明一次。'''


class InputBlocked(DomainError):
    pass


@contextmanager
def execution_lock(store):
    path = store.path("automation/.runner.lock")
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = path.open("a+b")
    if fh.tell() == 0:
        fh.write(b"0")
        fh.flush()
    acquired = False
    try:
        fh.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(fh.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
            acquired = True
        except OSError:
            raise ConflictError("已有研究正在执行；不重复调用模型")
        yield
    finally:
        if acquired:
            fh.seek(0)
            if os.name == "nt":
                msvcrt.locking(fh.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(fh, fcntl.LOCK_UN)
        fh.close()


def jobs(store):
    return sorted(store.list("automation/jobs"), key=lambda j: j["created_at"], reverse=True)


def reconcile(store):
    # Only a released OS lock proves a worker is gone. A long model thought is not a failure.
    try:
        with execution_lock(store), store.transaction():
            for job in jobs(store):
                age = (parse_time(now_iso()) - parse_time(job["updated_at"])).total_seconds()
                if job["status"] == "running" or (job["status"] == "queued" and age > 60):
                    job.update(status="interrupted", error="后台进程已结束，已保存阶段可继续", updated_at=now_iso())
                    store.write(f"automation/jobs/{job['id']}.json", job, overwrite=True)
    except ConflictError:
        pass


def status(store):
    reconcile(store)
    public_jobs = [{k: v for k, v in j.items() if k not in ("request", "prepared_request")} for j in jobs(store)[:20]]
    return {"model": public_config(store), "jobs": public_jobs, "agents": [agent_status(store, role) for role in ROLES],
            "schedule": store.read("automation/schedule.json", {"installed": False, "times": ["13:00", "19:00"], "timezone": "Asia/Shanghai"})}


def create_job(store, request_id, *, source="manual", prepare=None):
    reconcile(store)
    jid = stable_id("job", request_id)
    with store.transaction():
        existing = store.read(f"automation/jobs/{jid}.json")
        if existing:
            return existing
        if any(j["status"] in ("queued", "running") for j in jobs(store)):
            raise ConflictError("已有研究排队或正在运行，请等待完成")
        record = {"id": jid, "source": source, "created_at": now_iso(), "updated_at": now_iso(),
                  "status": "queued", "phase": "等待启动", "request": prepare,
                  "producer": "deepseek", "producer_role": "automation", "review_owner": "user",
                  "review_state": "needs_review", "canonical_status": "record"}
        return store.write(f"automation/jobs/{jid}.json", record)


def launch(store, job):
    logdir = PROJECT_ROOT / ".tmp" / "workers"
    logdir.mkdir(parents=True, exist_ok=True)
    executable = sys.executable
    if os.name == "nt":
        candidate = os.path.join(os.path.dirname(executable), "pythonw.exe")
        if os.path.exists(candidate):
            executable = candidate
    flags = (subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP) if os.name == "nt" else 0
    with (logdir / f"{job['id']}.log").open("ab") as log:
        subprocess.Popen([executable, "-m", "app.runner", "job", job["id"], "--data-dir", str(store.root)],
                         cwd=PROJECT_ROOT, stdin=subprocess.DEVNULL, stdout=log, stderr=log,
                         creationflags=flags, start_new_session=os.name != "nt")
    return job


def resume(store, jid):
    reconcile(store)
    with store.transaction():
        job = store.read(f"automation/jobs/{safe_id(jid)}.json")
        if not job or job["status"] not in ("failed", "interrupted"):
            raise DomainError("只有失败或中断的研究可继续；资料缺失请补充后新建研究")
        if any(j["status"] in ("queued", "running") for j in jobs(store)):
            raise ConflictError("已有研究正在运行")
        job.update(status="queued", phase="等待继续", updated_at=now_iso(), error=None)
        store.write(f"automation/jobs/{jid}.json", job, overwrite=True)
    return launch(store, job)


class Runner:
    def __init__(self, store, client=None):
        self.store = store
        self.workflow = Workflow(store)
        self.client = client or DeepSeek(store)
        self.test_client = client
        self.agents = {}

    def role_agent(self, context):
        role = context["role"]
        config = context.get("agent")
        if not config and context.get("run_id"):
            # Legacy runs predate agent snapshots; keep their existing session identity.
            session = self.store.read(f"agents/ashare-{safe_id(role)}/sessions/{safe_id(context['run_id'])}/session.json", {})
            config = session.get("config")
        config = config or agent_config(role)
        if config.get("role") != role or config.get("id") != f"ashare-{role}":
            raise DomainError("冻结 agent 配置与角色不一致")
        key = (config["id"], config["revision"])
        if key not in self.agents:
            self.agents[key] = RoleAgent(self.store, config, self.test_client)
        return self.agents[key]

    def update(self, **values):
        with self.store.transaction():
            self.job = self.store.read(f"automation/jobs/{self.job['id']}.json")
            self.job.update(values, updated_at=now_iso())
            self.store.write(f"automation/jobs/{self.job['id']}.json", self.job, overwrite=True)

    def call(self, stage, context, contract, validator=None):
        self.update(phase=stage)
        rel = f"automation/steps/{self.job['id']}/{safe_id(stage)}.json"
        cached = self.store.read(rel)
        if cached:
            if validator:
                validator(cached["payload"])
            return cached["payload"]
        repair = None
        for attempt in range(2):
            try:
                request_context = {**context, **({"validation_feedback": repair} if repair else {})}
                progress = lambda v: self.update(progress=v)
                if context.get("role") in ROLES and context.get("run_id"):
                    body, audit = self.role_agent(context).complete(request_context, contract,
                                      job_id=self.job["id"], stage=stage, progress=progress)
                else:
                    body, audit = self.client.complete(request_context, contract, progress=progress)
            except ModelFormatError as exc:
                if attempt:
                    raise
                repair = {"error": str(exc), "previous_output": exc.output,
                          "instruction": "上一次不是完整单个JSON对象。修复为契约要求的一个JSON对象，不附加其他正文或第二个对象。"}
                continue
            body.update(model=audit["model"], reasoning_effort="max", agent_id=audit.get("agent_id", "source-selector"),
                        call_id=audit["id"], agent_session_id=audit.get("agent_session_id"),
                        producer="deepseek", producer_role="automation", producer_evidence=f"model_calls/{audit['id']}.json",
                        review_owner="user", review_state="needs_review", canonical_status="candidate")
            try:
                if validator:
                    validator(body)
            except DomainError as exc:
                if attempt:
                    raise
                repair = {"error": str(exc), "previous_output": body,
                          "instruction": "依据同一冻结材料修复该字段问题，不改变来源与可知时间。"}
                continue
            self.store.write(rel, {"payload": body, "call_id": audit["id"], "completed_at": now_iso()})
            return body

    def inputs(self):
        from .packets import sync_inbox, latest_packet, is_local_collection
        from .market import market_readiness
        sync_inbox(self.store)
        cfg = settings(self.store)
        try:
            channel = market_channel(self.store)
        except DomainError as exc:
            raise InputBlocked(str(exc)) from exc
        if not cfg["channels"]["trading"]:
            raise InputBlocked("交易信息渠道尚未指定，请在资料与来源填写实际使用的平台或文件来源")
        self.update(phase="更新指定资料")
        if channel["mode"] == "tencent":
            from .market import collect
            market = collect(self.store, self.job["id"])
        else:
            eligible = [e for e in self.store.list("evidence") if e.get("kind") == "market" and e.get("source_id") == channel["source_id"]]
            market = max(eligible, key=lambda e: e["available_at"], default=None)
        if not market or market.get("status") != "ready":
            raise InputBlocked("指定渠道尚无可用主价格，请同步报价及对应交易日期")
        readiness = market_readiness(market, [x["symbol"] for x in cfg["watchlist"]], now_iso())
        if readiness["ready_state"] == "NOT_READY":
            raise InputBlocked("行情交付不完整：" + "；".join(readiness["gaps"]))
        packet = latest_packet(self.store, now_iso())
        reference = cfg["channels"]["trading"].get("reference", "")
        if ("chatgpt.com/c/" in reference or is_local_collection(reference)) and packet["ready_state"] == "NOT_READY":
            raise InputBlocked("绑定情报尚不能支持新研究：" + "；".join(packet["gaps"]))
        if cfg["channels"]["caixin_enabled"]:
            from .caixin import sync_caixin
            sync_caixin(self.store, request_id=self.job["id"])
        allowed = {"manual", cfg["channels"]["trading"]["source_id"]}
        if cfg["channels"]["caixin_enabled"]:
            allowed.add("caixin")
        # A deterministic source filter, not an LLM source-selection permission.
        selected = {}
        for e in self.store.list("evidence"):
            if e.get("kind") == "market" or e.get("status") != "ready" or e.get("source_id") not in allowed:
                continue
            identity = (e["source_id"], e.get("metadata", {}).get("article_id") or e.get("url") or e["id"])
            if identity not in selected or e["available_at"] > selected[identity]["available_at"]:
                selected[identity] = e
        request = {"request_id": self.job["id"], "mode": "live", "as_of": now_iso(), "decision_contract": 3,
                   "symbols": readiness["usable_symbols"],
                   "evidence_ids": [market["id"]]}
        brief = self.store.read("research_profile.json", {})
        request["research_brief"] = {**brief, "data_scope": {
            "requested_symbols": [x["symbol"] for x in cfg["watchlist"]],
            "included_symbols": readiness["usable_symbols"],
            "unavailable_symbols": readiness["unavailable_symbols"],
            "limitations": readiness["warnings"] + packet.get("warnings", [])}}
        if packet.get("evidence_id") and packet["ready_state"] != "NOT_READY":
            request["evidence_ids"].append(packet["evidence_id"])
            selected.pop((cfg["channels"]["trading"]["source_id"], cfg["channels"]["trading"]["reference"]), None)
            selected = {key: e for key, e in selected.items() if e["id"] != packet["evidence_id"]}
        # Fail before billable calls rather than publishing ungrounded scenarios.
        base = {**request, "evidence": [market]}
        for symbol in request["symbols"]:
            if self.workflow._baseline_for(symbol, base)["price"] is None:
                raise InputBlocked(f"{symbol} 缺少当前应有交易日的有效报价；请更新指定渠道")
        if selected:
            catalog = [{"id": e["id"], "title": e.get("title"), "published_at": e.get("published_at"),
                        "source_id": e["source_id"], "characters": len(e.get("text", ""))} for e in selected.values()]
            allowed_ids = {e["id"] for e in selected.values()}
            def validate_selection(body):
                ids = body.get("evidence_ids")
                if not isinstance(ids, list) or any(not isinstance(eid, str) or eid not in allowed_ids for eid in ids):
                    raise DomainError("资料选择只能引用给定目录中的证据id")
                if len(ids) != len(set(ids)) or not isinstance(body.get("reasoning"), str) or not body["reasoning"].strip():
                    raise DomainError("资料选择需去重并说明选择或不选的理由")
            selection = self.call("select-evidence", {"watchlist": cfg["watchlist"], "catalog": catalog,
                                    "research_brief": self.store.read("research_profile.json", {}), "as_of": request["as_of"]},
                                  '根据目录选择本轮全账户比较与观察标的研究所需的公告/行业/宏观资料。仅返回JSON {evidence_ids:[id],reasoning:string}。目录不是正文，不能声称读过正文；不因来源名称纳入全部资料，也不忽略不利证据。选择后各研究角色会收到被选资料的完整正文。没有相关材料时允许空数组并说明缺口。不要引用目录外资料。', validate_selection)
            request["evidence_ids"] += [eid for eid in selection["evidence_ids"] if eid not in request["evidence_ids"]]
        from .server import current_prices
        from .ledger import Ledger
        request["account_snapshot"] = Ledger(self.store).summary(current_prices(self.store))
        return request

    def review_due(self, market, as_of):
        from .calendar import expected_market_date
        cutoff = parse_time(as_of)
        for run in self.workflow.list_runs():
            if run.get("mode") != "live" or not run.get("decision"):
                continue
            completed = self.workflow._completed_observations(run["id"])
            facts = [self.workflow._observe(plan, point, [market], [market["id"]], expected_market_date(cutoff), cutoff)
                     for plan in run["decision"]["plans"] for point in plan["review_points"]
                     if (plan["symbol"], point["review_date"]) not in completed]
            outcomes = self.workflow.plan_outcomes(run["id"], [market], cutoff)
            previous = run.get("reviews", [])
            changed = self.workflow._outcomes_changed(outcomes, previous[-1].get("plan_outcomes", []) if previous else [])
            if not any(f.get("status") == "completed" for f in facts) and not changed:
                continue
            reflections = {}
            for role in ("opportunity", "counter", "judge"):
                ctx = self.workflow.context(run["id"], role, "decision")
                ctx.update(new_observations=facts, plan_outcomes=outcomes,
                           own_assessment=self.workflow.role_assessments(run["id"], facts)[role],
                           new_evidence=[market], review_as_of=as_of)
                body = self.call(f"review-{run['id']}-{role}", ctx,
                                 '返回JSON {reflection:string}。逐项对照own_assessment中的自己初稿、交叉回应、裁决采纳理由，以及plan_outcomes中的触发路径、模拟和实际执行归因。区分判断错误、证据错误、执行遗漏、先止损后上涨、未触发与不参与机会成本；未发生的结果和未确认成交不得写成盈利。明确下一次应保留或修改的方法及适用条件，不因一次涨跌制造普遍规律。',
                                 lambda b: self.workflow._normalise_reflections({role: b.get("reflection")}))
                reflections[role] = body["reflection"]
            self.workflow.review(run["id"], {"request_id": self.job["id"] + "-" + run["id"],
                                           "as_of": as_of, "evidence_ids": [market["id"]], "reflections": reflections})

    def execute(self, jid):
        with execution_lock(self.store):
            self.job = self.store.read(f"automation/jobs/{safe_id(jid)}.json")
            if not self.job:
                raise DomainError("研究任务不存在")
            if self.job["status"] in ("completed", "blocked"):
                return self.job
            self.update(status="running", phase="检查输入", error=None, pid=os.getpid())
            try:
                if not self.job.get("run_id"):
                    request = self.job.get("prepared_request") or self.job.get("request") or self.inputs()
                    self.update(prepared_request=request)
                    if request["mode"] == "live" and not self.job.get("reviews_done"):
                        market = self.store.read(f"evidence/{safe_id(request['evidence_ids'][0])}.json")
                        self.review_due(market, request["as_of"])
                        request = {**request, "as_of": now_iso()}
                        self.update(reviews_done=True, prepared_request=request)
                    run = self.workflow.prepare(request)
                    self.update(run_id=run["id"])
                rid = self.job["run_id"]
                for phase in ("initial", "challenge"):
                    # Context isolation is enforced by Workflow, including on resume.
                    for role in ("opportunity", "counter"):
                        run = self.workflow.get_run(rid)
                        if run["opinions"].get(role, {}).get(phase):
                            continue
                        ctx = self.workflow.context(rid, role, phase)
                        def validate(body):
                            normal = self.workflow._normalise_opinion(body)
                            self.workflow._validate_evidence_refs(normal["evidence_ids"], run)
                            self.workflow._validate_forecasts(normal, run)
                        body = self.call(f"{phase}-{role}", ctx, OPINION, validate)
                        self.workflow.submit_role(rid, role, phase, body)
                run = self.workflow.get_run(rid)
                if not run.get("decision"):
                    def validate_decision(body):
                        self.workflow._normalise_decision(body, run)
                        if {p["symbol"] for p in body["plans"]} != set(run["symbols"]):
                            raise DomainError("裁决必须覆盖全部冻结标的")
                    body = self.call("judge", self.workflow.context(rid, "judge", "judge"), DECISION, validate_decision)
                    self.workflow.publish(rid, body)
                for role in ("opportunity", "counter"):
                    def validate_receipt(body):
                        if not isinstance(body.get("acknowledgement"), str) or not body["acknowledgement"].strip():
                            raise DomainError("读取回执需有非空 acknowledgement")
                        disagreements = body.get("remaining_disagreements")
                        if not isinstance(disagreements, list) or any(not isinstance(x, str) for x in disagreements):
                            raise DomainError("remaining_disagreements 必须为字符串列表")
                    self.call(f"decision-{role}", self.workflow.context(rid, role, "decision"),
                              '返回JSON {acknowledgement:string,remaining_disagreements:[string]}。阅读共同裁决，概括它与你的观点的差异；读取不等于同意，不新增事实或事后经验。', validate_receipt)
                self.update(status="completed", phase="研究完成，等待用户阅读", progress=None)
            except InputBlocked as exc:
                self.update(status="blocked", error=str(exc), phase="等待指定渠道或补充资料")
            except Exception as exc:
                key = self.store.read("model-secret.json", {}).get("api_key") or "\0"
                self.update(status="failed", error=str(exc).replace(key, "[redacted]")[:1500], phase="执行失败，已保留完成阶段")
            return self.job


def scheduled_request(now=None):
    dt = parse_time(now or now_iso())
    if dt.hour >= 19:
        slot = dt.replace(hour=19, minute=0, second=0, microsecond=0)
    elif dt.hour >= 13:
        slot = dt.replace(hour=13, minute=0, second=0, microsecond=0)
    else:
        slot = (dt - timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
    return "scheduled-" + slot.strftime("%Y%m%d-%H%M")


def catch_up(store):
    """A late delivery may retry the last blocked slot once per input version."""
    from .market import market_readiness
    from .packets import latest_packet, sync_inbox, is_local_collection
    sync_inbox(store)
    slot = scheduled_request()
    original = store.read(f"automation/jobs/{stable_id('job', slot)}.json")
    if not original or original["status"] != "blocked":
        return {"status": "not_needed", "slot": slot, "reason": "最近时段没有等待资料的任务"}
    completed = next((j for j in jobs(store) if j.get("catch_up_for") == original["id"] and j["status"] == "completed"), None)
    if completed:
        return {"status": "not_needed", "slot": slot, "job_id": completed["id"], "reason": "该时段已补跑完成"}
    cfg = settings(store)
    channel = cfg["channels"]["market"]
    eligible = [e for e in store.list("evidence") if channel and e.get("kind") == "market"
                and e.get("source_id") == channel["source_id"]]
    market = max(eligible, key=lambda e: e["available_at"], default=None)
    quality = market_readiness(market, [x["symbol"] for x in cfg["watchlist"]], now_iso())
    packet = latest_packet(store, now_iso())
    gaps = list(quality["gaps"])
    if market and (market.get("status") != "ready" or parse_time(market["available_at"]) > parse_time(now_iso())):
        gaps.append("行情证据尚不可用")
    trading = cfg["channels"]["trading"] or {}
    if not trading:
        gaps.append("尚未指定交易信息渠道")
    elif ("chatgpt.com/c/" in trading.get("reference", "") or is_local_collection(trading.get("reference"))) and packet["ready_state"] == "NOT_READY":
        gaps.extend(packet["gaps"])
    if gaps:
        return {"status": "waiting_for_inputs", "slot": slot, "original_job_id": original["id"], "gaps": gaps}
    identity = slot + ":delivery:" + market["id"] + ":" + packet.get("evidence_id", "")
    previous = store.read(f"automation/jobs/{stable_id('job', identity)}.json")
    if previous:
        return {"status": "already_attempted", "slot": slot, "job_id": previous["id"], "job_status": previous["status"]}
    try:
        job = create_job(store, identity, source="late-delivery")
    except ConflictError:
        return {"status": "busy", "slot": slot, "reason": "已有研究运行，不能重复启动"}
    job.update(catch_up_for=original["id"], scheduled_slot=slot)
    store.write(f"automation/jobs/{job['id']}.json", job, overwrite=True)
    launch(store, job)
    return {"status": "queued", "slot": slot, "job_id": job["id"], "original_job_id": original["id"]}


def main():
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("scheduled", "start", "job", "status", "replay", "catch-up"))
    parser.add_argument("id", nargs="?")
    parser.add_argument("--data-dir")
    args = parser.parse_args()
    store = Store(args.data_dir)
    try:
        if args.command == "status":
            result = status(store)
        elif args.command == "catch-up":
            result = catch_up(store)
        elif args.command == "job":
            result = Runner(store).execute(args.id)
        else:
            request = None
            if args.command == "replay":
                old = Workflow(store).get_run(args.id)
                request = {"request_id": "replay-" + uuid.uuid4().hex, "mode": "replay", "as_of": old["as_of"],
                           "symbols": old["symbols"], "evidence_ids": old["evidence_ids"],
                           "account_snapshot": old["account_snapshot"], "research_brief": old.get("research_brief", {}), "decision_contract": 3}
            job = create_job(store, scheduled_request() if args.command == "scheduled" else uuid.uuid4().hex,
                             source=args.command, prepare=request)
            result = Runner(store).execute(job["id"])
        print(json.dumps({k: v for k, v in result.items() if k not in ("request", "prepared_request")}, ensure_ascii=False))
        return 0 if result.get("status") not in ("failed", "interrupted") else 2
    except (DomainError, OSError) as exc:
        print(json.dumps({"error": str(exc)}, ensure_ascii=False))
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
