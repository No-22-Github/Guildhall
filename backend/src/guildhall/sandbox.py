"""sandbox-agent server 生命周期(§5 autostart)。

guildhall-server 启动时拉起,父进程退出时一并杀掉;已在本机运行则复用、不动它。
"""

from __future__ import annotations

import asyncio
import logging
import subprocess

import httpx

from .acp import AcpSession
from .config import Config

log = logging.getLogger("guildhall.sandbox")


class SandboxManager:
    def __init__(self, config: Config):
        self.config = config
        self._proc: subprocess.Popen | None = None
        self._sessions: list[AcpSession] = []

    async def ensure_running(self) -> None:
        """健康检查 → 不通且 autostart 则 Popen 拉起并等健康。"""
        if await self._healthy():
            return
        if not self.config.sandbox_agent.autostart:
            raise RuntimeError(
                f"sandbox-agent 不在 {self.config.sandbox_agent.base_url} 且 autostart=false"
            )
        cmd = [
            self.config.sandbox_agent.binary,
            "server",
            "--no-token",  # 硬约束:不许配 token,demo 只监听 127.0.0.1
            "--host",
            self.config.sandbox_agent.host,
            "--port",
            str(self.config.sandbox_agent.port),
        ]
        log.info("starting sandbox-agent: %s", " ".join(cmd))
        self._proc = subprocess.Popen(cmd)
        for _ in range(120):  # 最多等 60s(首次要解压 agent 二进制)
            await asyncio.sleep(0.5)
            if await self._healthy():
                return
            if self._proc.poll() is not None:
                raise RuntimeError(f"sandbox-agent exited with code {self._proc.returncode}")
        raise RuntimeError("sandbox-agent did not become healthy in 60s")

    async def _healthy(self) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as c:
                r = await c.get(f"{self.config.sandbox_agent.base_url}/v1/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def assert_agent_available(self) -> None:
        """启动自检:确认配置的 agent 在列且已安装(§2.1 listAgents)。"""
        async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
            r = await c.get(f"{self.config.sandbox_agent.base_url}/v1/agents")
            r.raise_for_status()
            agents = {a["id"]: a for a in r.json().get("agents", [])}
        name = self.config.agent_name
        if name not in agents:
            raise RuntimeError(
                f"agent {name!r} 不在 sandbox-agent 的列表里: {sorted(agents)}。"
                "见 docs/m0-sandbox-agent-api.md 的勘误说明。"
            )
        if not agents[name].get("installed"):
            raise RuntimeError(f"agent {name!r} 未安装:运行 `sandbox-agent install-agent --all`")

    def open_session(self, session_name: str, *, resume_offset: int = 0, on_update=None, on_continuity_break=None) -> AcpSession:
        s = AcpSession(
            self.config.sandbox_agent.base_url,
            self.config.agent_name,
            session_name,
            mode=self.config.agent_mode,
            resume_offset=resume_offset,
            on_update=on_update,
            on_continuity_break=on_continuity_break,
        )
        self._sessions.append(s)
        return s

    async def shutdown(self) -> None:
        for s in self._sessions:
            try:
                await s.close()
            except Exception:  # noqa: BLE001
                pass
        self._sessions.clear()
        if self._proc and self._proc.poll() is None:
            self._proc.terminate()
            try:
                self._proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._proc.kill()
            log.info("sandbox-agent stopped")
        self._proc = None
