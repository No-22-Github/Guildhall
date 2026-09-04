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
from typing import Any, Optional

from . import store as st
from .acp import AcpError, AcpSession, SessionDied
from .sandbox import SandboxManager

log = logging.getLogger("guildhall.runtime")

ROLES = ("receptionist", "adventurer", "appraiser")


class RoleRuntime:
    """一个活着的角色 session + 它的事件扇出。"""

    def __init__(self, store: st.QuestStore, role: str, session: AcpSession):
        self.store = store
        self.role = role
        self.session = session
        self.subscribers: set[asyncio.Queue] = set()
        session.on_update = self._on_envelope
        self.died: Optional[str] = None

    # ---------- 扇出 ----------

    def _on_envelope(self, env: dict[str, Any]) -> None:
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

    async def send_user(self, text: str) -> None:
        self.publish_synthetic({"method": "_guildhall/user_message", "params": {"text": text}})
        await self.session.prompt(text)
        self.publish_synthetic({"method": "_guildhall/turn_end", "params": {}})
        self.flush_offset()

    async def send_system(self, text: str) -> str:
        """首条投喂(系统 prompt)。不写 user 气泡,但 turn_end 照发。"""
        stop = await self.session.prompt(text)
        self.publish_synthetic({"method": "_guildhall/turn_end", "params": {}})
        self.flush_offset()
        return stop

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
        self.closing = False

    # ---------- 角色会话 ----------

    async def open_role(self, role: str, cwd: str, first_message: str, *, system: bool = False) -> RoleRuntime:
        state = self.store.read_state()
        name = state["sessions"].get(role) or f"guildhall-{self.store.quest_id}-{role}"
        session = self.manager.open_session(
            name,
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
        if system:
            # 系统轮必须先收尾再发开场白:排队并发会导致 adapter 重复流式输出
            await self._send_first(rt, first_message)
        else:
            task = asyncio.create_task(self._safe_send_user(rt, first_message))
            self.tasks.add(task)
            task.add_done_callback(self.tasks.discard)
        return rt

    async def _send_first(self, rt: RoleRuntime, text: str) -> None:
        try:
            await rt.send_system(text)
        except Exception as e:  # noqa: BLE001
            self._record_role_error(rt.role, e)

    async def _safe_send_user(self, rt: RoleRuntime, text: str) -> None:
        try:
            await rt.send_user(text)
        except Exception as e:  # noqa: BLE001
            self._record_role_error(rt.role, e)

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

    async def close_all(self) -> None:
        self.closing = True
        for rt in self.roles.values():
            await rt.close()
        self.roles.clear()
