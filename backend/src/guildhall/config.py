"""config.toml(§5)。缺省时生成完整可用样例。

三角色分流(M1,见 docs/m1-per-role-model.md):[agent.<role>] 全部可选,
缺省继承 [agent];有独立 env 的角色各起一个 sandbox-agent 实例(方案 B)。
auth_token 是敏感值:任何日志/异常路径都不许打出它(见 sandbox.py)。
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import layout

DEFAULT_CONFIG = """\
[sandbox_agent]
binary = "sandbox-agent"          # PATH 里找得到就不用改
host = "127.0.0.1"
port = 2468                       # 不许改成 0.0.0.0,demo 无鉴权。有独立模型配置的角色按顺序 +1
autostart = true                  # guildhall-server 启动时自动拉起

[agent]
# M0 考古勘误:0.4.2 的 agent id 是 "claude"(规格书成稿时的 "claude-code" 不存在),
# 见 docs/m0-sandbox-agent-api.md
name = "claude"
mode = "default"

# 三角色分流(M1):以下三段全部可选,不配则三角色共用上面这份。
# 配了 base_url/auth_token/model 中任意一项的角色,会起一个带独立环境变量的
# sandbox-agent 实例(方案 B,见 docs/m1-per-role-model.md)。
#
# [agent.receptionist]
# model = "claude-opus-4-6"
# base_url = "https://xxx/api"
# auth_token = "sk-..."
#
# [agent.adventurer]
# model = "glm-4.7"
# base_url = "https://xxx/api/anthropic"
# auth_token = "..."
#
# [agent.appraiser]
# model = "claude-opus-4-6"

[paths]
worktree_root = "~/.guildhall/worktrees"

[server]
port = 8420
"""

ROLES = ("receptionist", "adventurer", "appraiser")


@dataclass
class SandboxAgentConfig:
    binary: str = "sandbox-agent"
    host: str = "127.0.0.1"
    port: int = 2468
    autostart: bool = True

    @property
    def base_url(self) -> str:
        return f"http://{self.host}:{self.port}"


@dataclass
class AgentRoleConfig:
    """单个角色的独立模型配置;全空 = 继承 [agent](不起独立实例)。"""

    model: str | None = None
    base_url: str | None = None
    auth_token: str | None = None

    def env_vars(self) -> dict[str, str]:
        """Claude Code 官方约定的环境变量名(§2.1.3)。"""
        env: dict[str, str] = {}
        if self.base_url:
            env["ANTHROPIC_BASE_URL"] = self.base_url
        if self.auth_token:
            env["ANTHROPIC_AUTH_TOKEN"] = self.auth_token
        if self.model:
            env["ANTHROPIC_MODEL"] = self.model
        return env

    def signature(self) -> tuple:
        """实例去重键:env 完全相同的角色复用同一实例。不含 token 之外的可读信息。"""
        return tuple(sorted(self.env_vars().items()))


@dataclass
class Config:
    sandbox_agent: SandboxAgentConfig = field(default_factory=SandboxAgentConfig)
    agent_name: str = "claude"
    agent_mode: str = "default"
    agent_roles: dict[str, AgentRoleConfig] = field(default_factory=dict)
    worktree_root: str = "~/.guildhall/worktrees"
    server_port: int = 8420

    def role_config(self, role: str) -> AgentRoleConfig:
        """角色独立配置;未配置的角色回落到空配置(继承 [agent],共享默认实例)。"""
        return self.agent_roles.get(role) or AgentRoleConfig()


def load() -> Config:
    path = layout.GUILDHALL_DIR / "config.toml"
    if not path.exists():
        layout.GUILDHALL_DIR.mkdir(parents=True, exist_ok=True)
        path.write_text(DEFAULT_CONFIG, encoding="utf-8")
    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    sa = raw.get("sandbox_agent", {})
    agent = raw.get("agent", {})
    paths = raw.get("paths", {})
    server = raw.get("server", {})
    roles: dict[str, AgentRoleConfig] = {}
    for role in ROLES:
        section = agent.get(role)
        if not isinstance(section, dict):
            continue
        rc = AgentRoleConfig(
            model=section.get("model"),
            base_url=section.get("base_url"),
            auth_token=section.get("auth_token"),
        )
        if rc.env_vars():  # 整段为空/全空值:视同未配置
            roles[role] = rc
    return Config(
        sandbox_agent=SandboxAgentConfig(
            binary=sa.get("binary", "sandbox-agent"),
            host=sa.get("host", "127.0.0.1"),
            port=int(sa.get("port", 2468)),
            autostart=bool(sa.get("autostart", True)),
        ),
        agent_name=agent.get("name", "claude"),
        agent_mode=agent.get("mode", "default"),
        agent_roles=roles,
        worktree_root=paths.get("worktree_root", "~/.guildhall/worktrees"),
        server_port=int(server.get("port", 8420)),
    )
