"""HTTP 接口(§3)。所有路由前缀 /api。「不得」清单逐条执行:

- transition 不接受表外转移 → 409
- events 逐事件转发,不缓冲整流;旁路写 jsonl 并更新 offsets
- in_progress 状态下任何路由不得改 quest.md → 409
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, AsyncIterator, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

from . import flow, gitutil, layout, statemachine as sm, store as st
from .runtime import QuestRuntime, RoleRuntime
from .sandbox import SandboxManager

log = logging.getLogger("guildhall.api")


class AppContext:
    """应用级单例:配置、sandbox 管理器、活 quest 运行时。"""

    def __init__(self, config):
        self.config = config
        self.manager = SandboxManager(config)
        self.runtimes: dict[str, QuestRuntime] = {}

    def runtime(self, store: st.QuestStore) -> QuestRuntime:
        rt = self.runtimes.get(store.quest_id)
        if rt is None:
            rt = QuestRuntime(store, self.manager)
            self.runtimes[store.quest_id] = rt
        return rt

    async def shutdown(self) -> None:
        for rt in self.runtimes.values():
            await rt.close_all()
        self.runtimes.clear()
        await self.manager.shutdown()


ctx: Optional[AppContext] = None


def get_ctx() -> AppContext:
    assert ctx is not None, "app context not initialized"
    return ctx


def set_context(context: AppContext) -> FastAPI:
    """lifespan 装配真正的运行时上下文。"""
    global ctx
    ctx = context
    return build_app(context)


def _store(quest_id: str) -> st.QuestStore:
    q = st.find_quest(quest_id)
    if q is None:
        raise HTTPException(404, f"quest not found: {quest_id}")
    return q


def _guard_quest_md_writable(state: str) -> None:
    if state == sm.IN_PROGRESS:
        raise HTTPException(409, "in_progress 状态下不许改 quest.md")


# ─────────────────────────────────────────────────────────────────────────────
# models
# ─────────────────────────────────────────────────────────────────────────────


class ProjectIn(BaseModel):
    path: str


class QuestIn(BaseModel):
    project: str
    message: Optional[str] = None


class MessageIn(BaseModel):
    text: str


class TransitionIn(BaseModel):
    to: str


# ─────────────────────────────────────────────────────────────────────────────
# routes
# ─────────────────────────────────────────────────────────────────────────────


def build_app(context: AppContext) -> FastAPI:
    global ctx
    if ctx is None:
        ctx = context
    app = FastAPI(title="guildhall-server", version="0.1.0")
    app.add_middleware(
        CORSMiddleware,
        allow_origins=["*"],  # demo:本机单用户,无鉴权
        allow_methods=["*"],
        allow_headers=["*"],
    )

    # ---- projects ----

    @app.get("/api/projects")
    async def list_projects() -> list[dict[str, Any]]:
        return [{"path": p} for p in layout.list_projects()]

    @app.post("/api/projects", status_code=201)
    async def register_project(body: ProjectIn) -> dict[str, str]:
        try:
            layout.register_project(body.path)
        except ValueError as e:
            raise HTTPException(400, str(e))
        return {"path": body.path}

    # ---- quests ----

    @app.get("/api/quests")
    async def list_quests(project: str) -> list[dict[str, Any]]:
        return st.list_quests(project)

    @app.post("/api/quests", status_code=201)
    async def create_quest(body: QuestIn) -> dict[str, Any]:
        if body.project not in layout.list_projects():
            raise HTTPException(400, f"project 未注册: {body.project}")
        store = st.QuestStore.create(body.project, body.message or "draft")
        rt = get_ctx().runtime(store)
        await flow.start_receptionist(rt, body.message)
        return {"id": store.quest_id, "state": sm.DRAFTING}

    @app.get("/api/quests/stream")
    async def quests_stream(request: Request) -> StreamingResponse:
        """列表页 SSE(§2.2):全部项目的 quest 摘要,全量推送,同样最长 5s 心跳。

        注册顺序注意:必须在 /api/quests/{quest_id} 之前,否则 "stream" 会被
        当成 quest_id 抓走。
        """
        async def gen() -> AsyncIterator[str]:
            loop = asyncio.get_running_loop()
            last_sig: Optional[str] = None
            last_push = 0.0
            while True:
                if await request.is_disconnected():
                    return
                data = st.list_all_quests()
                sig = json.dumps(data, sort_keys=True, ensure_ascii=False)
                now = loop.time()
                if sig != last_sig or now - last_push >= 4.5:
                    last_sig, last_push = sig, now
                    yield f"data: {json.dumps(data, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.get("/api/quests/{quest_id}")
    async def get_quest(quest_id: str) -> dict[str, Any]:
        store = _store(quest_id)
        runtime = get_ctx().runtimes.get(quest_id)
        return {
            "quest_md": store.read_quest_md(),
            "state": store.read_state(),
            "appraisal": store.read_appraisal(),
            "runtime": runtime.status() if runtime is not None else {},
        }

    @app.get("/api/quests/{quest_id}/status/stream")
    async def quest_status_stream(quest_id: str, request: Request) -> StreamingResponse:
        """状态/运行态 SSE(§2.2):当前值,不做游标。

        每次推送都是全量快照;断线重连后 EventSource 收到的第一条就是当前全量,
        前端不需要补任何历史。quest_md 只在连接建立与状态转移的推送里带
        (它可能很大,心跳/运行态变化不带)。变化检测排除 seconds_since_event
        ——它只随时间走动,由最长 5s 一次的心跳负责刷新。
        """
        store = _store(quest_id)

        async def gen() -> AsyncIterator[str]:
            loop = asyncio.get_running_loop()
            last_sig: Optional[str] = None
            last_state: Optional[str] = None
            last_qmd: Optional[str] = None
            last_push = 0.0
            first = True
            while True:
                if await request.is_disconnected():
                    return
                runtime = get_ctx().runtimes.get(quest_id)
                status = runtime.status() if runtime is not None else {}
                status_for_sig = {
                    role: {k: v for k, v in s.items() if k != "seconds_since_event"}
                    if isinstance(s, dict) else s
                    for role, s in status.items()
                }
                state = store.read_state()
                qmd = store.read_quest_md()
                payload = {"state": state, "appraisal": store.read_appraisal(), "runtime": status}
                # quest_md 必须进变化检测:生成/手改都发生在 drafting 内,不伴随状态转移,
                # 不进签名的话前端永远收不到新需求单(心跳又刻意不带它)。
                sig = json.dumps(
                    {"state": state, "appraisal": payload["appraisal"], "runtime": status_for_sig, "quest_md": qmd},
                    sort_keys=True,
                    ensure_ascii=False,
                    default=str,
                )
                now = loop.time()
                if sig != last_sig:
                    body = dict(payload)
                    if first or state["state"] != last_state or qmd != last_qmd:
                        body["quest_md"] = qmd
                    last_sig, last_state, last_qmd, last_push, first = sig, state["state"], qmd, now, False
                    yield f"data: {json.dumps(body, ensure_ascii=False)}\n\n"
                elif now - last_push >= 4.5:  # 心跳:最长 5s 一次(0.5s 轮询粒度)
                    last_push = now
                    yield f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"
                await asyncio.sleep(0.5)

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.put("/api/quests/{quest_id}/quest")
    async def put_quest(quest_id: str, request: Request) -> dict[str, str]:
        store = _store(quest_id)
        _guard_quest_md_writable(store.read_state()["state"])
        text = (await request.body()).decode("utf-8")
        if not text.strip():
            raise HTTPException(400, "quest.md 不能置空")
        store.write_quest_md(text)
        return {"ok": "true"}

    @app.post("/api/quests/{quest_id}/message")
    async def post_message(quest_id: str, body: MessageIn) -> dict[str, Any]:
        store = _store(quest_id)
        state = store.read_state()
        if state["state"] != sm.DRAFTING:
            raise HTTPException(409, f"只有 drafting 状态能对话(当前 {state['state']})")
        rt = get_ctx().runtime(store)
        if flow.is_generation_request(body.text):
            result = flow.request_generate_quest(
                rt,
                prompt_override=body.text,
                visible_user_text=body.text,
                force_regenerate=True,
            )
            if not result.get("ok"):
                raise HTTPException(409, result.get("reason") or "无法开始生成需求单")
            return result
        receiver = rt.get_role("receptionist")
        if receiver is None:
            # 服务重启后 runtime 丢失:重建 session(从 offsets 续),再投喂
            await flow.start_receptionist(rt, body.text)
        else:
            try:
                await flow.continue_chat(rt, body.text)
            except Exception as e:  # noqa: BLE001
                raise HTTPException(502, f"receptionist session 异常:{e}")
        return {"ok": True}

    @app.post("/api/quests/{quest_id}/generate")
    async def generate(quest_id: str) -> dict[str, Any]:
        """「生成需求单」按钮:触发 receptionist 产出并落盘 quest.md。

        (§3 表格外的辅助路由:否则服务端无法从聊天流里可靠截出 quest.md。)
        """
        store = _store(quest_id)
        rt = get_ctx().runtime(store)
        return flow.request_generate_quest(rt)

    @app.get("/api/quests/{quest_id}/events/{role}")
    async def events(quest_id: str, role: str, request: Request, offset: int = -1) -> StreamingResponse:
        if role not in ("receptionist", "adventurer", "appraiser"):
            raise HTTPException(404, f"unknown role: {role}")
        store = _store(quest_id)
        # 浏览器 EventSource 断线重连自动带 Last-Event-ID;query offset 优先
        if offset < 0:
            lei = request.headers.get("last-event-id", "")
            offset = int(lei) if lei.isdigit() else 0
        rt = get_ctx().runtime(store)
        rt_role: Optional[RoleRuntime] = rt.roles.get(role)

        async def gen() -> AsyncIterator[str]:
            # 1) 重放 jsonl(权威持久层)到当前行数
            lines = store.read_events(role, offset)
            for i, env in enumerate(lines):
                yield sse_line(offset + i, env)
            last = offset + len(lines)
            # 2) 订阅活流,跳过重放已覆盖的行,只透传新事件
            if rt_role is not None:
                q = rt_role.subscribe()
                try:
                    while True:
                        line_no, env = await q.get()
                        if line_no < last:
                            continue
                        yield sse_line(line_no, env)
                        last = line_no + 1
                finally:
                    rt_role.unsubscribe(q)
            else:
                yield "event: end\ndata: {}\n\n"  # 没有活 session:重放完即止

        return StreamingResponse(
            gen(),
            media_type="text/event-stream",
            headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
        )

    @app.post("/api/quests/{quest_id}/transition")
    async def transition(quest_id: str, body: TransitionIn) -> dict[str, Any]:
        store = _store(quest_id)
        state = store.read_state()
        to = body.to
        # 表外转移必须 409(§3 不得清单)
        try:
            sm.assert_transition(state["state"], to)
        except sm.InvalidTransition as e:
            raise HTTPException(409, str(e))

        # 转移前副作用闸门
        if to == sm.POSTED:
            _guard_quest_md_writable(state["state"])
        if to == sm.IN_PROGRESS:
            busy = st.any_in_progress()
            if busy and busy != quest_id:
                raise HTTPException(409, f"同一时刻只允许一个 quest 处于 in_progress(当前:{busy})")

        try:
            new_state = store.transition(to)
        except st.GateRejected as e:
            raise HTTPException(409, e.reason)

        # 转移后触发流程
        if to == sm.POSTED:
            flow.finalize_receptionist_integrity(store)
            rt = get_ctx().runtime(store)
            await rt.close_all()
        elif to == sm.IN_PROGRESS:
            rt = get_ctx().runtime(store)
            asyncio.create_task(flow.dispatch_adventurer(rt, get_ctx().manager))
        elif to == sm.APPRAISING:
            # 服务重启恢复路径:人工把 in_progress 推进到 appraising 时,补跑验收
            rt = get_ctx().runtime(store)
            if not any(t.get_name() == f"appraise:{quest_id}" for t in rt.tasks):
                asyncio.create_task(_named_appraise(rt, quest_id))
        elif to in (sm.SETTLED, sm.WITHDRAWN):
            rt = get_ctx().runtime(store)
            await rt.close_all()

        return {"state": new_state["state"]}

    @app.post("/api/quests/{quest_id}/reappraise")
    async def reappraise(quest_id: str) -> dict[str, Any]:
        """服务重启于 appraising 期间或验收流程中断后的恢复入口:重跑验收。

        仅 appraising 状态可用;表内转移之外的状态一律 409。
        (§3 表格外的辅助路由,与 /generate 同理。)
        """
        store = _store(quest_id)
        if store.read_state()["state"] != sm.APPRAISING:
            raise HTTPException(409, "只有 appraising 状态能重跑验收")
        rt = get_ctx().runtime(store)
        asyncio.create_task(_named_appraise(rt, quest_id))
        return {"ok": True}

    @app.post("/api/quests/{quest_id}/retry-adventurer")
    async def retry_adventurer(quest_id: str) -> dict[str, Any]:
        """恢复因后端/sandbox-agent 断开而失败的冒险者，复用原 worktree。"""
        store = _store(quest_id)
        state = store.read_state()
        if state.get("state") != sm.FAILED:
            raise HTTPException(409, "只有 failed 状态能重试冒险者")
        failure = state.get("history", [])[-1] if state.get("history") else {}
        if failure.get("from") != sm.IN_PROGRESS:
            raise HTTPException(409, "该失败不是发生在冒险者阶段，不能从这里重试")
        busy = st.any_in_progress()
        if busy and busy != quest_id:
            raise HTTPException(409, f"同一时刻只允许一个 quest 处于 in_progress(当前:{busy})")
        try:
            new_state = store.retry_failed(sm.IN_PROGRESS)
        except RuntimeError as e:
            raise HTTPException(409, str(e))
        rt = get_ctx().runtime(store)
        asyncio.create_task(flow.dispatch_adventurer(rt, get_ctx().manager, reuse_worktree=True))
        return {"ok": True, "state": new_state["state"]}

    @app.post("/api/quests/{quest_id}/resume-appraisal")
    async def resume_appraisal(quest_id: str) -> dict[str, Any]:
        """冒险者已产出并正常退出但被误判失败时，跳过重复执行，直接开始验收。"""
        store = _store(quest_id)
        state = store.read_state()
        if state.get("state") != sm.FAILED:
            raise HTTPException(409, "只有 failed 状态能恢复到验收")
        failure = state.get("history", [])[-1] if state.get("history") else {}
        if failure.get("from") != sm.IN_PROGRESS:
            raise HTTPException(409, "该失败不是发生在冒险者阶段，不能直接开始验收")
        if not state.get("base_commit") or not layout.worktree_path(quest_id).is_dir():
            raise HTTPException(409, "worktree 或基点不存在，不能直接开始验收")
        events = store.read_events("adventurer")
        has_output = any(
            ((event.get("params") or {}).get("update") or {}).get("sessionUpdate") == "agent_message_chunk"
            for event in events
        )
        clean_exit = any(
            event.get("method") == "_adapter/agent_exited"
            and (
                (event.get("params") or {}).get("success") is True
                or (event.get("params") or {}).get("exitCode") == 0
                or (event.get("params") or {}).get("code") == 0
            )
            for event in events
        )
        if not has_output or not clean_exit:
            raise HTTPException(409, "没有找到冒险者完整输出与正常退出证据，请使用“重试冒险者”")
        try:
            store.retry_failed(sm.IN_PROGRESS)
            new_state = store.transition(sm.APPRAISING)
        except (RuntimeError, sm.InvalidTransition) as e:
            raise HTTPException(409, str(e))
        rt = get_ctx().runtime(store)
        asyncio.create_task(_named_appraise(rt, quest_id))
        return {"ok": True, "state": new_state["state"]}

    @app.get("/api/quests/{quest_id}/diff")
    async def quest_diff(quest_id: str) -> dict[str, Any]:
        store = _store(quest_id)
        state = store.read_state()
        wt = state.get("worktree")
        if not wt or not layout.worktree_path(store.quest_id).exists():
            return {"diff": "", "base_commit": state.get("base_commit")}
        from pathlib import Path

        return {
            "diff": gitutil.diff_vs_base(Path(wt), state.get("base_commit")),
            "base_commit": state.get("base_commit"),
        }

    @app.get("/api/quests/{quest_id}/appraisal")
    async def quest_appraisal(quest_id: str) -> dict[str, Any]:
        store = _store(quest_id)
        data = store.read_appraisal()
        if data is None:
            raise HTTPException(404, "appraisal.json 尚未生成")
        return data

    return app


async def _named_appraise(rt: QuestRuntime, quest_id: str) -> None:
    task = asyncio.current_task()
    if task:
        task.set_name(f"appraise:{quest_id}")
    await flow.run_appraisal(rt, get_ctx().manager)


def sse_line(seq: int, env: dict[str, Any]) -> str:
    return f"id: {seq}\ndata: {json.dumps(env, ensure_ascii=False)}\n\n"
