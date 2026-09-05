"""M1 三角色分流(方案 B,docs/m1-per-role-model.md)。

1. 旧配置(无 [agent.<role>])照常加载,三角色回落 [agent],只起一个实例。
2. 三角色不同 base_url → 起对应数量的实例,端口不冲突。
3. 任何日志输出中不含 auth_token 的值(负向)。
"""

from __future__ import annotations

import json

import pytest

from guildhall.config import AgentRoleConfig, Config, load
from guildhall.sandbox import SandboxManager

TOKEN = "sk-test-secret-do-not-log"

THREE_ROLE_CONFIG = """\
[sandbox_agent]
binary = "sandbox-agent"
host = "127.0.0.1"
port = 2468
autostart = true

[agent]
name = "claude"
mode = "default"

[agent.receptionist]
model = "claude-opus-4-6"
base_url = "https://receptionist.example/api"
auth_token = "sk-test-secret-do-not-log"

[agent.adventurer]
model = "glm-4.7"
base_url = "https://adventurer.example/api/anthropic"
auth_token = "sk-other-token"

[agent.appraiser]
model = "claude-opus-4-6"
base_url = "https://appraiser.example/api"
auth_token = "sk-third-token"
"""


def _write_config(home, text: str) -> None:
    home.mkdir(parents=True, exist_ok=True)
    (home / "config.toml").write_text(text, encoding="utf-8")


def test_legacy_config_without_role_sections_falls_back_to_agent(tmp_guildhall):
    _write_config(
        tmp_guildhall,
        """\
[sandbox_agent]
binary = "sandbox-agent"
host = "127.0.0.1"
port = 2468
autostart = true

[agent]
name = "claude"
mode = "default"
""",
    )
    config = load()
    assert config.agent_name == "claude"
    assert config.agent_roles == {}
    for role in ("receptionist", "adventurer", "appraiser"):
        assert config.role_config(role).env_vars() == {}

    # 全部角色共享 base_port 上的同一个默认实例(旧行为)
    manager = SandboxManager(config)
    insts = manager.instances()
    assert len(insts) == 1
    assert insts[0].port == 2468
    assert insts[0].env_vars == {}
    assert manager.instance_for("adventurer") is insts[0]


def test_three_roles_with_distinct_base_url_get_distinct_ports(tmp_guildhall, monkeypatch):
    _write_config(tmp_guildhall, THREE_ROLE_CONFIG)
    config = load()
    assert set(config.agent_roles) == {"receptionist", "adventurer", "appraiser"}

    manager = SandboxManager(config)
    insts = manager.instances()
    assert len(insts) == 3
    ports = sorted(i.port for i in insts)
    assert len(set(ports)) == 3  # 端口不冲突
    assert ports == [2468, 2469, 2470]  # 从 base_port 起顺序分配

    by_url = {i.env_vars["ANTHROPIC_BASE_URL"]: i for i in insts}
    assert by_url["https://receptionist.example/api"].env_vars["ANTHROPIC_MODEL"] == "claude-opus-4-6"
    assert manager.instance_for("receptionist").env_vars["ANTHROPIC_AUTH_TOKEN"] == TOKEN
    assert manager.instance_for("adventurer").env_vars["ANTHROPIC_BASE_URL"] == "https://adventurer.example/api/anthropic"


def test_identical_role_env_reuses_one_instance(tmp_guildhall):
    config = Config()
    config.agent_roles = {
        "receptionist": AgentRoleConfig(model="m1", base_url="https://same/api", auth_token="tok-a"),
        "adventurer": AgentRoleConfig(model="m1", base_url="https://same/api", auth_token="tok-a"),
        "appraiser": AgentRoleConfig(model="m2", base_url="https://other/api", auth_token="tok-b"),
    }
    manager = SandboxManager(config)
    assert manager.instance_for("receptionist") is manager.instance_for("adventurer")
    assert manager.instance_for("appraiser") is not manager.instance_for("receptionist")
    assert len(manager.instances()) == 2


class _FakeProc:
    def __init__(self):
        self.pid = 4242
        self.returncode = None

    def poll(self):
        return None

    def wait(self, timeout=None):
        return 0

    def terminate(self):
        pass

    def kill(self):
        pass


@pytest.mark.anyio
async def test_spawn_uses_role_env_and_never_logs_token(tmp_guildhall, monkeypatch, caplog):
    """方案 B 拉起路径:每个实例一次 Popen,env 叠加角色变量;token 不出现在任何日志里。"""
    import guildhall.sandbox as sandbox_module

    _write_config(tmp_guildhall, THREE_ROLE_CONFIG)
    config = load()
    manager = SandboxManager(config)

    spawned: list[tuple[list[str], dict | None]] = []

    def fake_popen(cmd, env=None):
        spawned.append((list(cmd), env))
        return _FakeProc()

    async def fake_healthy(self, inst):
        return inst.proc is not None  # 拉起成功前不健康,拉起后立即健康

    monkeypatch.setattr(sandbox_module.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(SandboxManager, "_healthy", fake_healthy)

    with caplog.at_level("DEBUG", logger="guildhall.sandbox"):
        await manager.ensure_running()

    assert len(spawned) == 3
    for cmd, env in spawned:
        assert "--port" in cmd
        assert env is not None
        assert "ANTHROPIC_AUTH_TOKEN" in env
    # 每个角色的 token 只进自己实例的子进程 env
    all_envs = [env for _, env in spawned]
    assert sum(1 for e in all_envs if e.get("ANTHROPIC_AUTH_TOKEN") == TOKEN) == 1

    # 负向:全部日志(含 DEBUG)与序列化产物中都不许出现 token 值
    log_text = caplog.text
    assert TOKEN not in log_text
    assert "sk-other-token" not in log_text
    assert "sk-third-token" not in log_text
    # 实例日志只带 env 键名
    assert "ANTHROPIC_AUTH_TOKEN" in log_text  # 键名可以出现
    json.dumps([cmd for cmd, _ in spawned])  # 命令行可序列化且无敏感值
    assert all(TOKEN not in json.dumps(cmd) for cmd, _ in spawned)
