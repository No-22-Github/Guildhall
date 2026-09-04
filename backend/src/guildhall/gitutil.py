"""git 层:worktree 管理(§6.3)与「进出一致」校验(§6.2,逐字实现)。

校验方式是 `git diff HEAD` 的 sha256 对比,不是 `git status --porcelain`——
后者会把构建产物、缓存全列出来,等于没检查;`git diff HEAD` 只看 tracked 文件。
"""

from __future__ import annotations

import hashlib
import subprocess
from pathlib import Path


def _run(args: list[str], cwd: Path | None = None, check: bool = True) -> subprocess.CompletedProcess:
    p = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if check and p.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {p.stderr.strip()}")
    return p


def is_repo(path: Path) -> bool:
    return _run(["rev-parse", "--is-inside-work-tree"], cwd=path, check=False).returncode == 0


def head_commit(repo: Path) -> str:
    return _run(["rev-parse", "HEAD"], cwd=repo).stdout.strip()


def diff_head_sha256(workdir: Path) -> str:
    """`git diff HEAD` 的 sha256——「进出一致」的唯一度量(§6.2)。"""
    p = _run(["diff", "HEAD"], cwd=workdir, check=False)
    patch = p.stdout
    return "sha256:" + hashlib.sha256(patch.encode("utf-8")).hexdigest()


def create_worktree(repo: Path, quest_id: str, worktree: Path, branch: str) -> str:
    """in_progress 入口才建 worktree + 分支;返回分支基点 HEAD(§6.3)。

    分支名 guildhall/<quest-id>。基点记进 state.json,之后所有 diff 相对它,
    不相对 main——主仓库可能在此期间前进。
    """
    worktree.parent.mkdir(parents=True, exist_ok=True)
    base = head_commit(repo)
    _run(["worktree", "add", "-b", branch, str(worktree), base], cwd=repo)
    return base


def diff_vs_base(worktree: Path, base_commit: str | None) -> str:
    """review 页展示的 diff:相对创建 worktree 时的基点,不含未跟踪文件。"""
    if base_commit:
        return _run(["diff", base_commit], cwd=worktree, check=False).stdout
    return _run(["diff", "HEAD"], cwd=worktree, check=False).stdout
