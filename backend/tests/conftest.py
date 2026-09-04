"""共享 fixture:隔离的 ~/.guildhall(GUILDHALL_DIR 用环境变量重定向)。"""

from __future__ import annotations

import pytest

from guildhall import layout, store as st


@pytest.fixture()
def tmp_guildhall(tmp_path, monkeypatch):
    home = tmp_path / "guildhall"
    monkeypatch.setattr(layout, "GUILDHALL_DIR", home)
    monkeypatch.setattr(layout, "WORKTREE_ROOT", home / "worktrees")
    # store.py 通过 layout.GUILDHALL_DIR 的引用取值;确保动态查找
    import guildhall.store as store_mod

    monkeypatch.setattr(store_mod.layout, "GUILDHALL_DIR", home, raising=False)
    import guildhall.config as config_mod

    monkeypatch.setattr(config_mod.layout, "GUILDHALL_DIR", home, raising=False)
    return home


@pytest.fixture()
def demo_repo(tmp_path):
    """一个最小 git 仓库,当 quest 的目标项目。"""
    import subprocess

    repo = tmp_path / "demo-repo"
    repo.mkdir()
    def git(*args):
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True)
    git("init", "-q")
    git("config", "user.email", "g@g")
    git("config", "user.name", "g")
    (repo / "src").mkdir()
    (repo / "src" / "main.py").write_text("def main():\n    return 1\n")
    (repo / "tests").mkdir()
    (repo / "tests" / "test_main.py").write_text("from src.main import main\n\ndef test_main():\n    assert main() == 1\n")
    git("add", ".")
    git("commit", "-qm", "init")
    return repo


@pytest.fixture()
def make_quest(tmp_guildhall, demo_repo):
    def _make() -> st.QuestStore:
        return st.QuestStore.create(str(demo_repo), "sync upstream quant")
    return _make


@pytest.fixture()
def fake_acp():
    from fake_acp import FakeAcpServer

    server = FakeAcpServer()
    url = server.start()
    yield server
    server.stop()


@pytest.fixture()
def client(tmp_guildhall, fake_acp, demo_repo):
    """guildhall FastAPI 应用,连到假 sandbox-agent,磁盘重定向到 tmp。"""
    from fastapi.testclient import TestClient

    from guildhall import api
    from guildhall.config import Config, SandboxAgentConfig

    from urllib.parse import urlparse

    u = urlparse(fake_acp.base_url)
    config = Config(
        sandbox_agent=SandboxAgentConfig(host=u.hostname, port=u.port, autostart=False),
        agent_name="claude",
    )
    context = api.AppContext(config)
    app = api.build_app(context)

    @app.on_event("startup")
    async def _startup():
        await context.manager.ensure_running()
        await context.manager.assert_agent_available()
        api.ctx = context

    with TestClient(app) as c:
        yield c
