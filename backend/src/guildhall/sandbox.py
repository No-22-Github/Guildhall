"""sandbox-agent server 生命周期(§5 autostart;M1 per-role 分流见 docs/m1-per-role-model.md)。

方案 B:有独立 env 的角色各起一个实例(端口从 base_port 起按角色顺序分配),
env 完全相同的角色复用同一实例;全部角色无独立配置时只有 base_port 一个实例
(即旧行为:已在本机运行则复用、不动它)。父进程退出时一并杀掉。

auth_token 只进子进程 env,任何日志/异常消息都不许出现它。
"""

from __future__ import annotations

import asyncio
import logging
import os
import subprocess

import httpx

from .acp import AcpSession
from .config import Config, ROLES

log = logging.getLogger("guildhall.sandbox")


class SandboxInstance:
    """一个 sandbox-agent server 实例:端口 + 角色环境变量 + 可选的进程句柄。"""

    def __init__(self, host: str, port: int, env_vars: dict[str, str]):
        self.host = host
        self.port = port
        self.env_vars = env_vars  # 只含分流相关键;拉起时叠加在 os.environ 上
        self.proc: subprocess.Popen | None = None

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


class SandboxManager:
    def __init__(self, config: Config):
        self.config = config
        self._instances: dict[tuple, SandboxInstance] = {}  # env 签名 → 实例(含端口分配)
        self._role_signature: dict[str, tuple] = {}  # role → env 签名
        self._sessions: list[AcpSession] = []

    # ---------- 实例规划 ----------

    def plan(self) -> None:
        """按角色顺序给每个不同 env 签名分配端口(base_port 起 +1)。幂等。

        无独立配置的角色共享空签名实例(base_port,兼容旧单实例部署)。
        """
        next_port = self.config.sandbox_agent.port
        for role in ROLES:
            rc = self.config.role_config(role)
            sig = rc.signature()
            self._role_signature[role] = sig
            if sig in self._instances:
                continue
            self._instances[sig] = SandboxInstance(
                host=self.config.sandbox_agent.host, port=next_port, env_vars=rc.env_vars()
            )
            next_port += 1

    def instance_for(self, role: str) -> SandboxInstance:
        self.plan()
        return self._instances[self._role_signature[role]]

    def instances(self) -> list[SandboxInstance]:
        self.plan()
        return list(self._instances.values())

    # ---------- 生命周期 ----------

    async def ensure_running(self) -> None:
        """逐实例健康检查 → 不通且 autostart 则 Popen 拉起并等健康。"""
        for inst in self.instances():
            if await self._healthy(inst):
                log.info("sandbox-agent already running at %s (reusing)", inst.base_url)
                continue
            if not self.config.sandbox_agent.autostart:
                raise RuntimeError(f"sandbox-agent 不在 {inst.base_url} 且 autostart=false")
            cmd = [
                self.config.sandbox_agent.binary,
                "server",
                "--no-token",  # 硬约束:不许配 token,demo 只监听 127.0.0.1
                "--host",
                inst.host,
                "--port",
                str(inst.port),
            ]
            # 只记命令行与 env 键名;值(尤其 auth_token)不许进日志
            log.info(
                "starting sandbox-agent at %s (role env keys: %s)",
                inst.base_url,
                sorted(inst.env_vars) or "(none)",
            )
            inst.proc = subprocess.Popen(cmd, env={**os.environ, **inst.env_vars})
            for _ in range(120):  # 最多等 60s(首次要解压 agent 二进制)
                await asyncio.sleep(0.5)
                if await self._healthy(inst):
                    break
                if inst.proc.poll() is not None:
                    raise RuntimeError(
                        f"sandbox-agent (port {inst.port}) exited with code {inst.proc.returncode}"
                    )
            else:
                raise RuntimeError(f"sandbox-agent (port {inst.port}) did not become healthy in 60s")

    async def _healthy(self, inst: SandboxInstance) -> bool:
        try:
            async with httpx.AsyncClient(timeout=2.0, trust_env=False) as c:
                r = await c.get(f"{inst.base_url}/v1/health")
                return r.status_code == 200
        except Exception:  # noqa: BLE001
            return False

    async def assert_agent_available(self) -> None:
        """启动自检:对每个实际使用的实例,确认配置的 agent 在列且已安装(§2.1 listAgents)。"""
        for inst in self.instances():
            async with httpx.AsyncClient(timeout=5.0, trust_env=False) as c:
                r = await c.get(f"{inst.base_url}/v1/agents")
                r.raise_for_status()
                agents = {a["id"]: a for a in r.json().get("agents", [])}
            name = self.config.agent_name
            if name not in agents:
                raise RuntimeError(
                    f"agent {name!r} 不在 sandbox-agent({inst.base_url})的列表里: {sorted(agents)}。"
                    "见 docs/m0-sandbox-agent-api.md 的勘误说明。"
                )
            if not agents[name].get("installed"):
                raise RuntimeError(
                    f"agent {name!r} 未安装:运行 `sandbox-agent install-agent --all`"
                )

    def open_session(
        self,
        session_name: str,
        *,
        role: str = "",
        resume_offset: int = 0,
        on_update=None,
        on_continuity_break=None,
    ) -> AcpSession:
        inst = self.instance_for(role) if role else self.instance_for(ROLES[0])
        s = AcpSession(
            inst.base_url,
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
        for inst in self._instances.values():
            if inst.proc and inst.proc.poll() is None:
                inst.proc.terminate()
                try:
                    inst.proc.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    inst.proc.kill()
                log.info("sandbox-agent (port %s) stopped", inst.port)
            inst.proc = None
