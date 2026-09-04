"""假 sandbox-agent:进程内 uvicorn 起一个 ACP-over-HTTP 服务,脚本化各角色行为。

用途:不烧真模型就能测 guildhall 的管线(plumbing)。模型本身的判断力
(receptionist 拷问、appraiser 抓作弊)由真实 e2e 脚本验证,不在此列。
"""

from __future__ import annotations

import asyncio
import json
import socket
import threading
import uuid
from typing import Any, AsyncIterator, Awaitable, Callable, Optional

import httpx
import uvicorn
from fastapi import FastAPI, Request, Response
from fastapi.responses import JSONResponse, StreamingResponse

# 脚本签名:给定 prompt 文本,返回 (要发的 session/update 列表, 最终 agent 文本, stopReason)
Script = Callable[[str], Awaitable[tuple[list[dict[str, Any]], str, str]]]


def _free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


class FakeConn:
    def __init__(self, sid: str):
        self.sid = sid
        self.events: list[tuple[int, dict[str, Any]]] = []  # (id, envelope) 持久化以支持 Last-Event-ID
        self.subscribers: list[asyncio.Queue] = []
        self.next_event_id = 0
        self.closed = False
        self._pending: dict[int, dict[str, Any]] = {}  # request id -> envelope(延迟应答)
        self.active_prompts = 0

    def push(self, env: dict[str, Any]) -> int:
        self.next_event_id += 1
        self.events.append((self.next_event_id, env))
        for q in list(self.subscribers):
            q.put_nowait((self.next_event_id, env))
        return self.next_event_id

    def reset(self) -> None:
        """模拟 sandbox-agent 进程重启:事件序列从 1 重来。"""
        self.events.clear()
        self.next_event_id = 0


class FakeAcpServer:
    """POST/GET(SSE)/DELETE /v1/acp/{sid} + /v1/health + /v1/agents。"""

    def __init__(self) -> None:
        self.conns: dict[str, FakeConn] = {}
        self.prompts: list[tuple[str, str]] = []  # (sid, text) 收到的 prompt
        # 按连接名前缀路由脚本;默认脚本见 _default_script
        self.scripts: dict[str, Script] = {}
        self.on_prompt: Optional[Callable[[str, str], None]] = None  # 旁路钩子(测试用来污染 worktree)
        self.respond_via_post_body = False  # True 时 prompt 响应走 POST body,不走 SSE
        self.drop_prompt_response = False  # 模拟 adapter 吐完文本却丢失 stopReason
        self.hold_prompt_response = False  # 模拟 POST 本身保持连接，只有 SSE 在持续出事件
        self.agent_exit_code: Optional[int] = None  # 模拟 sandbox-agent 把 CLI 退出包装成 HTTP 500
        self.app = FastAPI()
        self._wire()
        self.port = _free_port()
        self.server = uvicorn.Server(uvicorn.Config(self.app, host="127.0.0.1", port=self.port, log_level="error"))
        self.thread: Optional[threading.Thread] = None

    # ---------- 生命周期 ----------

    def start(self) -> str:
        self.thread = threading.Thread(target=self.server.run, daemon=True)
        self.thread.start()
        for _ in range(100):
            try:
                r = httpx.get(f"{self.base_url}/v1/health", timeout=0.5)
                if r.status_code == 200:
                    return self.base_url
            except Exception:  # noqa: BLE001
                pass
            import time

            time.sleep(0.05)
        raise RuntimeError("fake acp server did not start")

    def stop(self) -> None:
        self.server.should_exit = True
        if self.thread:
            self.thread.join(timeout=5)

    @property
    def base_url(self) -> str:
        return f"http://127.0.0.1:{self.port}"

    # ---------- 测试装配 ----------

    def set_script(self, name_contains: str, script: Script) -> None:
        self.scripts[name_contains] = script

    def conn(self, sid: str) -> FakeConn:
        if sid not in self.conns:
            self.conns[sid] = FakeConn(sid)
        return self.conns[sid]

    # ---------- 路由 ----------

    def _wire(self) -> None:
        @self.app.get("/v1/health")
        async def health():
            return {"status": "ok"}

        @self.app.get("/v1/agents")
        async def agents():
            return {
                "agents": [
                    {"id": "claude", "installed": True, "credentialsAvailable": True, "capabilities": {}},
                ]
            }

        @self.app.post("/v1/acp/{sid}")
        async def post(sid: str, request: Request):
            env = await request.json()
            conn = self.conn(sid)
            method = env.get("method")
            rid = env.get("id")

            if method == "initialize":
                return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {"authMethods": [], "protocolVersion": 1}})
            if method == "session/new":
                return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {"sessionId": str(uuid.uuid4()), "modes": {}, "configOptions": []}})
            if method == "session/prompt":
                text = "".join(b.get("text", "") for b in env["params"].get("prompt", []) if isinstance(b, dict))
                self.prompts.append((sid, text))
                if self.on_prompt:
                    self.on_prompt(sid, text)
                conn.active_prompts += 1
                try:
                    updates, final_text, stop = await self._run_script(sid, text)
                    for u in updates:
                        conn.push({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": "fake", "update": u}})
                    resp_env = {"id": rid, "jsonrpc": "2.0", "result": {"stopReason": stop}}
                    if final_text:
                        # 最终 agent 文本按块发出,模拟流式
                        for i in range(0, len(final_text), 7):
                            conn.push({
                                "jsonrpc": "2.0",
                                "method": "session/update",
                                "params": {"sessionId": "fake", "update": {
                                    "sessionUpdate": "agent_message_chunk",
                                    "content": {"type": "text", "text": final_text[i : i + 7]},
                                    "messageId": "fake-msg",
                                }},
                            })
                    if self.respond_via_post_body:
                        return JSONResponse(resp_env)
                    if self.agent_exit_code is not None:
                        conn.push({
                            "jsonrpc": "2.0",
                            "method": "_adapter/agent_exited",
                            "params": {"agent": "claude", "exitCode": self.agent_exit_code},
                        })
                        return JSONResponse(
                            status_code=500,
                            content={
                                "type": "urn:sandbox-agent:error:agent_process_exited",
                                "title": "Agent Process Exited",
                                "status": 500,
                                "detail": "agent process exited: claude",
                                "agent": "claude",
                                "details": {"exitCode": self.agent_exit_code, "stderr": ""},
                            },
                        )
                    if self.hold_prompt_response:
                        while not await request.is_disconnected():
                            await asyncio.sleep(0.02)
                        return Response(status_code=499)
                    if self.drop_prompt_response:
                        return Response(status_code=202)
                    conn.push(resp_env)
                    return Response(status_code=202)
                finally:
                    conn.active_prompts = max(0, conn.active_prompts - 1)
            if method == "_session/steering":
                text = "".join(b.get("text", "") for b in env["params"].get("prompt", []) if isinstance(b, dict))
                if conn.active_prompts:
                    self.prompts.append((sid, text))
                    return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {"outcome": "injected"}})
                return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {"outcome": "promptRequired"}})
            if method == "authenticate":
                return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {}})
            if rid is not None:  # 其他请求:回空结果
                return JSONResponse({"id": rid, "jsonrpc": "2.0", "result": {}})
            return JSONResponse(None, status_code=202)

        @self.app.delete("/v1/acp/{sid}")
        async def delete(sid: str):
            conn = self.conn(sid)
            conn.closed = True
            for q in conn.subscribers:
                q.put_nowait(None)
            return Response(status_code=204)

        @self.app.get("/v1/acp/{sid}")
        async def sse(sid: str, request: Request):
            conn = self.conn(sid)
            last_event_id = request.headers.get("last-event-id")
            start_after = int(last_event_id) if last_event_id and last_event_id.isdigit() else 0

            async def gen() -> AsyncIterator[str]:
                q: asyncio.Queue = asyncio.Queue()
                # 先同步重放,再订阅,避免丢事件/重复
                replay = [(i, e) for i, e in conn.events if i > start_after]
                for i, e in replay:
                    yield sse_fmt(i, e)
                conn.subscribers.append(q)
                try:
                    last = replay[-1][0] if replay else start_after
                    while not conn.closed:
                        try:
                            item = await asyncio.wait_for(q.get(), timeout=1.0)
                        except asyncio.TimeoutError:
                            yield ": keepalive\n\n"
                            continue
                        if item is None:
                            break
                        i, e = item
                        if i <= last:
                            continue
                        last = i
                        yield sse_fmt(i, e)
                finally:
                    if q in conn.subscribers:
                        conn.subscribers.remove(q)

            return StreamingResponse(gen(), media_type="text/event-stream")

    async def _run_script(self, sid: str, text: str) -> tuple[list[dict[str, Any]], str, str]:
        for key, script in self.scripts.items():
            if key in sid:
                return await script(text)
        return await _default_script(text)


def sse_fmt(i: int, env: dict[str, Any]) -> str:
    return f"id: {i}\nevent: message\ndata: {json.dumps(env, ensure_ascii=False)}\n\n"


async def _default_script(text: str) -> tuple[list[dict[str, Any]], str, str]:
    return (
        [{"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "收到"}, "messageId": "m"}],
        "收到",
        "end_turn",
    )


def chunk(text: str, message_id: str = "m") -> dict[str, Any]:
    return {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": text}, "messageId": message_id}
