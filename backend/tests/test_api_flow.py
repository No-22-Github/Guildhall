"""M2–M6 管线验收(假 sandbox-agent,不烧模型)。

M5 的两条负向测试在此覆盖 guildhall 侧的接线:
1. appraiser 报 touched_tests=true → 必须 disputed。
2. appraiser 弄脏 worktree(tracked 文件不还原)→ 结论整份作废(invalidated),必须 disputed。
模型本身的抓作弊能力由 scripts/e2e_check.sh 用真模型验证。
"""

from __future__ import annotations

import asyncio
import json
import time

import pytest

from fake_acp import chunk

QUEST_MD_TEMPLATE = """\
---
id: {qid}
title: 同步上游新增的量化代码
project: {project}
created: 2026-09-04T14:32:11+08:00
---

## 目标

上游新增了一组量化实现,本仓库对应模块需要跟进。

## 约束

- 不引入新的第三方依赖

## 现状调研

> 以下由receptionist 读取代码得出,**允许在实施中推翻**。推翻了必须在完成报告里写明哪一条、为什么。

- 量化代码集中在 `src/`

## 改动范围

只允许修改以下路径,超出即视为越界:

- `src/**`

## 验收步骤

1. `python -m src.main` 退出码为 0
2. 【负向】把 main() 的返回值改错一位,测试必须失败

## 产物

- 分支:`guildhall/fake-branch`
"""


@pytest.fixture()
def wired(fake_acp):
    """装好三个角色的脚本,返回可查询 prompts 的句柄。"""
    async def receptionist_script(text: str):
        if "需求拷问者" in text:
            return ([chunk("上游是哪个仓库哪个 commit?")], "上游是哪个仓库哪个 commit?", "end_turn")
        if text.strip() == "生成需求单":
            return ([chunk("好的,这是需求单。")], "好的,这是需求单。\n\n" + QUEST_MD_TEMPLATE.format(qid="PLACEHOLDER", project="PLACEHOLDER"), "end_turn")
        return ([chunk("收到,继续。")], "收到,继续。", "end_turn")

    async def adventurer_script(text: str):
        return (
            [
                {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Terminal", "kind": "execute", "status": "pending", "rawInput": {}},
                {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed", "content": []},
            ],
            "完成报告:改了 src/main.py,推翻调研 0 条,验收步骤 1 已跑,通过。",
            "end_turn",
        )

    async def appraiser_script(text: str):
        appraisal = {
            "checks": [
                {"index": 1, "step": "python -m src.main 退出码为 0", "result": "pass", "evidence": "ran"},
                {"index": 2, "step": "负向", "result": "pass", "evidence": "ran"},
            ],
            "touched_tests": False,
            "out_of_scope_files": [],
            "summary": "实现与需求单一致。",
        }
        return ([chunk("逐条验收完成。")], json.dumps(appraisal, ensure_ascii=False), "end_turn")

    fake_acp.set_script("receptionist", receptionist_script)
    fake_acp.set_script("adventurer", adventurer_script)
    fake_acp.set_script("appraiser", appraiser_script)
    return fake_acp


def _wait_state(client, qid, states, timeout=15.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        st = client.get(f"/api/quests/{qid}").json()["state"]["state"]
        if st in states:
            return st
        time.sleep(0.1)
    raise AssertionError(f"timeout waiting for {states}, last={st}")


def _wait_dir(path, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if path.exists():
            return
        time.sleep(0.05)
    raise AssertionError(f"timeout waiting for dir {path}")


def test_full_pipeline_to_settled(client, wired, demo_repo, tmp_guildhall):
    # 注册项目
    r = client.post("/api/projects", json={"path": str(demo_repo)})
    assert r.status_code == 201

    # 新建委托 → drafting
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "上游那个量化的东西更新了,我这边得跟一下"})
    assert r.status_code == 201
    qid = r.json()["id"]

    body = client.get(f"/api/quests/{qid}").json()
    assert body["state"]["state"] == "drafting"
    assert body["quest_md"] is None

    # 对话
    r = client.post(f"/api/quests/{qid}/message", json={"text": "上游是 github.com/x/y,commit abc123"})
    assert r.status_code == 200
    # 收到的 prompt 应该是:系统契约(逐字)+ 用户两轮;回答是脚本的事
    receptionist_prompts = [t for sid, t in wired.prompts if sid.endswith("receptionist")]
    assert any("需求拷问者" in t for t in receptionist_prompts)  # §2.6.1 契约已投喂
    assert receptionist_prompts[-1].endswith("上游是 github.com/x/y,commit abc123")

    # 生成需求单(闸门通过,frontmatter 被服务端规范化)
    r = client.post(f"/api/quests/{qid}/generate")
    assert r.json()["ok"] is True, r.json()
    md = r.json()["quest_md"]
    assert "## 验收步骤" in md
    assert f"id: {qid}" in md  # 服务端盖章,不信 receptionist
    assert "PLACEHOLDER" not in md

    # 手改 quest.md(§2.3:人可以直接手改)
    client.put(f"/api/quests/{qid}/quest", content=md + "\n(手改:补充验收环境说明)\n")

    # 张贴 → posted
    r = client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    assert r.status_code == 200, r.text
    assert r.json()["state"] == "posted"

    # 表外转移必须 409(M1 负向的 API 层)
    r = client.post(f"/api/quests/{qid}/transition", json={"to": "ready"})
    assert r.status_code == 409

    # 派单 → in_progress →(脚本 adventurer 收工)→ appraising → appraised
    r = client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    assert r.status_code == 200
    st = _wait_state(client, qid, {"appraised", "disputed", "failed"})
    assert st == "appraised", client.get(f"/api/quests/{qid}").json()["state"]

    # worktree 与 diff
    body = client.get(f"/api/quests/{qid}").json()
    assert body["state"]["base_commit"]
    assert "sessions" in body["state"]

    # 验收结论
    ap = client.get(f"/api/quests/{qid}/appraisal").json()
    assert ap["touched_tests"] is False
    assert ap["invalidated"] is False
    assert all(c["result"] == "pass" for c in ap["checks"])

    # 接受 → settled;不许自动跳步:appraised 只能去 settled
    r = client.post(f"/api/quests/{qid}/transition", json={"to": "settled"})
    assert r.status_code == 200
    assert client.get(f"/api/quests/{qid}").json()["state"]["state"] == "settled"


def test_touched_tests_goes_disputed(client, wired, demo_repo, tmp_guildhall):
    """M5 负向 1:adventurer 偷改测试让测试变绿 → touched_tests 必须 true → disputed。"""
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    wt = tmp_guildhall / "worktrees" / qid
    cheat = wt / "tests" / "test_main.py"

    async def cheating_adventurer(text):
        # 「作弊」写进 worktree 之后才收工,保证 appraiser 基线里已含作弊 diff
        # (等内容而不是存在:checkout 之后这个文件本来就在)
        while "被偷改的断言" not in cheat.read_text():
            await asyncio.sleep(0.05)
        return (
            [{"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Edit", "kind": "edit", "status": "pending", "rawInput": {}}],
            "完成报告:全绿(其实改了断言)。",
            "end_turn",
        )

    wired.set_script("adventurer", cheating_adventurer)

    client.post(f"/api/quests/{qid}/generate")
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})

    _wait_dir(wt / "tests")  # 等 worktree 建好(dispatch 是后台任务)
    cheat.write_text("def test_main():\n    assert True  # 被偷改的断言\n")

    async def cheating_appraiser(text):
        appraisal = {"checks": [{"index": 1, "step": "任意", "result": "pass", "evidence": "ran"}],
                     "touched_tests": True, "out_of_scope_files": [], "summary": "diff 里改了测试断言。"}
        return ([chunk("...")], json.dumps(appraisal), "end_turn")

    wired.set_script("appraiser", cheating_appraiser)

    st = _wait_state(client, qid, {"appraised", "disputed"})
    assert st == "disputed"
    ap = client.get(f"/api/quests/{qid}/appraisal").json()
    assert ap["touched_tests"] is True
    state = client.get(f"/api/quests/{qid}").json()["state"]
    assert state["error"] is None  # 不是错误,是验收结论:disputed
    # 这条断言的靶子:touched_tests=true 时前端不许展示全绿


def test_appraiser_pollution_invalidates(client, wired, demo_repo, tmp_guildhall):
    """M5 负向 2:appraiser 跑负向测试顺手不还原 → 结论整份作废 → disputed。"""
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    wt = tmp_guildhall / "worktrees" / qid
    pollution = wt / "src" / "main.py"

    async def lingering_appraiser(text):
        # appraiser 污染 worktree 后不再还原(哨兵:污染内容出现才吐结论)
        while "appraiser 忘了还原" not in pollution.read_text():
            await asyncio.sleep(0.05)
        appraisal = {"checks": [{"index": 1, "step": "任意", "result": "pass", "evidence": "ran"}],
                     "touched_tests": False, "out_of_scope_files": [], "summary": "全过。"}
        return ([chunk("...")], json.dumps(appraisal), "end_turn")

    wired.set_script("appraiser", lingering_appraiser)

    client.post(f"/api/quests/{qid}/generate")
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    _wait_state(client, qid, {"in_progress", "appraising"})

    # 等 appraiser 基线记录完毕,再污染
    deadline = time.time() + 10
    while time.time() < deadline:
        state = client.get(f"/api/quests/{qid}").json()["state"]
        if state["integrity"].get("appraiser", {}).get("before"):
            break
        time.sleep(0.05)
    assert state["integrity"]["appraiser"]["before"], "appraiser integrity baseline missing"
    pollution.write_text("def main():\n    return 42  # appraiser 忘了还原\n")

    st = _wait_state(client, qid, {"appraised", "disputed"}, timeout=20)
    assert st == "disputed"
    ap = client.get(f"/api/quests/{qid}/appraisal").json()
    assert ap["invalidated"] is True  # 整份作废
    assert ap["checks"]  # 结论还在,但已作废
    assert client.get(f"/api/quests/{qid}").json()["state"]["error"]  # error 指明作废


def test_appraisal_parse_failure_no_retry(client, wired, demo_repo):
    """§6.4:解析失败 → disputed + error 存原始输出,不重试。"""
    async def bad_appraiser(text):
        return ([chunk("...")], "我觉得应该没问题,全过。", "end_turn")  # 不是 JSON

    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    client.post(f"/api/quests/{qid}/generate")
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    wired.set_script("appraiser", bad_appraiser)

    st = _wait_state(client, qid, {"appraised", "disputed"})
    assert st == "disputed"
    state = client.get(f"/api/quests/{qid}").json()["state"]
    assert "解析失败" in state["error"]
    assert "全过" in state["error"]  # 原始输出在 error 里
    with pytest.raises(AssertionError):
        client.get(f"/api/quests/{qid}/appraisal")  # 404:没写 appraisal.json
        raise AssertionError("should be 404")


def test_generate_rejected_without_acceptance(client, wired, demo_repo):
    """receptionist 写不出可执行验收 → 生成被拒,不许落盘。"""
    async def bad_receptionist(text):
        if "需求拷问者" in text:
            return ([chunk("问题?")], "问题?", "end_turn")
        if text.strip() == "生成需求单":
            return ([chunk("写不出来")], "## 目标\n\n做点什么。\n\n## 验收步骤\n\n(待定)", "end_turn")
        return ([chunk("嗯")], "嗯", "end_turn")

    wired.set_script("receptionist", bad_receptionist)
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "模糊的想法"})
    qid = r.json()["id"]
    r = client.post(f"/api/quests/{qid}/generate")
    assert r.json()["ok"] is False
    assert client.get(f"/api/quests/{qid}").json()["quest_md"] is None


def test_sse_replay_from_jsonl(client, wired, demo_repo):
    """事件旁路:jsonl 是权威重放源;连接断开/刷新后从 offset 续。"""
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "第一句话"})
    qid = r.json()["id"]
    client.post(f"/api/quests/{qid}/message", json={"text": "第二句话"})
    # 用 withdrawn 关闭 receptionist session(posted 有验收步骤闸门,这里没生成需求单)
    r = client.post(f"/api/quests/{qid}/transition", json={"to": "withdrawn"})
    assert r.status_code == 200

    # 全量重放:含合成 user_message 与 agent 增量
    with client.stream("GET", f"/api/quests/{qid}/events/receptionist?offset=0") as resp:
        payload = b"".join(resp.iter_bytes()).decode()
    assert "_guildhall/user_message" in payload
    assert "第一句话" in payload
    assert "agent_message_chunk" in payload

    # offset 续读:从「第二句话」那条合成事件起截取
    events = [e for e in payload.split("\n\n") if "data:" in e]
    cut = next(i for i, e in enumerate(events) if "第二句话" in e)
    with client.stream("GET", f"/api/quests/{qid}/events/receptionist?offset={cut}") as resp:
        tail = b"".join(resp.iter_bytes()).decode()
    assert "第一句话" not in tail  # 之前的整段不重放
    assert "第二句话" in tail


def test_in_progress_blocks_quest_md_edit_and_second_dispatch(client, wired, demo_repo, tmp_guildhall):
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    release = tmp_guildhall / "worktrees" / qid / ".release"

    async def holding_adventurer(text):
        # 哨兵:release 文件出现才收工,把 quest 钉在 in_progress
        import os as _os
        while not _os.path.exists(release):
            await asyncio.sleep(0.05)
        return ([], "完成报告。", "end_turn")

    wired.set_script("adventurer", holding_adventurer)
    client.post(f"/api/quests/{qid}/generate")
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    assert client.get(f"/api/quests/{qid}").json()["state"]["state"] == "in_progress"

    # in_progress 不许改 quest.md(§3 不得清单)
    r = client.put(f"/api/quests/{qid}/quest", content="---\nid: x\n---\n\n## 验收步骤\n\n1. hack\n")
    assert r.status_code == 409

    # 第二单不许同时 in_progress
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "第二单"})
    qid2 = r.json()["id"]
    client.post(f"/api/quests/{qid2}/generate")
    client.post(f"/api/quests/{qid2}/transition", json={"to": "posted"})
    r = client.post(f"/api/quests/{qid2}/transition", json={"to": "in_progress"})
    assert r.status_code == 409

    # 放行第一单
    release.write_text("go\n")
    _wait_state(client, qid, {"appraised", "disputed", "failed"}, timeout=20)
    _wait_state(client, qid2, {"posted"})
