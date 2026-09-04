"""M1 验收:状态机 + 磁盘布局 + quest.md 闸门。

规格书 §7:M1 必须包含负向测试——构造「## 验收步骤」为空的 quest.md,
尝试 drafting → posted,必须被拒绝;表外转移(如 drafting → ready)必须抛异常。
"""

from __future__ import annotations

import pytest

from guildhall import layout, questmd, statemachine as sm, store as st


# ── 状态机:封闭转移表 ────────────────────────────────────────────────────────


@pytest.mark.parametrize(
    "frm,to",
    [
        (sm.DRAFTING, sm.POSTED),
        (sm.DRAFTING, sm.WITHDRAWN),
        (sm.POSTED, sm.IN_PROGRESS),
        (sm.IN_PROGRESS, sm.APPRAISING),
        (sm.IN_PROGRESS, sm.FAILED),
        (sm.APPRAISING, sm.APPRAISED),
        (sm.APPRAISING, sm.DISPUTED),
        (sm.APPRAISED, sm.SETTLED),
        (sm.DISPUTED, sm.SETTLED),
        (sm.DISPUTED, sm.WITHDRAWN),
    ],
)
def test_legal_transitions(frm, to):
    sm.assert_transition(frm, to)


@pytest.mark.parametrize(
    "frm,to",
    [
        (sm.DRAFTING, "ready"),  # M1 负向:规格书点名的表外转移
        (sm.DRAFTING, sm.IN_PROGRESS),  # 不许跳过 posted
        (sm.POSTED, sm.SETTLED),
        (sm.APPRAISED, sm.DISPUTED),  # 全过了不许打回
        (sm.APPRAISED, sm.WITHDRAWN),  # appraised 唯一出口是 settled
        (sm.SETTLED, sm.DRAFTING),  # 终态无出口
        (sm.FAILED, sm.IN_PROGRESS),
        (sm.WITHDRAWN, sm.POSTED),
        (sm.DISPUTED, sm.IN_PROGRESS),
        (None, sm.POSTED),  # 新建只许进 drafting
    ],
)
def test_illegal_transitions_raise(frm, to):
    with pytest.raises(sm.InvalidTransition):
        sm.assert_transition(frm, to)


def test_transition_persists_history(tmp_guildhall, make_quest):
    store = make_quest()
    store.write_quest_md(_valid_quest_md(store))
    store.transition(sm.POSTED)
    store.transition(sm.IN_PROGRESS)
    state = store.read_state()
    assert [h["to"] for h in state["history"]] == ["drafting", "posted", "in_progress"]
    assert state["state"] == sm.IN_PROGRESS


# ── quest.md 闸门:空验收步骤的单不许张贴 ────────────────────────────────────


def test_empty_acceptance_blocks_posting(tmp_guildhall, make_quest):
    store = make_quest()
    md = f"""\
---
id: {store.quest_id}
title: 空验收
project: {store.project}
created: 2026-09-04T14:32:11+08:00
---

## 目标

随便改改。

## 验收步骤

(待定,还没想好怎么验)
"""
    store.write_quest_md(md)
    assert store.check_postable() is not None  # 闸门必须拦
    with pytest.raises(st.GateRejected):  # 转移本身被拒绝,原因进 error
        store.transition(sm.POSTED)
    assert store.read_state()["state"] == sm.DRAFTING
    assert store.read_state()["error"] is not None


def test_valid_acceptance_allows_posting(tmp_guildhall, make_quest):
    store = make_quest()
    store.write_quest_md(_valid_quest_md(store))
    assert store.check_postable() is None
    state = store.transition(sm.POSTED)
    assert state["state"] == sm.POSTED


def test_missing_quest_md_blocks_posting(make_quest):
    store = make_quest()
    assert store.check_postable() is not None


def test_acceptance_steps_extraction():
    body = """\
## 验收步骤

1. `swift build` 退出码为 0
2. 【负向】把常量改错一位,测试必须失败

## 产物

- 分支:`x`
"""
    steps = questmd.acceptance_steps(body)
    assert len(steps) == 2
    assert "负向" in steps[1]


# ── 磁盘布局:路径转义逐字规则 ───────────────────────────────────────────────


def test_escape_project_path():
    assert layout.escape_project_path("/Users/no22/code/preen") == "-Users-no22-code-preen"
    assert layout.escape_project_path("/tmp/x") == "-tmp-x"


def test_quest_id_format():
    qid = layout.make_quest_id("20260904-1432", "同步 上游 Quant 代码")
    assert layout.QUEST_ID_RE.match(qid)
    assert qid.startswith("20260904-1432-")


def test_slug_rules():
    assert layout.slugify("Hello World Foo") == "hello-world-foo"
    assert len(layout.slugify("x" * 100)) <= 40
    assert layout.slugify("中文没有词元") == "draft"


def test_quest_store_layout(tmp_guildhall, make_quest):
    store: st.QuestStore = make_quest()
    assert (store.dir / "events").is_dir()
    state = store.read_state()
    assert state["state"] == "drafting"
    assert state["sessions"]["receptionist"] is None
    assert state["offsets"] == {"receptionist": 0, "adventurer": 0, "appraiser": 0}
    assert state["branch"] == f"guildhall/{store.quest_id}"
    assert state["worktree"].endswith(store.quest_id)
    assert state["error"] is None


def test_events_jsonl_append_and_replay(tmp_guildhall, make_quest):
    store = make_quest()
    a = store.append_event("receptionist", {"n": 1})
    b = store.append_event("receptionist", {"n": 2})
    store.append_event("adventurer", {"n": 99})
    assert (a, b) == (0, 1)
    assert store.read_events("receptionist") == [{"n": 1}, {"n": 2}]
    assert store.read_events("receptionist", offset=1) == [{"n": 2}]
    assert store.read_events("appraiser") == []


# ── helpers ──────────────────────────────────────────────────────────────────


def _valid_quest_md(store) -> str:
    return f"""\
---
id: {store.quest_id}
title: 同步上游新增的量化代码
project: {store.project}
created: 2026-09-04T14:32:11+08:00
---

## 目标

上游仓库新增了一组量化实现,本仓库对应模块需要跟进,保持行为一致。

## 约束

- 不引入新的第三方依赖

## 现状调研

> 以下由receptionist 读取代码得出,**允许在实施中推翻**。推翻了必须在完成报告里写明哪一条、为什么。

- 量化相关代码集中在 `Sources/PreenCore/Quant/`

## 改动范围

只允许修改以下路径,超出即视为越界:

- `Sources/PreenCore/Quant/**`

## 验收步骤

1. `swift build` 退出码为 0
2. 【负向】把新增量化函数的某个常量改错一位,`swift test --filter QuantTests` 必须失败

## 产物

- 分支:`{store.quest_id}`
- worktree:`~/.guildhall/worktrees/{store.quest_id}`
"""
