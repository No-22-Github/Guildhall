"""§2.2 磁盘布局与路径转义。

转义规则(逐字,不许自由发挥):绝对路径中的 ``/`` 全部替换为 ``-``,
开头的 ``/`` 也转,所以结果以 ``-`` 开头。
"""

from __future__ import annotations

import os
import re
from pathlib import Path

GUILDHALL_DIR = Path(os.path.expanduser("~/.guildhall"))
WORKTREE_ROOT = GUILDHALL_DIR / "worktrees"


def escape_project_path(project: str) -> str:
    """/Users/no22/code/preen → -Users-no22-code-preen"""
    return project.replace("/", "-")


def project_dir(project: str) -> Path:
    return GUILDHALL_DIR / "projects" / escape_project_path(project)


def quests_dir(project: str) -> Path:
    return project_dir(project) / "quests"


def quest_dir(project: str, quest_id: str) -> Path:
    return quests_dir(project) / quest_id


def worktree_path(quest_id: str) -> Path:
    return WORKTREE_ROOT / quest_id


QUEST_ID_RE = re.compile(r"^\d{8}-\d{4}-[a-z0-9-]{1,40}$")


def slugify(text: str, max_len: int = 40) -> str:
    """小写、连字符分隔、不超过 40 字符。中文等非 [a-z0-9] 字符按词元丢弃。

    全部被丢时回退 ``draft``——slug 不许为空。
    """
    text = text.strip().lower()
    tokens = re.findall(r"[a-z0-9]+", text)
    slug = "-".join(tokens)[:max_len].strip("-")
    return slug or "draft"


def make_quest_id(now: str, seed_text: str) -> str:
    """quest id = YYYYMMDD-HHMM-<slug>(§2.2)。now 是已格式化的时间戳。"""
    quest_id = f"{now}-{slugify(seed_text)}"
    if not QUEST_ID_RE.match(quest_id):  # pragma: no cover - 防御
        raise ValueError(f"bad quest id: {quest_id}")
    return quest_id


def list_projects() -> list[str]:
    """列出已注册项目。项目目录里有注册时写入的 .project.json。"""
    root = GUILDHALL_DIR / "projects"
    if not root.exists():
        return []
    out = []
    for d in sorted(root.iterdir()):
        marker = d / ".project.json"
        if d.is_dir() and marker.exists():
            import json

            try:
                out.append(json.loads(marker.read_text(encoding="utf-8"))["path"])
            except Exception:
                continue
    return out


def register_project(project: str) -> Path:
    """注册项目:建目录和空 lore.md(§3 POST /projects)。"""
    import json

    if not Path(project).is_absolute():
        raise ValueError(f"project path must be absolute: {project}")
    if not Path(project).exists():
        raise ValueError(f"project path does not exist: {project}")

    pdir = project_dir(project)
    (pdir / "quests").mkdir(parents=True, exist_ok=True)
    lore = pdir / "lore.md"
    if not lore.exists():
        lore.write_text("", encoding="utf-8")
    marker = pdir / ".project.json"
    if not marker.exists():
        marker.write_text(json.dumps({"path": project}, ensure_ascii=False), encoding="utf-8")
    return pdir
