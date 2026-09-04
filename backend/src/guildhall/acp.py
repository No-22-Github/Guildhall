"""sandbox-agent ACP 客户端(§2.1 回填后版,见 docs/m0-sandbox-agent-api.md)。

0.4.2 的会话协议是 ACP JSON-RPC,跑在流式 HTTP 上:
- POST /v1/acp/{server_id}          —— 出站 envelope(首个 POST 带 ?agent=)
- GET  /v1/acp/{server_id}          —— SSE 入站,`id: N` 单调递增即 offset 游标
- DELETE /v1/acp/{server_id}        —— 关连接

硬约束照抄:server 用 --no-token 拉起(§2.1);Python 侧一律走 HTTP。
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
from typing import Any, AsyncIterator, Callable, Optional

import httpx

log = logging.getLogger("guildhall.acp")

JSONRPC_PARSE_ERROR = -32700
JSONRPC_METHOD_NOT_FOUND = -32601


class AcpError(Exception):
    def __init__(self, code: int, message: str, data: Any = None):
        self.code = code
        self.data = data
        super().__init__(message)


class SessionDied(AcpError):
    """连接/adapter 不可用,SSE 连续性也断了——quest 要停下交给人。"""


class AcpSession:
    """一条 ACP 连接 = 一个角色的 session。

    on_update 回调收到所有 agent → client 的通知(session/update 等),
    用于旁路写 jsonl 和向 Web 转发。turn 内的 agent_message_chunk 文本
    会累计进 turn_text,prompt 返回时即「本轮最终输出」。
    """

    def __init__(
        self,
        base_url: str,
        agent: str,
        session_name: str,
        *,
        mode: str = "default",
        resume_offset: int = 0,
        on_update: Optional[Callable[[dict[str, Any]], None]] = None,
        on_continuity_break: Optional[Callable[[str], None]] = None,
    ):
        self.base_url = base_url.rstrip("/")
        self.agent = agent
        self.session_name = session_name
        self.mode = mode
        self.on_update = on_update or (lambda env: None)
        self.on_continuity_break = on_continuity_break or (lambda msg: None)

        self.url = f"{self.base_url}/v1/acp/{session_name}"
        self._client = httpx.AsyncClient(timeout=httpx.Timeout(60.0), trust_env=False)  # 本机直连,不理会 http_proxy
        self._next_id = 1
        self._pending: dict[int, asyncio.Future] = {}
        self._agent_session_id: Optional[str] = None
        self._sse_task: Optional[asyncio.Task] = None
        self._last_event_id = resume_offset  # 续读游标:重启后从 state.json 快照恢复
        self._closed = False
        self._last_event_at = asyncio.get_running_loop().time()
        self._posted_once = False
        self._seen_response_ids: set[str] = set()
        self.turn_text: list[str] = []  # 本轮 agent_message_chunk 文本,按序拼接
        self._active_prompts = 0

    # ---------- 生命周期 ----------

    async def start(self) -> None:
        self._sse_task = asyncio.create_task(self._sse_loop(), name=f"sse:{self.session_name}")

    async def new_session(self, cwd: str) -> str:
        init = await self._request(
            "initialize",
            {"protocolVersion": 1, "clientInfo": {"name": "guildhall", "version": "0.1.0"}},
            bootstrap=True,
        )
        auth_methods = (init.get("result") or {}).get("authMethods") or []
        env_based = next(
            (m for m in auth_methods if m.get("id") in ("anthropic-api-key", "openai-api-key", "codex-api-key")),
            None,
        )
        if env_based:  # 官方 SDK 同款:只自动走 env 认证,交互式(claude-login)跳过
            try:
                await self._request("authenticate", {"methodId": env_based["id"]})
            except AcpError:
                pass  # 宿主 CLI 登录态可能已就绪,best-effort

        result = await self._request("session/new", {"cwd": cwd, "mcpServers": []})
        self._agent_session_id = result["sessionId"]
        if self.mode and self.mode != "default":
            await self._request("session/set_mode", {"sessionId": self._agent_session_id, "mode": self.mode})
        return self._agent_session_id

    async def prompt(self, text: str, *, timeout: float = 1800.0, idle_timeout: float | None = None) -> str:
        """投喂一轮。返回 stopReason;turn_text 累计本轮 agent 文本输出。

        轮末响应若在网络抖动中丢失,future 会永久悬空——用看门狗兜底:
        超时后 session/cancel 并报错,把 quest 交还给人,不许静默挂死。
        """
        if not self._agent_session_id:
            raise RuntimeError("session not created; call new_session() first")
        self.turn_text = []
        self._active_prompts += 1
        rid = self._next_id
        self._next_id += 1
        env = {"jsonrpc": "2.0", "id": rid, "method": "session/prompt",
               "params": {"sessionId": self._agent_session_id, "prompt": [{"type": "text", "text": text}]}}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        post_task: Optional[asyncio.Task[httpx.Response]] = asyncio.create_task(
            self._client.post(
                self.url,
                json=env,
                headers={"Accept": "application/json"},
                timeout=httpx.Timeout(3600.0, connect=10.0),
            ),
            name=f"prompt-post:{self.session_name}:{rid}",
        )

        async def stop_post() -> None:
            nonlocal post_task
            if post_task is None:
                return
            if not post_task.done():
                post_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await post_task
            post_task = None

        try:
            # 真实 sandbox-agent 可能把 POST 保持到整轮结束，同时把 token 从 SSE
            # 发出来。POST 必须与 future/空闲看门狗并行等待，否则 POST 自己挂住时
            # idle_timeout 永远没有机会执行。
            deadline = asyncio.get_running_loop().time() + timeout
            result = None
            while result is None:
                waiters: set[asyncio.Future] = {fut}
                if post_task is not None:
                    waiters.add(post_task)
                done, _ = await asyncio.wait(waiters, timeout=2.0, return_when=asyncio.FIRST_COMPLETED)

                if post_task is not None and post_task in done:
                    resp = post_task.result()
                    post_task = None
                    if resp.status_code in (202, 204):
                        pass
                    elif resp.status_code == 200:
                        body = resp.text.strip()
                        if body and body != "null":
                            self._maybe_resolve(json.loads(body))
                    elif self._is_clean_agent_exit(resp):
                        log.warning(
                            "session %s: agent 进程 exit 0 且已有正文，按轮结束处理",
                            self.session_name,
                        )
                        self._pending.pop(rid, None)
                        return "end_turn(agent_exit_0)"
                    else:
                        raise AcpError(-32000, f"session/prompt HTTP {resp.status_code}: {resp.text[:200]}")

                if fut.done():
                    result = fut.result()
                    break

                now = asyncio.get_running_loop().time()
                if now > deadline:
                    self._pending.pop(rid, None)
                    await stop_post()
                    with contextlib.suppress(Exception):
                        await self.cancel()
                    raise AcpError(-32000, f"turn 超过 {timeout:.0f}s 无响应,已发 cancel")
                if idle_timeout is not None and self.turn_text and now - self._last_event_at > idle_timeout:
                    log.warning(
                        "sse %s: 流空闲 %.0fs 且已有产出,按轮结束处理(响应可能已丢失)",
                        self.session_name, now - self._last_event_at,
                    )
                    self._pending.pop(rid, None)
                    await stop_post()
                    with contextlib.suppress(Exception):
                        await self.cancel()
                    return "end_turn(assumed)"
        finally:
            await stop_post()
            self._pending.pop(rid, None)
            self._active_prompts = max(0, self._active_prompts - 1)
        return result.get("stopReason", "end_turn")

    def _is_clean_agent_exit(self, response: httpx.Response) -> bool:
        """sandbox-agent 会把 Claude 正常 exit 0 包成 500；已有正文时等价于轮次完成。"""
        if response.status_code != 500 or not self.turn_text:
            return False
        try:
            body = response.json()
        except (json.JSONDecodeError, ValueError):
            return False
        return (
            str(body.get("type", "")).endswith(":agent_process_exited")
            and ((body.get("details") or {}).get("exitCode") == 0)
        )

    async def steer(self, text: str) -> str:
        """把补充消息注入正在运行的 Claude turn。

        claude-agent-acp 通过 `_session/steering` 暴露 Claude Code 的原生插话能力。
        空闲时要求调用方改走普通 prompt，避免 adapter 偷偷启动一个无法追踪生命周期的 turn。
        """
        if not self._agent_session_id:
            raise RuntimeError("session not created; call new_session() first")
        result = await self._request(
            "_session/steering",
            {
                "sessionId": self._agent_session_id,
                "prompt": [{"type": "text", "text": text}],
                "_meta": {"steering": {"idleBehavior": "promptRequired"}},
            },
        )
        return str((result or {}).get("outcome") or "promptRequired")

    async def cancel(self) -> None:
        if self._agent_session_id:
            await self._notify("session/cancel", {"sessionId": self._agent_session_id})

    async def close(self) -> None:
        self._closed = True
        for fut in self._pending.values():
            if not fut.done():
                fut.set_exception(AcpError(-32000, "session closed"))
        self._pending.clear()
        if self._sse_task:
            self._sse_task.cancel()
            try:
                await self._sse_task
            except (asyncio.CancelledError, Exception):
                pass
        try:
            await self._client.delete(self.url, timeout=3.0)
        except Exception:
            pass
        await self._client.aclose()

    # ---------- 出站 ----------

    async def _request(self, method: str, params: dict[str, Any], bootstrap: bool = False) -> Any:
        rid = self._next_id
        self._next_id += 1
        env = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params}
        fut: asyncio.Future = asyncio.get_running_loop().create_future()
        self._pending[rid] = fut
        try:
            url = self.url + ("?agent=" + self.agent if bootstrap and not self._posted_once else "")
            # prompt 的 POST 会挂到整轮结束(响应随 POST body 或 SSE 回来),
            # 真模型一轮可能跑几十分钟,单独给长超时
            timeout = httpx.Timeout(3600.0, connect=10.0) if method == "session/prompt" else None
            resp = await self._client.post(
                url,
                json=env,
                headers={"Accept": "application/json"},
                timeout=timeout,
            )
            if resp.status_code in (202, 204):
                pass  # 响应稍后从 SSE 来,等 future
            elif resp.status_code == 200:
                text = resp.text.strip()
                if text and text != "null":  # 响应随 POST body 回来;按 id 去重防双投
                    self._maybe_resolve(json.loads(text))
            else:
                raise AcpError(-32000, f"{method} HTTP {resp.status_code}: {resp.text[:200]}")
            return await fut
        finally:
            self._pending.pop(rid, None)

    async def _notify(self, method: str, params: dict[str, Any]) -> None:
        await self._client.post(self.url, json={"jsonrpc": "2.0", "method": method, "params": params})

    # ---------- 入站(SSE) ----------

    async def _sse_loop(self) -> None:
        """断线自动重连,Last-Event-ID 续读(§6.1:这是「真交互」的最低要求)。"""
        backoff = 0.3
        while not self._closed:
            try:
                headers = {"Accept": "text/event-stream"}
                if self._last_event_id:
                    headers["Last-Event-ID"] = str(self._last_event_id)
                # 读超时必须不设限:模型静默思考几分钟很常见,空闲断线会导致
                # 重连后丢事件(sandbox-agent 不重放),轮末响应丢失则整轮悬死。
                sse_timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=60.0)
                async with self._client.stream("GET", self.url, headers=headers, timeout=sse_timeout) as resp:
                    if resp.status_code != 200:
                        raise AcpError(-32000, f"SSE HTTP {resp.status_code}")
                    backoff = 0.3
                    async for chunk in self._sse_events(resp):
                        pass  # 消费在 _sse_events 内部完成
            except asyncio.CancelledError:
                return
            except Exception as e:  # noqa: BLE001
                if self._closed:
                    return
                log.warning("sse %s dropped (%s), retrying in %.1fs", self.session_name, e, backoff)
                await asyncio.sleep(backoff)
                backoff = min(backoff * 2, 5.0)

    async def _sse_events(self, resp: httpx.Response) -> AsyncIterator[None]:
        event_id: Optional[str] = None
        data_lines: list[str] = []
        expected_after_resume = self._last_event_id
        buffer = b""
        async for chunk in resp.aiter_bytes():
            buffer += chunk
            while b"\n" in buffer:
                line, _, buffer = buffer.partition(b"\n")
                text = line.decode("utf-8").rstrip("\r")
                if text.startswith(":"):
                    continue
                if text.startswith("id:"):
                    event_id = text[3:].strip()
                    continue
                if text.startswith("data:"):
                    data_lines.append(text[5:].lstrip())
                    continue
                if text == "" and data_lines:
                    self._handle_sse_event(event_id, "\n".join(data_lines), expected_after_resume)
                    event_id = None
                    data_lines = []
            if self._closed:
                return
            yield

    def _handle_sse_event(self, event_id: Optional[str], payload: str, expected_after_resume: int) -> None:
        if not payload.strip():
            return
        self._last_event_at = asyncio.get_running_loop().time()
        try:
            env = json.loads(payload)
        except json.JSONDecodeError:
            log.warning("non-json sse payload on %s: %r", self.session_name, payload[:100])
            return

        # 连续性守卫:sandbox-agent 重连后可能整条连接重建、id 从头计数。
        # 实测 adapter 会话本身仍活着(同一 sessionId),所以这里不做致命处理,
        # 而是重同步游标继续收——重建间隙丢的事件是 demo 可接受的代价。
        if event_id is not None and event_id.isdigit():
            n = int(event_id)
            if expected_after_resume and n <= expected_after_resume:
                log.warning(
                    "sse %s id counter reset (got %s, cursor was >%s): resyncing",
                    self.session_name, n, expected_after_resume,
                )
                expected_after_resume = 0
            self._last_event_id = n

        self._maybe_resolve(env)
        self._dispatch(env)

    def _maybe_resolve(self, env: dict[str, Any]) -> None:
        rid = env.get("id")
        if rid is None or "method" in env:
            return
        key = str(rid)
        if key in self._seen_response_ids:
            return
        self._seen_response_ids.add(key)
        fut = self._pending.pop(rid, None)
        if fut is None and isinstance(rid, str) and rid.isdigit():
            fut = self._pending.pop(int(rid), None)
        if fut and not fut.done():
            if "error" in env:
                err = env["error"]
                fut.set_exception(AcpError(err.get("code", -32603), err.get("message", "rpc error"), err.get("data")))
            else:
                fut.set_result(env.get("result"))

    def _dispatch(self, env: dict[str, Any]) -> None:
        method = env.get("method")
        if method is None:
            return  # 纯响应,已在 _maybe_resolve 处理
        if method == "session/request_permission":
            self._auto_approve(env)
            return
        if ("fs/" in (method or "")) or method.startswith("terminal/"):
            # 客户端侧文件/终端回调:claude adapter 不需要,回答 method-not-found
            if "id" in env:
                asyncio.get_running_loop().create_task(self._reply_error(env["id"]))
            return
        params = env.get("params") or {}
        update = params.get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            text = (update.get("content") or {}).get("text")
            if text:
                self.turn_text.append(text)
        self.on_update(env)

    def _auto_approve(self, env: dict[str, Any]) -> None:
        """无人值守策略:一律放行(选第一个 allow_* 选项)。"""
        params = env.get("params") or {}
        options = params.get("options") or []
        allow = next((o for o in options if str(o.get("kind", "")).startswith("allow")), None)
        outcome = {"outcome": "selected", "optionId": allow["optionId"]} if allow else {"outcome": "cancelled"}
        asyncio.get_running_loop().create_task(
            self._client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": env["id"], "result": {"outcome": outcome}},
                headers={"Accept": "application/json"},
            )
        )

    async def _reply_error(self, rid: Any) -> None:
        try:
            await self._client.post(
                self.url,
                json={"jsonrpc": "2.0", "id": rid, "error": {"code": JSONRPC_METHOD_NOT_FOUND, "message": "guildhall does not implement client fs/terminal callbacks"}},
            )
        except Exception:  # noqa: BLE001
            pass

    # ---------- 状态 ----------

    @property
    def offset(self) -> int:
        """当前上游 SSE 游标(state.json offsets 的来源)。"""
        return self._last_event_id

    @property
    def is_prompting(self) -> bool:
        return self._active_prompts > 0

    @property
    def seconds_since_event(self) -> float:
        return max(0.0, asyncio.get_running_loop().time() - self._last_event_at)
