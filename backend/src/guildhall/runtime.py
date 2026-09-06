"""quest 运行时:角色 session 的生命周期、事件旁路与扇出(§3/§6.1)。

事件通路(单向,不做二次缓存):
    sandbox-agent SSE ──► publish() ──┬── events/<role>.jsonl(旁路,必须)
                                      ├── state.json offsets(上游游标快照)
                                      └── 全部 Web 订阅者(SSE 逐事件转发,不许缓冲)

Web 端 offset 语义 = jsonl 行号;state.json offsets 语义 = 上游 SSE 游标(重启续读)。
两个游标各司其职,互不去重。
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from typing import Any, Callable, Optional

from . import store as st
from .acp import AcpError, AcpSession, SessionDied
from .sandbox import SandboxManager

log = logging.getLogger("guildhall.runtime")

ROLES = ("receptionist", "adventurer", "appraiser")

# 思考文本只保留尾部进运行态(§2.3.1):全量会把每次心跳撑爆,
# 完整内容在 events/<role>.jsonl 里,前端需要时可从事件流取。
THOUGHT_TAIL_CHARS = 2000


class RoleRuntime:
    """一个活着的角色 session + 它的事件扇出。"""

    def __init__(self, store: st.QuestStore, role: str, session: AcpSession):
        self.store = store
        self.role = role
        self.session = session
        self.subscribers: set[asyncio.Queue] = set()
        session.on_update = self._on_envelope
        # turn 结束回调:(turn_id, kind, turn 开始时的 quest.md 快照)。
        # flow 层用来 reconcile「Agent 在对话轮里直接写盘的 quest.md」。
        self.on_turn_end: Optional[Callable[[str, str, Optional[str]], None]] = None
        self.died: Optional[str] = None
        self.active_turns = 0
        self.activity = "waiting"
        self.activity_detail: Optional[str] = None
        self.active_tool: Optional[dict[str, Any]] = None
        self.context_used: Optional[int] = None
        self.context_size: Optional[int] = None
        self.thought_tail: Optional[str] = None
        self.last_event_at = time.time()

    # ---------- 扇出 ----------

    def _on_envelope(self, env: dict[str, Any]) -> None:
        self.last_event_at = time.time()
        update = (env.get("params") or {}).get("update") or {}
        kind = update.get("sessionUpdate")
        if kind == "agent_thought_chunk":
            text = (update.get("content") or {}).get("text")
            if text:
                self.thought_tail = ((self.thought_tail or "") + text)[-THOUGHT_TAIL_CHARS:]
            self.activity = "thinking"
            self.activity_detail = None
        elif kind == "agent_message_chunk":
            self.thought_tail = None  # 新一轮输出开始了
            self.activity = "responding"
            self.activity_detail = None
        elif kind in ("tool_call", "tool_call_update"):
            tool_name = ((update.get("_meta") or {}).get("claudeCode") or {}).get("toolName")
            title = update.get("title")
            status = update.get("status")
            self.active_tool = {
                "id": update.get("toolCallId"),
                "name": tool_name or title or "工具",
                "title": title,
                "status": status or "pending",
            }
            if status == "completed":
                self.activity = "thinking"
                self.activity_detail = None
                self.active_tool = None
            else:
                self.activity = "tool"
                self.activity_detail = title or tool_name
        elif kind == "usage_update":
            self.context_used = update.get("used")
            self.context_size = update.get("size")
        line = self.store.append_event(self.role, env)
        for q in list(self.subscribers):
            q.put_nowait((line, env))

    def subscribe(self) -> asyncio.Queue:
        q: asyncio.Queue = asyncio.Queue()
        self.subscribers.add(q)
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        self.subscribers.discard(q)

    def flush_offset(self) -> None:
        """把上游 SSE 游标快照进 state.json(§6.1:快照,不是权威)。"""
        try:
            self.store.update_state_offsets(self.role, self.session.offset)
        except Exception:  # noqa: BLE001
            log.exception("flush offset failed for %s/%s", self.store.quest_id, self.role)

    def publish_synthetic(self, env: dict[str, Any]) -> int:
        """用户消息等本地事件:只进 jsonl 和 Web 流,不动上游游标。"""
        line = self.store.append_event(self.role, env)
        for q in list(self.subscribers):
            q.put_nowait((line, env))
        return line

    # ---------- 交互 ----------

    def begin_prompt(
        self,
        prompt_text: str,
        *,
        visible_user_text: Optional[str] = None,
        kind: str = "chat",
        idle_timeout: float | None = None,
    ) -> asyncio.Task[str]:
        """启动一个可观测 turn；调用方无需把 HTTP 请求挂到整轮结束。"""
        turn_id = uuid.uuid4().hex
        if visible_user_text:
            self.publish_synthetic(
                {"method": "_guildhall/user_message", "params": {"text": visible_user_text, "turnId": turn_id}}
            )
        self.active_turns += 1
        self.activity = "starting"
        self.activity_detail = None
        self.last_event_at = time.time()
        self.publish_synthetic(
            {"method": "_guildhall/turn_start", "params": {"turnId": turn_id, "kind": kind}}
        )

        async def run() -> str:
            try:
                before_quest_md = self.store.read_quest_md()
            except Exception:  # noqa: BLE001
                before_quest_md = None
            stop_reason = "end_turn"
            try:
                stop_reason = await self.session.prompt(prompt_text, idle_timeout=idle_timeout)
                return "".join(self.session.turn_text).strip()
            except Exception as e:  # noqa: BLE001
                self.died = str(e)
                self.activity = "error"
                self.activity_detail = str(e)
                self.publish_synthetic(
                    {"method": "_guildhall/turn_error", "params": {"turnId": turn_id, "kind": kind, "error": str(e)}}
                )
                raise
            finally:
                self.active_turns = max(0, self.active_turns - 1)
                if self.active_turns == 0 and self.activity != "error":
                    self.activity = "waiting"
                    self.activity_detail = None
                    self.active_tool = None
                self.publish_synthetic(
                    {
                        "method": "_guildhall/turn_end",
                        "params": {"turnId": turn_id, "kind": kind, "stopReason": stop_reason},
                    }
                )
                self.flush_offset()
                if self.on_turn_end is not None:
                    try:
                        self.on_turn_end(turn_id, kind, before_quest_md)
                    except Exception:  # noqa: BLE001
                        log.exception("on_turn_end callback failed for %s/%s", self.store.quest_id, self.role)

        return asyncio.create_task(run(), name=f"{kind}:{self.store.quest_id}:{turn_id[:8]}")

    async def submit_user(self, text: str, *, system: bool = False) -> tuple[str, Optional[asyncio.Task[str]]]:
        """优先插入当前 Claude turn；空闲时再启动一个普通 turn。

        system=True 标记服务端转达的消息(如 quest.md 校验失败原因):事件流里
        带 system 字段,前端按「系统转达」样式渲染,不冒充用户原话。
        """
        turn_id = uuid.uuid4().hex
        params: dict[str, Any] = {"text": text, "turnId": turn_id}
        if system:
            params["system"] = True
        self.publish_synthetic({"method": "_guildhall/user_message", "params": params})
        outcome = await self.session.steer(text)
        if outcome == "promptRequired":
            return outcome, self.begin_prompt(text, kind="chat")
        self.activity = "steering"
        self.activity_detail = "正在处理你的补充"
        self.last_event_at = time.time()
        self.publish_synthetic(
            {"method": "_guildhall/steered", "params": {"turnId": turn_id, "outcome": outcome}}
        )
        return outcome, None

    def status(self) -> dict[str, Any]:
        seconds = max(0.0, time.time() - self.last_event_at)
        busy = self.active_turns > 0 or self.session.is_prompting
        return {
            "connected": True,
            "busy": busy,
            "activity": self.activity if busy or self.activity == "error" else "waiting",
            "detail": self.activity_detail,
            "active_tool": self.active_tool,
            "seconds_since_event": round(seconds, 1),
            "context_used": self.context_used,
            "context_size": self.context_size,
            "thought_tail": self.thought_tail,
            "error": self.died,
        }

    async def close(self) -> None:
        try:
            await self.session.close()
        except Exception:  # noqa: BLE001
            pass


class QuestRuntime:
    """一个 quest 的全部活角色 + 流程编排入口。"""

    def __init__(self, store: st.QuestStore, manager: SandboxManager):
        self.store = store
        self.manager = manager
        self.roles: dict[str, RoleRuntime] = {}
        self.tasks: set[asyncio.Task] = set()
        self.generation_task: Optional[asyncio.Task] = None
        self.closing = False
        self.phase_task: Optional[asyncio.Task] = None
        # quest.md 校验失败连续自动回喂 receptionist 的次数(上限见 flow.QUEST_MD_FEEDBACK_CAP,
        # 通过后清零;防止坏模型与服务端互相无限拉扯)。
        self.quest_md_feedback_streak = 0

    def phase_busy(self) -> bool:
        return self.phase_task is not None and not self.phase_task.done()

    def start_phase(self, factory) -> None:
        if self.phase_busy():
            raise RuntimeError("当前阶段仍在运行，请等待结束后再恢复")
        async def run():
            try:
                await factory()
            except Exception as exc:
                self.store.set_error(f"阶段启动或执行失败:{exc}")
                state = self.store.read_state()["state"]
                if state in ("in_progress", "appraising"):
                    self.store.transition("failed" if state == "in_progress" else "disputed")
        self.phase_task = asyncio.create_task(run(), name=f"phase:{self.store.quest_id}")
        self.track(self.phase_task)

    # ---------- 角色会话 ----------

    async def open_role(self, role: str, cwd: str) -> RoleRuntime:
        state = self.store.read_state()
        name = state["sessions"].get(role) or f"guildhall-{self.store.quest_id}-{role}"
        session = self.manager.open_session(
            name,
            role=role,
            resume_offset=int(state["offsets"].get(role) or 0),
            on_continuity_break=self._make_continuity_handler(role),
        )
        await session.start()
        try:
            await session.new_session(cwd)
        except (AcpError, SessionDied):
            await session.close()
            raise
        rt = RoleRuntime(self.store, role, session)
        self.roles[role] = rt
        # state.json 记 session 名(命名规则逐字:guildhall-<quest-id>-<role>)
        self.store.update_state(sessions={**state["sessions"], role: name})
        return rt

    def track(self, task: asyncio.Task) -> None:
        self.tasks.add(task)

        def done(finished: asyncio.Task) -> None:
            self.tasks.discard(finished)
            if finished.cancelled():
                return
            error = finished.exception()
            if error is not None:
                log.error("background task %s failed: %s", finished.get_name(), error)
                self.store.set_error(f"后台任务异常:{error}")

        task.add_done_callback(done)

    def _record_role_error(self, role: str, e: Exception) -> None:
        rt = self.roles.get(role)
        if rt:
            rt.died = str(e)
        self.store.set_error(f"{role} session 异常:{e}")

    def _make_continuity_handler(self, role: str):
        def handler(msg: str) -> None:
            log.error("continuity break on %s/%s: %s", self.store.quest_id, role, msg)
            rt = self.roles.get(role)
            if rt:
                rt.died = msg
            self.store.set_error(msg)

        return handler

    def get_role(self, role: str) -> Optional[RoleRuntime]:
        return self.roles.get(role)

    def status(self) -> dict[str, Any]:
        return {role: rt.status() for role, rt in self.roles.items()}

    async def close_all(self) -> None:
        self.closing = True
        tasks = [t for t in self.tasks if t is not asyncio.current_task() and not t.done()]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        for rt in self.roles.values():
            await rt.close()
        self.roles.clear()
