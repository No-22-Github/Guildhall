"""config.toml(§5)。缺省时生成完整可用样例。"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from . import layout

DEFAULT_CONFIG = """\
[sandbox_agent]
binary = "sandbox-agent"          # PATH 里找得到就不用改
host = "127.0.0.1"
port = 2468                       # 不许改成 0.0.0.0,demo 无鉴权
autostart = true                  # guildhall-server 启动时自动拉起

[agent]
# M0 考古勘误:0.4.2 的 agent id 是 "claude"(规格书成稿时的 "claude-code" 不存在),
# 见 docs/m0-sandbox-agent-api.md
name = "claude"
mode = "default"

[paths]
worktree_root = "~/.guildhall/worktrees"

[server]
port = 8420
"""


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
class Config:
    sandbox_agent: SandboxAgentConfig = field(default_factory=SandboxAgentConfig)
    agent_name: str = "claude"
    agent_mode: str = "default"
    worktree_root: str = "~/.guildhall/worktrees"
    server_port: int = 8420


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
    return Config(
        sandbox_agent=SandboxAgentConfig(
            binary=sa.get("binary", "sandbox-agent"),
            host=sa.get("host", "127.0.0.1"),
            port=int(sa.get("port", 2468)),
            autostart=bool(sa.get("autostart", True)),
        ),
        agent_name=agent.get("name", "claude"),
        agent_mode=agent.get("mode", "default"),
        worktree_root=paths.get("worktree_root", "~/.guildhall/worktrees"),
        server_port=int(server.get("port", 8420)),
    )
