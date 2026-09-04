"""流水线编排:生成需求单 → 张贴 → 派单 → 验收 → 人拍板。

各流程都由状态转移触发,状态机(§2.5)是唯一的调度权威。
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from . import gitutil, prompts, statemachine as sm, store as st
from .runtime import QuestRuntime, RoleRuntime
from .sandbox import SandboxManager

log = logging.getLogger("guildhall.flow")


# ─────────────────────────────────────────────────────────────────────────────
# 对话(§2.6.1):drafting 期间 receptionist session 的建立与续聊
# ─────────────────────────────────────────────────────────────────────────────


async def start_receptionist(rt: QuestRuntime, opening_message: Optional[str]) -> None:
    """建 receptionist session 并投喂系统 prompt(逐字契约)。

    receptionist 的「进出一致」:进工作区前先记 `git diff HEAD` 的 sha256(§6.2)。
    """
    store = rt.store
    state = store.read_state()
    if not state["integrity"].get("receptionist"):
        try:
            before = gitutil.diff_head_sha256(store.project)
            state = store.update_state(
                integrity={**state["integrity"], "receptionist": {"before": before, "after": None, "ok": None}}
            )
        except RuntimeError as e:
            store.set_error(f"无法建立完整性基线:{e}")

    await rt.open_role(
        "receptionist",
        cwd=store.project,
        first_message=prompts.receptionist_system_prompt(store),
        system=True,
    )
    if opening_message:
        receiver = rt.get_role("receptionist")
        if receiver:
            await receiver.send_user(opening_message)


async def continue_chat(rt: QuestRuntime, text: str) -> None:
    receiver = rt.get_role("receptionist")
    if receiver is None:
        raise RuntimeError("receptionist session 未运行")
    await receiver.send_user(text)


# ─────────────────────────────────────────────────────────────────────────────
# 生成需求单:receptionist 吐 markdown → 服务端规范化 frontmatter → 校验闸门
# ─────────────────────────────────────────────────────────────────────────────


async def generate_quest(rt: QuestRuntime) -> dict[str, Any]:
    """触发 receptionist 输出需求单,落盘 quest.md。写不出可执行验收则拒绝。"""
    store = rt.store
    if store.read_state()["state"] != sm.DRAFTING:
        return {"ok": False, "reason": "只有 drafting 状态能生成需求单"}
    receiver = rt.get_role("receptionist")
    if receiver is None:
        return {"ok": False, "reason": "receptionist session 未运行"}

    await receiver.send_user(prompts.GENERATE_TRIGGER)
    raw = "".join(receiver.session.turn_text).strip()
    if not raw:
        return {"ok": False, "reason": "receptionist 没有输出任何内容"}

    md = normalize_quest_md(store, raw)
    if md is None:
        return {
            "ok": False,
            "reason": "没有解析出需求单正文(需要包含「## 验收步骤」)。需求可能还没问清楚,继续追问后再试。",
        }
    gate = st.acceptance_gate_error(md)
    if gate:
        return {"ok": False, "reason": gate, "raw": md}
    store.write_quest_md(md)
    return {"ok": True, "quest_md": md}


_FENCE_RE = re.compile(r"^```[a-zA-Z0-9_-]*\n(.*?)\n?```\s*$", re.DOTALL)
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def normalize_quest_md(store: st.QuestStore, raw: str) -> Optional[str]:
    """把 receptionist 的输出规范成 quest.md:剥围栏、重建 frontmatter(程序事实字段
    以服务端为准,id 与目录一致,不许 Receptionist 发明)。"""
    text = raw.strip()
    m = _FENCE_RE.match(text)
    if m:
        text = m.group(1).strip()
    # 正文从第一个 ## 章节开始,前面的闲话一律丢掉
    body_match = re.search(r"^## .*$", text, re.MULTILINE)
    if not body_match:
        return None
    body = text[body_match.start() :].strip()
    if "## 验收步骤" not in body:
        return None
    title = ""
    fm_title = re.search(r"^title:\s*(.+)$", raw, re.MULTILINE)
    if fm_title:
        title = fm_title.group(1).strip()
    if not title:
        h1 = re.search(r"^#\s+(.+)$", raw, re.MULTILINE)
        if h1:
            title = h1.group(1).strip()
    frontmatter = {
        "id": store.quest_id,
        "title": title or store.quest_id,
        "project": store.project,
        "created": st.now_iso(),
    }
    doc = "---\n" + "\n".join(f"{k}: {v}" for k, v in frontmatter.items()) + "\n---\n\n" + body
    return doc


# ─────────────────────────────────────────────────────────────────────────────
# 张贴(→ posted):过闸门 + receptionist 进出一致校验(失败不拦,记 error)
# ─────────────────────────────────────────────────────────────────────────────


def finalize_receptionist_integrity(store: st.QuestStore) -> None:
    state = store.read_state()
    entry = state["integrity"].get("receptionist")
    if not entry or not entry.get("before"):
        return
    try:
        after = gitutil.diff_head_sha256(store.project)
    except RuntimeError as e:
        store.set_error(f"receptionist 进出一致校验失败:{e}")
        return
    ok = entry["before"] == after
    state = store.update_state(
        integrity={**state["integrity"], "receptionist": {**entry, "after": after, "ok": ok}}
    )
    if not ok:
        # §6.2:receptionist 脏了只记 error、前端提示,流程继续——它在你的工作区,你看着办
        store.set_error("receptionist 结束时项目工作区的 tracked 文件与开始时不一致(见 integrity.receptionist)。流程继续,但请自己检查工作区。")


# ─────────────────────────────────────────────────────────────────────────────
# 派单(→ in_progress):worktree + adventurer(§2.6.2)
# ─────────────────────────────────────────────────────────────────────────────


async def dispatch_adventurer(rt: QuestRuntime, manager: SandboxManager) -> None:
    store = rt.store
    state = store.read_state()
    repo = Path(store.project)
    worktree = Path(state["worktree"])
    branch = state["branch"]

    base = gitutil.create_worktree(repo, store.quest_id, worktree, branch)
    state = store.update_state(base_commit=base)
    log.info("worktree ready: %s @ %s (base %s)", worktree, branch, base[:12])

    quest_md = store.read_quest_md() or ""
    session_name = f"guildhall-{store.quest_id}-adventurer"
    session = manager.open_session(session_name)
    await session.start()
    try:
        await session.new_session(cwd=str(worktree))
    except Exception as e:  # noqa: BLE001
        await session.close()
        store.set_error(f"adventurer session 创建失败:{e}")
        _safe_transition(store, sm.FAILED)
        return

    state = store.read_state()
    store.update_state(sessions={**state["sessions"], "adventurer": session_name})
    rt.roles["adventurer"] = RoleRuntime(store, "adventurer", session)

    async def run() -> None:
        try:
            await session.prompt(prompts.adventurer_first_message(quest_md), idle_timeout=120)
            # adventurer session 结束事件 → in_progress → appraising(§2.5)
            _safe_transition(store, sm.APPRAISING)
            await run_appraisal(rt, manager)
        except Exception as e:  # noqa: BLE001
            log.exception("adventurer failed")
            store.set_error(f"adventurer 异常:{e}")
            _safe_transition(store, sm.FAILED)

    task = asyncio.create_task(run())
    rt.tasks.add(task)
    task.add_done_callback(rt.tasks.discard)


# ─────────────────────────────────────────────────────────────────────────────
# 验收(§2.6.3):全新 appraiser session,输入白名单严格三样
# ─────────────────────────────────────────────────────────────────────────────


async def run_appraisal(rt: QuestRuntime, manager: SandboxManager) -> None:
    store = rt.store
    state = store.read_state()
    worktree = Path(state["worktree"])

    # 进出一致基线:appraiser 开始前的 tracked 状态(adventurer 刚改完的样子)
    try:
        before = gitutil.diff_head_sha256(worktree)
        state = store.update_state(
            integrity={**state["integrity"], "appraiser": {"before": before, "after": None, "ok": None}}
        )
    except RuntimeError as e:
        store.set_error(f"无法建立 appraiser 完整性基线:{e}")
        _safe_transition(store, sm.DISPUTED)
        return

    quest_md = store.read_quest_md() or ""
    session_name = f"guildhall-{store.quest_id}-appraiser"
    session = manager.open_session(session_name)
    await session.start()
    await session.new_session(cwd=str(worktree))
    state = store.read_state()
    store.update_state(sessions={**state["sessions"], "appraiser": session_name})
    rt.roles["appraiser"] = RoleRuntime(store, "appraiser", session)

    try:
        await session.prompt(prompts.appraiser_first_message(quest_md), idle_timeout=120)
    except Exception as e:  # noqa: BLE001
        store.set_error(f"appraiser 异常:{e}")
        await session.close()
        _safe_transition(store, sm.DISPUTED)
        return
    await session.close()

    # 进出一致校验:不一致 → 结论整份作废(§6.2)
    try:
        after = gitutil.diff_head_sha256(worktree)
    except RuntimeError as e:
        store.set_error(f"appraiser 进出一致校验失败:{e}")
        _safe_transition(store, sm.DISPUTED)
        return
    integrity_ok = before == after
    # §6.2:sha256 存进 state.json(after/ok 落盘,receptionist 同理)
    state = store.read_state()
    store.update_state(
        integrity={**state["integrity"], "appraiser": {"before": before, "after": after, "ok": integrity_ok}}
    )

    raw = "".join(session.turn_text).strip()
    appraisal: Optional[dict[str, Any]] = None
    parse_error: Optional[str] = None
    if raw:
        appraisal = parse_appraisal(raw)
        if appraisal is None:
            parse_error = "appraisal 输出不是合法 JSON(§6.4:不重试,原始输出见 error)"

    if appraisal is not None:
        appraisal["invalidated"] = not integrity_ok
        store.write_appraisal(appraisal)
        store.set_error(None if integrity_ok else "appraiser 结束时 worktree 的 tracked 文件与开始时不一致:本次验收结论已整份作废(invalidated)。")

        if not integrity_ok:
            _safe_transition(store, sm.DISPUTED)
        elif appraisal.get("touched_tests") or appraisal.get("out_of_scope_files") or any(
            c.get("result") != "pass" for c in appraisal.get("checks", [])
        ):
            _safe_transition(store, sm.DISPUTED)
        else:
            _safe_transition(store, sm.APPRAISED)
    else:
        # §6.4 第 7 条:解析失败 → disputed + error 存原始输出,不重试
        store.set_error(f"appraisal.json 解析失败:{parse_error}\n原始输出:\n{raw[:4000]}")
        _safe_transition(store, sm.DISPUTED)


def parse_appraisal(raw: str) -> Optional[dict[str, Any]]:
    text = raw.strip()
    m = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.DOTALL)
    if m:
        text = m.group(1)
    else:
        start = text.find("{")
        end = text.rfind("}")
        if start == -1 or end == -1 or end <= start:
            return None
        text = text[start : end + 1]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None
    if not isinstance(data, dict) or "checks" not in data:
        return None
    return data


def _safe_transition(store: st.QuestStore, to: str) -> None:
    try:
        store.transition(to)
    except sm.InvalidTransition:
        log.warning("transition to %s already handled (state=%s)", to, store.read_state()["state"])
