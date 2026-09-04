"""quest/state.json 读写(§2.4)。程序独占写 state.json,原子落盘。"""

from __future__ import annotations

import json
import threading
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Any, Optional

from . import layout, statemachine as sm
from .questmd import acceptance_gate_error

# ±08:00 之外的时区无所谓,demo 单机。用本地时区,与 quest id 的时间戳一致。
_TZ = datetime.now().astimezone().tzinfo or timezone(timedelta(hours=8))


def now_iso() -> str:
    return datetime.now(_TZ).isoformat(timespec="seconds")


def now_stamp() -> str:
    return datetime.now(_TZ).strftime("%Y%m%d-%H%M")


def _atomic_write(path: Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)


class QuestStore:
    """一个 quest 的磁盘态。所有写都走这里,保证 state.json 的 history 完整。"""

    def __init__(self, project: str, quest_id: str):
        self.project = project
        self.quest_id = quest_id
        self.dir = layout.quest_dir(project, quest_id)
        self.quest_md = self.dir / "quest.md"
        self.state_json = self.dir / "state.json"
        self.events_dir = self.dir / "events"
        self.appraisal_json = self.dir / "appraisal.json"
        self._lock = threading.Lock()

    # ---------- 创建 ----------

    @classmethod
    def create(cls, project: str, seed_text: str) -> "QuestStore":
        qid = layout.make_quest_id(now_stamp(), seed_text)
        store = cls(project, qid)
        if store.dir.exists():  # 同一分钟同 slug:加序号后缀
            for i in range(2, 100):
                store = cls(project, f"{qid}-{i}")
                if not store.dir.exists():
                    break
            else:
                raise RuntimeError("cannot allocate quest id")
        store.dir.mkdir(parents=True)
        store.events_dir.mkdir()
        store.write_state(
            {
                "id": store.quest_id,
                "state": sm.DRAFTING,
                "project": project,
                "branch": f"guildhall/{store.quest_id}",
                "worktree": str(layout.worktree_path(store.quest_id)),
                "base_commit": None,
                "sessions": {r: None for r in ("receptionist", "adventurer", "appraiser")},
                "offsets": {r: 0 for r in ("receptionist", "adventurer", "appraiser")},
                "integrity": {},
                "history": [{"at": now_iso(), "from": None, "to": sm.DRAFTING}],
                "error": None,
            }
        )
        return store

    # ---------- 读取 ----------

    def exists(self) -> bool:
        return self.state_json.exists()

    def read_state(self) -> dict[str, Any]:
        return json.loads(self.state_json.read_text(encoding="utf-8"))

    def read_quest_md(self) -> Optional[str]:
        if not self.quest_md.exists():
            return None
        return self.quest_md.read_text(encoding="utf-8")

    def read_appraisal(self) -> Optional[dict[str, Any]]:
        if not self.appraisal_json.exists():
            return None
        return json.loads(self.appraisal_json.read_text(encoding="utf-8"))

    # ---------- 写入 ----------

    def write_state(self, state: dict[str, Any]) -> None:
        with self._lock:
            _atomic_write(self.state_json, json.dumps(state, ensure_ascii=False, indent=2) + "\n")

    def update_state(self, **fields: Any) -> dict[str, Any]:
        state = self.read_state()
        state.update(fields)
        self.write_state(state)
        return state

    def set_error(self, error: Optional[str]) -> dict[str, Any]:
        return self.update_state(error=error)

    def update_state_offsets(self, role: str, upstream_offset: int) -> None:
        state = self.read_state()
        if state.get("offsets", {}).get(role) == upstream_offset:
            return
        offsets = {**state.get("offsets", {}), role: upstream_offset}
        self.update_state(offsets=offsets)

    def transition(self, to: str) -> dict[str, Any]:
        """走状态机落盘。表外转移抛 InvalidTransition;闸门拒绝抛 GateRejected。

        §2.5 硬约束:进入 posted 必须先校验 quest.md 含非空「## 验收步骤」,
        否则拒绝转移并把原因写进 error——这是整条流水线唯一防止「空单」的闸门。
        """
        with self._lock:
            state = self.read_state()
            cur = state["state"]
            sm.assert_transition(cur, to)
            if to == sm.POSTED:
                reason = self.check_postable()
                if reason:
                    state["error"] = reason
                    _atomic_write(self.state_json, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
                    raise GateRejected(reason)
            state["state"] = to
            state["history"].append({"at": now_iso(), "from": cur, "to": to})
            _atomic_write(self.state_json, json.dumps(state, ensure_ascii=False, indent=2) + "\n")
            return state

    def write_quest_md(self, text: str) -> None:
        _atomic_write(self.quest_md, text if text.endswith("\n") else text + "\n")

    def write_appraisal(self, data: dict[str, Any]) -> None:
        _atomic_write(self.appraisal_json, json.dumps(data, ensure_ascii=False, indent=2) + "\n")

    def append_event(self, role: str, envelope: dict[str, Any]) -> int:
        """旁路事件流:一角色一个 jsonl,每行一个原始 envelope。返回行号(0 基)。"""
        path = self.events_dir / f"{role}.jsonl"
        with self._lock:
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(envelope, ensure_ascii=False) + "\n")
            return _line_count(path) - 1

    def read_events(self, role: str, offset: int = 0) -> list[dict[str, Any]]:
        path = self.events_dir / f"{role}.jsonl"
        if not path.exists() or offset < 0:
            return []
        out = []
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i >= offset and line.strip():
                    out.append(json.loads(line))
        return out

    # ---------- 派单前的闸门 ----------

    def check_postable(self) -> Optional[str]:
        """进入 posted 前校验。返回错误原因,None 表示可过。"""
        text = self.read_quest_md()
        if text is None:
            return "quest.md 还没有生成:先在对话页点「生成需求单」。"
        return acceptance_gate_error(text)


class GateRejected(Exception):
    """闸门拒绝(如 posted 前验收步骤为空)。HTTP 层必须回 409,原因写进 error。"""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


def _line_count(path: Path) -> int:
    with path.open("rb") as f:
        return sum(1 for _ in f)


def find_quest(quest_id: str) -> Optional[QuestStore]:
    """按 id 全盘找 quest(路由是 /quests/{id},不带 project)。"""
    root = layout.GUILDHALL_DIR / "projects"
    if not root.exists():
        return None
    for pdir in root.iterdir():
        qdir = pdir / "quests" / quest_id
        if (qdir / "state.json").exists():
            marker = pdir / ".project.json"
            import json

            project = json.loads(marker.read_text(encoding="utf-8"))["path"]
            return QuestStore(project, quest_id)
    return None


def list_quests(project: str) -> list[dict[str, Any]]:
    qroot = layout.quests_dir(project)
    if not qroot.exists():
        return []
    out = []
    for qdir in sorted(qroot.iterdir()):
        sj = qdir / "state.json"
        if not sj.exists():
            continue
        st = json.loads(sj.read_text(encoding="utf-8"))
        qmd = qdir / "quest.md"
        title = ""
        if qmd.exists():
            from .questmd import parse

            title = parse(qmd.read_text(encoding="utf-8")).frontmatter.get("title", "")
        out.append(
            {
                "id": st["id"],
                "state": st["state"],
                "project": st["project"],
                "title": title,
                "created": st["history"][0]["at"] if st.get("history") else None,
            }
        )
    return out


def any_in_progress() -> Optional[str]:
    """同一时刻只允许一个 quest 处于 in_progress(§1 明确不做并发)。"""
    root = layout.GUILDHALL_DIR / "projects"
    if not root.exists():
        return None
    import json

    for pdir in root.iterdir():
        for qdir in (pdir / "quests").glob("*/state.json") if (pdir / "quests").exists() else []:
            st = json.loads(qdir.read_text(encoding="utf-8"))
            if st.get("state") == sm.IN_PROGRESS:
                return st["id"]
    return None
