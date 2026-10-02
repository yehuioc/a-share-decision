"""Role-specific local agents with a shared API profile and isolated journals."""
from __future__ import annotations
import copy
import hashlib
import json
from .store import PROJECT_ROOT, DomainError, safe_id, now_iso
from .model import DeepSeek, MODEL

ROLES = ("opportunity", "counter", "judge")


def agent_config(role):
    if role not in ROLES:
        raise DomainError("未知 agent 角色")
    config = json.loads((PROJECT_ROOT / "roles" / role / "agent.json").read_text(encoding="utf-8"))
    if config.get("id") != f"ashare-{role}" or config.get("role") != role or config.get("api_profile") != "shared-deepseek":
        raise DomainError("agent 身份或共享 API 配置不匹配")
    if not isinstance(config.get("system_instruction"), str) or not config["system_instruction"].strip():
        raise DomainError("agent 缺少独立系统指令")
    config["method_path"] = f"roles/{role}/method.md"
    config["memory_path"] = f"roles/{role}"
    config["sessions_path"] = f"agents/{config['id']}/sessions"
    config["revision"] = hashlib.sha256(json.dumps(config, sort_keys=True, ensure_ascii=False).encode()).hexdigest()
    return config


def agent_status(store, role):
    config = agent_config(role)
    root = store.path(config["sessions_path"])
    return {**config, "model": MODEL.copy(), "session_count": len(list(root.glob("*/session.json"))) if root.exists() else 0}


class RoleAgent:
    def __init__(self, store, config, client=None):
        self.store = store
        self.config = copy.deepcopy(config)
        self.client = client if client is not None else DeepSeek(store, agent=self.config)

    def complete(self, context, contract, *, job_id, stage, progress=None):
        if context.get("role") != self.config["role"]:
            raise DomainError("不能把其他角色上下文交给当前 agent")
        run_id = safe_id(context["run_id"])
        agent_id = safe_id(self.config["id"])
        session_id = f"{agent_id}-{run_id}"
        root = f"agents/{agent_id}/sessions/{run_id}"
        self.store.write(f"{root}/session.json", {"id": session_id, "agent_id": agent_id, "role": self.config["role"],
                         "run_id": run_id, "config": self.config})
        # Workflow supplies the time-filtered history. Never append an unfiltered journal to model input.
        body, audit = self.client.complete(context, contract, progress=progress)
        audit = {**audit, "agent_id": agent_id, "agent_session_id": session_id}
        self.store.write(f"{root}/calls/{safe_id(audit['id'])}.json", {
            "call_id": audit["id"], "agent_id": agent_id, "agent_session_id": session_id,
            "job_id": job_id, "stage": stage, "recorded_at": now_iso(),
            "model_call_ref": f"model_calls/{audit['id']}.json", "response": body,
            "producer": "deepseek", "producer_role": "automation", "review_owner": "user",
            "review_state": "needs_review", "canonical_status": "record"})
        return body, audit
