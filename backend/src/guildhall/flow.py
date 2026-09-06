"""流水线编排:生成需求单 → 张贴 → 派单 → 验收 → 人拍板。

各流程都由状态转移触发,状态机(§2.5)是唯一的调度权威。
"""

from __future__ import annotations

from typing import Literal, Annotated
from pydantic import BaseModel, ConfigDict, Field, StringConstraints, ValidationError
from .questmd import acceptance_steps, scope_patterns, in_scope



import asyncio
import json
import logging
import re
from pathlib import Path
from typing import Any, Optional

from . import gitutil, prompts, statemachine as sm, store as st
from .questmd import negative_step_violation
from .runtime import QuestRuntime, RoleRuntime
from .sandbox import SandboxManager

Nonempty = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]

class AppraisalCheck(BaseModel):
    model_config = ConfigDict(strict=True)
    index: int = Field(ge=1)
    step: Nonempty
    result: Literal["pass", "fail"]
    evidence: Nonempty
    unsatisfiable: bool = False

class AppraisalResult(BaseModel):
    model_config = ConfigDict(strict=True)
    checks: list[AppraisalCheck] = Field(min_length=1)
    touched_tests: bool
    out_of_scope_files: list[Nonempty]
    summary: Nonempty


log = logging.getLogger("guildhall.flow")
RECEPTIONIST_GENERATION_IDLE_TIMEOUT = 30.0


def is_generation_request(text: str) -> bool:
    """识别用户在聊天框里发出的自然语言“出需求单”请求。

    只匹配短句，避免把讨论“需求单如何生成”的普通长消息误当成按钮动作。
    """
    compact = re.sub(r"\s+", "", text).strip("，,。.!！?？~～")
    if not compact or len(compact) > 30:
        return False
    return bool(re.fullmatch(
        r"(?:请|帮我|麻烦)?(?:重新|再|重)?(?:生成|出|整理)(?:一遍|一次|一下|一份|一个|新的|新版)?(?:需求单)?(?:给我|吧)?",
        compact,
    ))


# ─────────────────────────────────────────────────────────────────────────────
# 对话(§2.6.1):drafting 期间 receptionist session 的建立与续聊
# ─────────────────────────────────────────────────────────────────────────────


async def open_receptionist(rt: QuestRuntime) -> RoleRuntime:
    """建立 receptionist session，并记录只读完整性基线。

    receptionist 的「进出一致」:进工作区前先记 `git diff HEAD` 的 sha256(§6.2)。
    """
    store = rt.store
    state = store.read_state()
    if not state["integrity"].get("receptionist"):
        try:
            before = gitutil.diff_head_sha256(store.project)
            state = store.update_state(
                integrity={**state["integrity"], "receptionist": {"before": before, "after": None, "ok": None, "snapshot": gitutil.tracked_snapshot(Path(store.project))}}
            )
        except RuntimeError as e:
            store.set_error(f"无法建立完整性基线:{e}")

    return await rt.open_role("receptionist", cwd=store.project)


async def start_receptionist(rt: QuestRuntime, opening_message: Optional[str]) -> None:
    """立即建 session，把契约与开场问题合成首轮后放到后台执行。"""
    receiver = await open_receptionist(rt)
    task = receiver.begin_prompt(
        prompts.receptionist_first_message(rt.store, opening_message),
        visible_user_text=opening_message,
        kind="chat",
    )
    rt.track(task)
    # 让 prompt task 至少进入 HTTP 提交阶段，再把 create 请求交还给浏览器；
    # 不等待 Claude 回复，但可避免用户刚进页面就插话时跑到首轮之前。
    await asyncio.sleep(0)


async def continue_chat(rt: QuestRuntime, text: str) -> None:
    receiver = rt.get_role("receptionist")
    if receiver is None:
        raise RuntimeError("receptionist session 未运行")
    _, task = await receiver.submit_user(text)
    if task is not None:
        rt.track(task)


# ─────────────────────────────────────────────────────────────────────────────
# 生成需求单:receptionist 吐 markdown → 服务端规范化 frontmatter → 校验闸门
# ─────────────────────────────────────────────────────────────────────────────


async def generate_quest(
    rt: QuestRuntime,
    *,
    prompt_override: Optional[str] = None,
    visible_user_text: Optional[str] = None,
) -> dict[str, Any]:
    """触发 receptionist 输出需求单,落盘 quest.md。写不出可执行验收则拒绝。"""
    store = rt.store
    if store.read_state()["state"] != sm.DRAFTING:
        return {"ok": False, "reason": "只有 drafting 状态能生成需求单"}
    receiver = rt.get_role("receptionist")
    prompt_text = prompt_override or prompts.GENERATE_TRIGGER
    if receiver is None:
        receiver = await open_receptionist(rt)
        prompt_text = prompts.receptionist_first_message(store, prompt_text)
    if receiver.status()["busy"]:
        return {"ok": False, "reason": "前台 Agent 仍在工作，请等当前 turn 结束后再生成需求单"}

    receiver.publish_synthetic({"method": "_guildhall/generation_start", "params": {}})
    try:
        # 生成是单发结构化 turn：文本已经完整、但 adapter 丢失 stopReason 时，
        # 静默 30 秒后会 cancel 收口，随后仍用已经收到的 Markdown 落盘。
        raw = await receiver.begin_prompt(
            prompt_text,
            visible_user_text=visible_user_text,
            kind="generation",
            idle_timeout=RECEPTIONIST_GENERATION_IDLE_TIMEOUT,
        )
    except Exception as e:  # noqa: BLE001
        reason = f"生成需求单时 Agent 异常:{e}"
        store.set_error(reason)
        receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": {"ok": False, "reason": reason}})
        return {"ok": False, "reason": reason}
    if not raw:
        reason = "receptionist 没有输出任何内容"
        receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": {"ok": False, "reason": reason}})
        return {"ok": False, "reason": reason}

    md = normalize_quest_md(store, raw)
    if md is None:
        result = {
            "ok": False,
            "reason": "没有解析出需求单正文(需要包含「## 验收步骤」)。需求可能还没问清楚,继续追问后再试。",
        }
        receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": result})
        return result
    gate = st.acceptance_gate_error(md)
    if gate:
        result = {"ok": False, "reason": gate, "raw": md}
        receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": {"ok": False, "reason": gate}})
        return result
    violation = negative_step_violation(md)
    if violation:
        # §2.5.1:负向测试的机械校验,拒绝写盘,把原因回给 receptionist 让它重写
        result = {"ok": False, "reason": violation, "raw": md}
        receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": {"ok": False, "reason": violation}})
        return result
    store.write_quest_md(md)
    store.set_error(None)
    receiver.publish_synthetic({"method": "_guildhall/generation_end", "params": {"ok": True}})
    return {"ok": True, "quest_md": md}


def request_generate_quest(
    rt: QuestRuntime,
    *,
    prompt_override: Optional[str] = None,
    visible_user_text: Optional[str] = None,
    force_regenerate: bool = False,
) -> dict[str, Any]:
    """后台生成并落盘；HTTP 只负责受理，绝不等待 Claude 的完整一轮。"""
    if not force_regenerate:
        existing = rt.store.read_quest_md()
        if existing is not None:
            return {"ok": True, "quest_md": existing, "started": False}
        recovered = recover_generated_quest(rt.store)
        if recovered is not None:
            return {"ok": True, "quest_md": recovered, "started": False, "recovered": True}
    current = getattr(rt, "generation_task", None)
    if current is not None and not current.done():
        return {"ok": True, "started": True}
    receiver = rt.get_role("receptionist")
    if receiver is not None and receiver.status()["busy"]:
        return {"ok": False, "reason": "前台 Agent 仍在工作，请等当前 turn 结束后再生成需求单"}

    async def run() -> None:
        result = await generate_quest(
            rt,
            prompt_override=prompt_override,
            visible_user_text=visible_user_text,
        )
        if not result.get("ok"):
            rt.store.set_error(result.get("reason") or "生成需求单失败")

    task = asyncio.create_task(run(), name=f"generate:{rt.store.quest_id}")
    rt.generation_task = task
    rt.track(task)
    return {"ok": True, "started": True}


def recover_generated_quest(store: st.QuestStore) -> Optional[str]:
    """从事件旁路恢复“模型已输出、服务端却没等到轮末响应”的需求单。

    兼容修复前的 `user_message: 生成需求单` 标记和修复后的 generation_start。
    这里只在用户再次点击生成时调用；读出的正文仍必须经过规范化与验收闸门。
    """
    collecting = False
    messages: list[str] = []
    parts: list[str] = []
    message_id: Optional[str] = None

    def flush_message() -> None:
        nonlocal parts, message_id
        text = "".join(parts).strip()
        if text:
            messages.append(text)
        parts = []
        message_id = None

    for event in store.read_events("receptionist"):
        method = event.get("method")
        if method == "_guildhall/generation_start":
            flush_message()
            collecting = True
            messages = []
            continue
        if method == "_guildhall/user_message" and (event.get("params") or {}).get("text", "").strip() == prompts.GENERATE_TRIGGER:
            flush_message()
            collecting = True
            messages = []
            continue
        if not collecting:
            continue
        if method == "_guildhall/user_message":
            flush_message()
            continue
        update = (event.get("params") or {}).get("update") or {}
        if update.get("sessionUpdate") == "agent_message_chunk":
            next_message_id = update.get("messageId")
            if parts and next_message_id and message_id and next_message_id != message_id:
                flush_message()
            if next_message_id:
                message_id = next_message_id
            parts.append((update.get("content") or {}).get("text") or "")

    flush_message()
    for raw in reversed(messages):
        md = normalize_quest_md(store, raw)
        if md is None or st.acceptance_gate_error(md) or negative_step_violation(md):
            continue
        store.write_quest_md(md)
        store.set_error(None)
        return md
    return None


_FENCE_RE = re.compile(r"^(`{3,})[a-zA-Z0-9_-]*\n(.*?)\n?\1\s*$", re.DOTALL)
_FENCE_BLOCK_RE = re.compile(r"^(`{3,})[^\n]*\n(.*?)\n\1[ \t]*$", re.DOTALL | re.MULTILINE)
_FM_RE = re.compile(r"^---\n(.*?)\n---\n?", re.DOTALL)


def normalize_quest_md(store: st.QuestStore, raw: str) -> Optional[str]:
    """把 receptionist 的输出规范成 quest.md:剥围栏、重建 frontmatter(程序事实字段
    以服务端为准,id 与目录一致,不许 Receptionist 发明)。"""
    text = raw.strip()
    fenced_candidates = [
        match.group(2).strip()
        for match in _FENCE_BLOCK_RE.finditer(text)
        if "## 验收步骤" in match.group(2)
    ]
    if fenced_candidates:
        # Claude 常在四反引号围栏外加解释；quest.md 只收围栏内最后一份完整文档。
        text = fenced_candidates[-1]
    else:
        match = _FENCE_RE.match(text)
        if match:
            text = match.group(2).strip()
    # 正文从第一个 ## 章节开始,前面的闲话一律丢掉
    body_match = re.search(r"^## .*$", text, re.MULTILINE)
    if not body_match:
        return None
    body = text[body_match.start() :].strip()
    if "## 验收步骤" not in body:
        return None
    title = ""
    fm_title = re.search(r"^title:\s*(.+)$", text, re.MULTILINE)
    if fm_title:
        title = fm_title.group(1).strip()
    if not title:
        h1 = re.search(r"^#\s+(.+)$", text, re.MULTILINE)
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
    ok = entry["before"] == after and (not entry.get("snapshot") or entry["snapshot"] == gitutil.tracked_snapshot(Path(store.project)))
    state = store.update_state(
        integrity={**state["integrity"], "receptionist": {**entry, "after": after, "ok": ok}}
    )
    if not ok:
        # §6.2:receptionist 脏了只记 error、前端提示,流程继续——它在你的工作区,你看着办
        store.set_error("receptionist 结束时项目工作区的 tracked 文件与开始时不一致(见 integrity.receptionist)。流程继续,但请自己检查工作区。")


# ─────────────────────────────────────────────────────────────────────────────
# 派单(→ in_progress):worktree + adventurer(§2.6.2)
# ─────────────────────────────────────────────────────────────────────────────


async def dispatch_adventurer(
    rt: QuestRuntime,
    manager: SandboxManager,
    *,
    reuse_worktree: bool = False,
) -> None:
    store = rt.store
    state = store.read_state()
    repo = Path(store.project)
    worktree = Path(state["worktree"])
    branch = state["branch"]

    try:
        if reuse_worktree:
            if not worktree.is_dir() or not gitutil.is_repo(worktree):
                raise RuntimeError(f"重试所需 worktree 不存在或不是 Git 工作区:{worktree}")
            base = state.get("base_commit") or gitutil.head_commit(worktree)
        else:
            base = gitutil.create_worktree(repo, store.quest_id, worktree, branch)
        state = store.update_state(base_commit=base, target_branch=gitutil._run(["symbolic-ref", "--short", "HEAD"], cwd=repo).stdout.strip())
    except Exception as e:  # noqa: BLE001
        store.set_error(f"adventurer worktree 准备失败:{e}")
        _safe_transition(store, sm.FAILED)
        return
    log.info("worktree ready: %s @ %s (base %s)", worktree, branch, base[:12])

    quest_md = store.read_quest_md() or ""
    session_name = f"guildhall-{store.quest_id}-adventurer"
    session = manager.open_session(session_name, role="adventurer")
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
            await session.close()
            for path in gitutil.untracked_paths(worktree):
                if in_scope(path, scope_patterns(quest_md)):
                    gitutil._run(["add", "--", path], cwd=worktree)
            # adventurer session 结束事件 → in_progress → appraising(§2.5)
            _safe_transition(store, sm.APPRAISING)
            await run_appraisal(rt, manager)
        except Exception as e:  # noqa: BLE001
            log.exception("adventurer failed")
            store.set_error(f"adventurer 异常:{e}")
            _safe_transition(store, sm.DISPUTED if store.read_state()["state"] == sm.APPRAISING else sm.FAILED)
        finally:
            await session.close()

    await run()


# ─────────────────────────────────────────────────────────────────────────────
# 验收(§2.6.3):全新 appraiser session,输入白名单严格三样
# ─────────────────────────────────────────────────────────────────────────────


async def run_appraisal(rt: QuestRuntime, manager: SandboxManager) -> None:
    store = rt.store
    state = store.read_state()
    worktree = Path(state["worktree"])

    store.update_state(review_snapshot=None, review_quest=None)
    store.appraisal_json.unlink(missing_ok=True)
    # 进出一致基线:appraiser 开始前的 tracked 状态(adventurer 刚改完的样子)
    try:
        snapshot_before = gitutil.tracked_snapshot(worktree)
        untracked_before = gitutil.untracked_paths(worktree)
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
    session = manager.open_session(session_name, role="appraiser")
    try:
        await session.start()
        await session.new_session(cwd=str(worktree))
    except Exception:
        await session.close()
        raise
    state = store.read_state()
    store.update_state(sessions={**state["sessions"], "appraiser": session_name})
    rt.roles["appraiser"] = RoleRuntime(store, "appraiser", session)

    try:
        await session.prompt(prompts.appraiser_first_message(quest_md) + "\nchecks 必须从 1 连续编号，完整覆盖所有步骤；step 逐字复制验收步骤原文（不含编号），不得缩写。\n<git-diff>\n" + gitutil.diff_vs_base(worktree, state["base_commit"]) + "\n</git-diff>", idle_timeout=120)
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
    integrity_ok = before == after and snapshot_before == gitutil.tracked_snapshot(worktree)
    # §6.2:sha256 存进 state.json(after/ok 落盘,receptionist 同理)
    state = store.read_state()
    store.update_state(
        integrity={**state["integrity"], "appraiser": {"before": before, "after": after, "ok": integrity_ok}}
    )

    raw = "".join(session.turn_text).strip()
    appraisal: Optional[dict[str, Any]] = None
    parse_error: Optional[str] = None
    if raw:
        appraisal = parse_appraisal(raw, quest_md)
        if appraisal is None:
            parse_error = "appraisal 输出不是合法 JSON(§6.4:不重试,原始输出见 error)"

    if appraisal is not None:
        appraisal["invalidated"] = not integrity_ok
        store.update_state(review_snapshot=gitutil.tracked_snapshot(worktree), review_quest=quest_md)
        if integrity_ok:
            # 确定性防线(§2.6):服务端直接从 diff 抓偷改测试,不依赖 appraiser 的自觉。
            # 真模型的判断是 e2e 的职责,这里只做机械兜底。
            diff_text = gitutil.diff_vs_base(worktree, state.get("base_commit"))
            paths = gitutil.changed_paths(worktree, state["base_commit"]) + untracked_before
            appraisal["out_of_scope_files"] = sorted(set(appraisal["out_of_scope_files"]) | {p for p in paths if not in_scope(p, scope_patterns(quest_md))})
            store.update_state(review_snapshot=gitutil.tracked_snapshot(worktree), review_quest=quest_md)
            if detect_touched_tests(diff_text):
                appraisal["touched_tests"] = True
        store.write_appraisal(appraisal)
        store.set_error(None if integrity_ok else "appraiser 结束时 worktree 的 tracked 文件与开始时不一致:本次验收结论已整份作废(invalidated)。")

        if not integrity_ok:
            _safe_transition(store, sm.DISPUTED)
        elif appraisal.get("out_of_scope_files") or any(
            c.get("result") != "pass" for c in appraisal.get("checks", [])
        ):
            _safe_transition(store, sm.DISPUTED)
        else:
            _safe_transition(store, sm.APPRAISED)
    else:
        # §6.4 第 7 条:解析失败 → disputed + error 存原始输出,不重试
        store.set_error(f"appraisal.json 解析失败:{parse_error}\n原始输出:\n{raw}")
        _safe_transition(store, sm.DISPUTED)


def parse_appraisal(raw: str, quest_md: str | None = None) -> Optional[dict[str, Any]]:
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
    try:
        result = AppraisalResult.model_validate(data).model_dump()
    except ValidationError:
        return None
    checks = result["checks"]
    if [c["index"] for c in checks] != list(range(1, len(checks) + 1)):
        return None
    if any(c["unsatisfiable"] and c["result"] == "pass" for c in checks):
        return None
    if quest_md is not None:
        steps = [re.sub(r"^\d+\.\s*", "", s) for s in acceptance_steps(quest_md)]
        if len(checks) != len(steps):
            return None
        for c, s in zip(checks, steps):
            # appraiser 常把步骤下的命令块一并复制进 step,或改排换行缩进;
            # 空白归一后前缀匹配,只锚定步骤标题,标题被改写才算失配。
            if not _norm_ws(c["step"]).startswith(_norm_ws(s)):
                return None
    return result


def _norm_ws(s: str) -> str:
    return re.sub(r"\s+", " ", s).strip()


# 改了这些路径 = 改了测试/断言/CI(§2.6 确定性检测的判定面)
_TEST_PATH_RE = re.compile(
    r"(?:^|/)(?:tests?|spec|__tests__)/(?:|$)"        # 测试目录本身
    r"|(?:^|/)test_[^/]+\.py$"                        # test_*.py
    r"|(?:^|/)[^/]+_test\.(?:py|go|rb|rs)$"           # *_test.py/go/rb/rs
    r"|(?:^|/)[^/]+\.(?:test|spec)\.(?:js|jsx|ts|tsx|mjs)$"  # *.test.* / *.spec.*
    r"|(?:^|/)conftest\.py$"
    r"|(?:^|/)(?:jest|vitest)\.config\."
)
_CI_PATH_RE = re.compile(
    r"(?:^|/)\.github/workflows/"
    r"|(?:^|/)\.circleci/"
    r"|(?:^|/)\.gitlab-ci\.yml$"
    r"|(?:^|/)(?:Jenkinsfile|azure-pipelines\.yml|bitrise\.yml)$"
)


def detect_touched_tests(diff: str) -> bool:
    """从 unified diff 里机械判定是否动了测试文件、断言或 CI 配置。

    纯字符串检查,不过模型;是 touched_tests 的确定性兜底(M5 负向一,
    docs/m1-per-role-model.md 同期的测试基建)。
    """
    touched_paths: set[str] = set()
    for line in diff.splitlines():
        if line.startswith(("--- ", "+++ ")):
            path = line[4:].strip()
            if path == "/dev/null" or path.startswith(("a/", "b/")):
                path = path[2:]
            if path and path != "/dev/null":
                touched_paths.add(path)
    return any(_TEST_PATH_RE.search(p.lower()) or _CI_PATH_RE.search(p) for p in touched_paths)


def _safe_transition(store: st.QuestStore, to: str) -> None:
    try:
        store.transition(to)
    except sm.InvalidTransition:
        log.warning("transition to %s already handled (state=%s)", to, store.read_state()["state"])
