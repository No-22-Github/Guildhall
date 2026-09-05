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


def _wait_receptionist_idle(client, qid, timeout=10.0):
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/quests/{qid}").json()
        status = body.get("runtime", {}).get("receptionist")
        if status and not status["busy"]:
            return body
        time.sleep(0.05)
    raise AssertionError("timeout waiting for receptionist to become idle")


def _generate(client, qid, timeout=10.0):
    _wait_receptionist_idle(client, qid, timeout=timeout)
    response = client.post(f"/api/quests/{qid}/generate")
    assert response.status_code == 200
    assert response.json()["ok"] is True, response.json()
    deadline = time.time() + timeout
    while time.time() < deadline:
        body = client.get(f"/api/quests/{qid}").json()
        if body["quest_md"] is not None:
            return body["quest_md"]
        status = body.get("runtime", {}).get("receptionist")
        if status and not status["busy"] and body["state"].get("error"):
            return None
        time.sleep(0.05)
    raise AssertionError("timeout waiting for quest.md generation")


def test_create_returns_before_first_model_turn_and_includes_opening(client, fake_acp, demo_repo):
    """新建只等 ACP session 就绪，不把页面跳转绑在 Claude 的整轮回复上。"""
    async def slow_receptionist(text):
        await asyncio.sleep(0.6)
        return ([chunk("收到开场问题")], "收到开场问题", "end_turn")

    fake_acp.set_script("receptionist", slow_receptionist)
    client.post("/api/projects", json={"path": str(demo_repo)})
    started = time.monotonic()
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "这是我的真实问题"})
    elapsed = time.monotonic() - started

    assert response.status_code == 201
    assert elapsed < 0.4, f"create blocked for {elapsed:.2f}s"
    qid = response.json()["id"]
    status = client.get(f"/api/quests/{qid}").json()["runtime"]["receptionist"]
    assert status["busy"] is True
    _wait_receptionist_idle(client, qid)
    first_prompt = next(text for sid, text in fake_acp.prompts if sid.endswith("receptionist"))
    assert "需求拷问者" in first_prompt
    assert "<user-request>\n这是我的真实问题\n</user-request>" in first_prompt


def test_message_uses_claude_steering_while_turn_is_running(client, fake_acp, demo_repo, tmp_guildhall):
    """工作中发送不排队成下一轮，而是走 claude-agent-acp 的 steering 注入。"""
    release = tmp_guildhall / "release-receptionist"

    async def holding_receptionist(text):
        while not release.exists():
            await asyncio.sleep(0.02)
        return ([chunk("处理完成")], "处理完成", "end_turn")

    fake_acp.set_script("receptionist", holding_receptionist)
    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "先查代码"})
    qid = response.json()["id"]

    deadline = time.time() + 3
    sid = f"guildhall-{qid}-receptionist"
    while time.time() < deadline and fake_acp.conn(sid).active_prompts == 0:
        time.sleep(0.02)
    assert fake_acp.conn(sid).active_prompts == 1

    response = client.post(f"/api/quests/{qid}/message", json={"text": "补充：只看 src"})
    assert response.status_code == 200
    assert (sid, "补充：只看 src") in fake_acp.prompts
    assert fake_acp.conn(sid).active_prompts == 1  # steering 没再开第二个 prompt

    release.write_text("go\n")
    _wait_receptionist_idle(client, qid)


def test_generate_writes_quest_when_stop_reason_is_lost(client, wired, demo_repo, monkeypatch):
    """模型文本已完整时，即使轮末响应丢失也必须收口并把 quest.md 写到磁盘。"""
    from guildhall import flow

    monkeypatch.setattr(flow, "RECEPTIONIST_GENERATION_IDLE_TIMEOUT", 0.1)
    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)

    wired.drop_prompt_response = True
    md = _generate(client, qid, timeout=5)
    assert md is not None
    assert "## 验收步骤" in md
    assert client.get(f"/api/quests/{qid}").json()["state"]["error"] is None


def test_generation_idle_fallback_runs_while_prompt_post_is_still_open(client, wired, demo_repo, monkeypatch):
    """真实 adapter 会保持 POST；SSE 已输出完时空闲看门狗仍必须能收尾。"""
    from guildhall import flow

    monkeypatch.setattr(flow, "RECEPTIONIST_GENERATION_IDLE_TIMEOUT", 0.1)
    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)

    wired.hold_prompt_response = True
    started = time.monotonic()
    md = _generate(client, qid, timeout=5)
    assert md is not None
    assert time.monotonic() - started < 3
    assert "## 验收步骤" in md


def test_agent_exit_zero_with_output_is_treated_as_completed_turn(client, wired, demo_repo):
    """Claude 正常 exit 0 被 sandbox-agent 包成 500 时，已有正文不得误判失败。"""
    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)

    wired.agent_exit_code = 0
    md = _generate(client, qid, timeout=5)
    assert md is not None
    assert "## 验收步骤" in md
    assert client.get(f"/api/quests/{qid}").json()["state"]["error"] is None


def test_generate_recovers_valid_legacy_output_from_event_log(client, demo_repo):
    """修复前已吐到聊天里的完整需求单，再点生成时应直接恢复，不重跑模型。"""
    from guildhall import store as store_module

    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": None})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)
    quest = store_module.find_quest(qid)
    assert quest is not None
    quest.append_event("receptionist", {"method": "_guildhall/user_message", "params": {"text": "生成需求单"}})
    legacy_md = QUEST_MD_TEMPLATE.format(qid=qid, project=demo_repo)
    quest.append_event("receptionist", {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": legacy_md}}},
    })

    response = client.post(f"/api/quests/{qid}/generate")
    assert response.json()["ok"] is True
    assert response.json()["recovered"] is True
    assert response.json()["quest_md"] == client.get(f"/api/quests/{qid}").json()["quest_md"].rstrip()


def test_generate_recovers_latest_complete_output_without_concatenating_versions(client, demo_repo):
    """旧会话里先后吐过两版需求单时，应只恢复最新一版，不能拼接成重复文档。"""
    from guildhall import store as store_module

    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": None})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)
    quest = store_module.find_quest(qid)
    assert quest is not None
    quest.append_event("receptionist", {"method": "_guildhall/user_message", "params": {"text": "生成需求单"}})
    old_md = QUEST_MD_TEMPLATE.format(qid=qid, project=demo_repo).replace("同步上游新增的量化代码", "旧版本需求单")
    new_md = QUEST_MD_TEMPLATE.format(qid=qid, project=demo_repo).replace("同步上游新增的量化代码", "最新版本需求单")
    quest.append_event("receptionist", {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_message_chunk", "messageId": "old", "content": {"text": old_md}}},
    })
    quest.append_event("receptionist", {"method": "_guildhall/user_message", "params": {"text": "重新生成一下"}})
    quest.append_event("receptionist", {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_message_chunk", "messageId": "new", "content": {"text": new_md}}},
    })

    response = client.post(f"/api/quests/{qid}/generate")
    assert response.json()["recovered"] is True
    assert "最新版本需求单" in response.json()["quest_md"]
    assert "旧版本需求单" not in response.json()["quest_md"]


def test_generate_recovery_strips_outer_four_tick_fence_and_postscript(client, demo_repo):
    """Claude 用四反引号包需求单并在围栏后解释时，落盘不得带围栏和附言。"""
    from guildhall import store as store_module

    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": None})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)
    quest = store_module.find_quest(qid)
    assert quest is not None
    quest.append_event("receptionist", {"method": "_guildhall/user_message", "params": {"text": "生成需求单"}})
    fenced = (
        "需求单如下：\n\n````markdown\n"
        + QUEST_MD_TEMPLATE.format(qid=qid, project=demo_repo)
        + "\n````\n\n这句是围栏外说明，不应落盘。"
    )
    quest.append_event("receptionist", {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_message_chunk", "messageId": "fenced", "content": {"text": fenced}}},
    })

    response = client.post(f"/api/quests/{qid}/generate")
    md = response.json()["quest_md"]
    assert "````" not in md
    assert "围栏外说明" not in md
    assert md.rstrip().endswith("- 分支:`guildhall/fake-branch`")


def test_natural_language_regenerate_message_writes_quest(client, wired, demo_repo):
    """聊天框里的“重新生成”应走 generation turn，并把结果落到右侧 quest.md。"""
    from guildhall import store as store_module

    async def receptionist_script(text: str):
        if "重新生成一下" in text:
            return ([], QUEST_MD_TEMPLATE.format(qid="PLACEHOLDER", project="PLACEHOLDER"), "end_turn")
        return ([chunk("继续说")], "继续说", "end_turn")

    wired.set_script("receptionist", receptionist_script)
    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "先聊需求"})
    qid = response.json()["id"]
    _wait_receptionist_idle(client, qid)

    response = client.post(f"/api/quests/{qid}/message", json={"text": "重新生成一下"})
    assert response.status_code == 200
    assert response.json()["ok"] is True
    assert response.json()["started"] is True

    deadline = time.time() + 5
    while time.time() < deadline:
        body = client.get(f"/api/quests/{qid}").json()
        if body["quest_md"]:
            break
        time.sleep(0.05)
    assert body["quest_md"] is not None
    assert "## 验收步骤" in body["quest_md"]
    methods = [event.get("method") for event in store_module.find_quest(qid).read_events("receptionist")]
    assert "_guildhall/generation_start" in methods
    assert "_guildhall/generation_end" in methods


def test_generation_request_detection_does_not_capture_discussion_sentences():
    from guildhall.flow import is_generation_request

    assert is_generation_request("重新生成一下")
    assert is_generation_request("请生成一份需求单")
    assert is_generation_request("再出一份需求单吧")
    assert not is_generation_request("为什么要重新生成？")
    assert not is_generation_request("先不要重新生成")
    assert not is_generation_request("我们讨论一下需求单如何生成")


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
    _wait_receptionist_idle(client, qid)
    r = client.post(f"/api/quests/{qid}/message", json={"text": "上游是 github.com/x/y,commit abc123"})
    assert r.status_code == 200
    _wait_receptionist_idle(client, qid)
    # 首轮必须同时包含系统契约与用户开场白，后续消息独立发送。
    receptionist_prompts = [t for sid, t in wired.prompts if sid.endswith("receptionist")]
    assert "需求拷问者" in receptionist_prompts[0]
    assert "<user-request>\n上游那个量化的东西更新了,我这边得跟一下\n</user-request>" in receptionist_prompts[0]
    assert receptionist_prompts[-1] == "上游是 github.com/x/y,commit abc123"

    # 生成需求单(闸门通过,frontmatter 被服务端规范化)
    md = _generate(client, qid)
    assert md is not None
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


def test_failed_adventurer_can_retry_with_existing_worktree(client, wired, demo_repo, tmp_guildhall):
    """后端重启打断首轮后，重试应复用 worktree，不能因分支已存在再次失败。"""
    from guildhall import gitutil, store as store_module

    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
    qid = response.json()["id"]
    md = _generate(client, qid)
    assert md is not None
    quest = store_module.find_quest(qid)
    assert quest is not None
    quest.transition("posted")
    quest.transition("in_progress")
    state = quest.read_state()
    base = gitutil.create_worktree(demo_repo, qid, tmp_guildhall / "worktrees" / qid, state["branch"])
    quest.update_state(base_commit=base)
    quest.set_error("adventurer 异常:Server disconnected without sending a response.")
    quest.transition("failed")

    response = client.post(f"/api/quests/{qid}/retry-adventurer")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "in_progress"
    final = _wait_state(client, qid, {"appraised", "disputed", "failed"})
    assert final == "appraised", client.get(f"/api/quests/{qid}").json()["state"]
    history = client.get(f"/api/quests/{qid}").json()["state"]["history"]
    assert any(item.get("recovery") == "retry" for item in history)


def test_cleanly_exited_adventurer_can_resume_directly_to_appraisal(client, wired, demo_repo, tmp_guildhall):
    """历史上被误判 failed 的 exit-0 冒险者应直接验收，不得重复执行实现。"""
    from guildhall import gitutil, store as store_module

    client.post("/api/projects", json={"path": str(demo_repo)})
    response = client.post("/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
    qid = response.json()["id"]
    md = _generate(client, qid)
    assert md is not None
    quest = store_module.find_quest(qid)
    assert quest is not None
    quest.transition("posted")
    quest.transition("in_progress")
    state = quest.read_state()
    base = gitutil.create_worktree(demo_repo, qid, tmp_guildhall / "worktrees" / qid, state["branch"])
    quest.update_state(base_commit=base)
    quest.append_event("adventurer", {
        "method": "session/update",
        "params": {"update": {"sessionUpdate": "agent_message_chunk", "content": {"text": "实现完成"}}},
    })
    quest.append_event("adventurer", {
        "method": "_adapter/agent_exited",
        "params": {"code": 0, "success": True},
    })
    quest.set_error("adventurer 异常:session/prompt HTTP 500: agent_process_exited")
    quest.transition("failed")

    adventurer_prompts_before = len([sid for sid, _ in wired.prompts if sid.endswith("adventurer")])
    response = client.post(f"/api/quests/{qid}/resume-appraisal")
    assert response.status_code == 200, response.text
    assert response.json()["state"] == "appraising"
    assert _wait_state(client, qid, {"appraised", "disputed", "failed"}) == "appraised"
    adventurer_prompts_after = len([sid for sid, _ in wired.prompts if sid.endswith("adventurer")])
    assert adventurer_prompts_after == adventurer_prompts_before


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

    assert _generate(client, qid) is not None
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

    assert _generate(client, qid) is not None
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
    assert _generate(client, qid) is not None
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
    assert _generate(client, qid) is None
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


@pytest.fixture()
def live_server(tmp_guildhall, fake_acp, demo_repo):
    """真实 uvicorn(线程内)+ httpx:TestClient 在当前 starlette 版本下会整包缓冲
    流式响应,无限 SSE(状态流)必须走真 socket 才能测。"""
    import httpx
    import socket
    import threading
    import uvicorn

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

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    for _ in range(100):
        try:
            if httpx.get(f"{base}/api/projects", timeout=0.5).status_code == 200:
                break
        except Exception:  # noqa: BLE001
            time.sleep(0.05)
    else:
        raise RuntimeError("live server did not start")
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def test_status_stream_reconnect_delivers_full_snapshot(live_server, wired, demo_repo):
    """§2.2 测试 4:状态流不做游标,断线重连后第一条就是全量当前状态,不是增量。"""
    import httpx

    base = live_server

    def first_status_event() -> dict:
        with httpx.Client(timeout=10.0) as h:
            with h.stream("GET", f"{base}/api/quests/{qid}/status/stream") as resp:
                assert resp.status_code == 200
                for line in resp.iter_lines():
                    if line.startswith("data:"):
                        return json.loads(line[5:].strip())
        raise AssertionError("status stream closed without pushing")

    with httpx.Client(timeout=30.0) as h:
        h.post(f"{base}/api/projects", json={"path": str(demo_repo)})
        r = h.post(f"{base}/api/quests", json={"project": str(demo_repo), "message": "做一个可验收改动"})
        assert r.status_code == 201, r.text
        qid = r.json()["id"]
        deadline = time.time() + 10
        while time.time() < deadline:
            body = h.get(f"{base}/api/quests/{qid}").json()
            status = body.get("runtime", {}).get("receptionist")
            if status and not status["busy"]:
                break
            time.sleep(0.05)
        r = h.post(f"{base}/api/quests/{qid}/generate")
        assert r.json().get("ok") is True, r.text
        deadline = time.time() + 10
        while time.time() < deadline:
            if h.get(f"{base}/api/quests/{qid}").json()["quest_md"] is not None:
                break
            time.sleep(0.05)
        full = h.get(f"{base}/api/quests/{qid}").json()

    first = first_status_event()
    assert first["state"]["state"] == "drafting"
    assert first["quest_md"] is not None  # 首推带 quest_md
    assert "receptionist" in first["runtime"]

    # 断线重连(新连接、不带任何游标):第一条必须等于当前全量
    second = first_status_event()
    assert second["state"] == full["state"]
    assert second["quest_md"] is not None
    assert "receptionist" in second["runtime"]
    assert second["state"]["history"], "全量快照必须带完整 state.json,不是增量"


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
    assert _generate(client, qid) is not None
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    assert client.get(f"/api/quests/{qid}").json()["state"]["state"] == "in_progress"

    # in_progress 不许改 quest.md(§3 不得清单)
    r = client.put(f"/api/quests/{qid}/quest", content="---\nid: x\n---\n\n## 验收步骤\n\n1. hack\n")
    assert r.status_code == 409

    # 第二单不许同时 in_progress
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "第二单"})
    qid2 = r.json()["id"]
    assert _generate(client, qid2) is not None
    client.post(f"/api/quests/{qid2}/transition", json={"to": "posted"})
    r = client.post(f"/api/quests/{qid2}/transition", json={"to": "in_progress"})
    assert r.status_code == 409

    # 放行第一单
    release.write_text("go\n")
    _wait_state(client, qid, {"appraised", "disputed", "failed"}, timeout=20)
    _wait_state(client, qid2, {"posted"})
