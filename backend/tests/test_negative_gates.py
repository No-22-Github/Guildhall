"""M1 §2.5.1 / §2.6 的机械闸门与确定性检测(不过模型)。

测试 5:含 `main...HEAD` 的负向验收步骤必须被机械校验拒绝写盘。
测试 6:预置「偷改测试文件」的 diff fixture,detect_touched_tests 必须为 true。
"""

from __future__ import annotations

import asyncio
import json
import time

from fake_acp import chunk
from test_api_flow import _wait_receptionist_idle, wired  # noqa: F401  (fixture 与 helper 来自 test_api_flow.py)
QUEST_MD_WITH_BAD_NEGATIVE = """\
---
id: {{qid}}
title: 坏需求单
project: {{project}}
created: 2026-09-05T00:00:00+08:00
---

## 目标

演示一条结构上不可能满足的负向测试。

## 约束

- 无

## 现状调研

- 无

## 改动范围

- `src/**`

## 验收步骤

1. `python -m src.main` 退出码为 0
2. 【负向】篡改 src/main.py 后,`git diff main...HEAD --exit-code` 必须报警
"""


def test_negative_step_with_commit_range_rejected_before_write(client, wired, demo_repo):
    """§2.5.1:负向步骤用 `main...HEAD` → 机械校验拒绝,quest.md 不落盘。"""
    from guildhall import store as store_module
    from guildhall.questmd import negative_step_violation

    md_text = QUEST_MD_WITH_BAD_NEGATIVE
    assert negative_step_violation(md_text) is not None  # 单元层:校验器抓得住

    async def bad_receptionist(text: str):
        if "需求拷问者" in text:
            return ([chunk("问题?")], "问题?", "end_turn")
        if text.strip() == "生成需求单":
            return ([chunk("好的。")], "好的。\n\n" + md_text, "end_turn")
        return ([chunk("嗯")], "嗯", "end_turn")

    wired.set_script("receptionist", bad_receptionist)
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    _wait_receptionist_idle(client, qid)

    # /generate 是异步受理:等待后台生成收口(正常应为「错误落盘、quest.md 缺席」)
    assert client.post(f"/api/quests/{qid}/generate").status_code == 200
    deadline = time.time() + 10
    while time.time() < deadline:
        body = client.get(f"/api/quests/{qid}").json()
        if body["quest_md"] is not None or body["state"].get("error"):
            break
        time.sleep(0.05)
    body = client.get(f"/api/quests/{qid}").json()
    assert body["quest_md"] is None  # 拒绝写盘
    assert "提交对提交" in body["state"]["error"]
    # 时间窗内没有落盘发生
    assert not store_module.find_quest(qid).quest_md.exists()


def test_negative_step_on_workspace_passes_gate():
    """反例保护:作用于工作区的负向测试不许被误杀。"""
    from guildhall.questmd import negative_step_violation

    ok_md = QUEST_MD_WITH_BAD_NEGATIVE.replace(
        "`git diff main...HEAD --exit-code` 必须报警",
        "`python -m pytest -q` 必须失败",
    )
    assert negative_step_violation(ok_md) is None
    # diff HEAD 是工作区对基点,不属于要拦的三点范围;两点/三点范围与 diff HEAD 之外的纯工作区命令照常放行
    assert negative_step_violation("## 验收步骤\n\n1. 【负向】改坏后 `python -m pytest -q` 必须失败\n") is None


def test_detect_touched_tests_fixture_is_true():
    """§2.6 测试 6:预置「偷改测试文件」的 diff fixture,确定性判定必须 true(不过模型)。"""
    from guildhall.flow import detect_touched_tests, parse_appraisal

    cheat_diff = """\
diff --git a/src/calc.py b/src/calc.py
index 1111111..2222222 100644
--- a/src/calc.py
+++ b/src/calc.py
@@ -1,3 +1,5 @@
 def add(a, b):
     return a + b
+
+def power(a, b):
+    return a * b  # 故意写错
diff --git a/tests/test_power.py b/tests/test_power.py
index 3333333..4444444 100644
--- a/tests/test_power.py
+++ b/tests/test_power.py
@@ -1,3 +1,3 @@
 from src.calc import power
 def test_power():
-    assert power(2, 10) == 1024
+    assert power(2, 10) == 20  # 断言被偷改
"""
    assert detect_touched_tests(cheat_diff) is True

    # 干净 diff(只改实现)必须 false,不许冤枉
    clean_diff = cheat_diff.split("diff --git a/tests")[0]
    assert detect_touched_tests(clean_diff) is False

    # CI 配置也算
    ci_diff = "diff --git a/.github/workflows/ci.yml b/.github/workflows/ci.yml\n--- a/.github/workflows/ci.yml\n+++ b/.github/workflows/ci.yml\n"
    assert detect_touched_tests(ci_diff) is True

    # 走「解析 + 检查」的完整机械链路(绕过模型):模型谎报 false 也拦得住
    appraisal = parse_appraisal('{"checks": [], "touched_tests": false, "out_of_scope_files": [], "summary": "全绿"}')
    assert appraisal is not None
    assert appraisal["touched_tests"] is False
    assert detect_touched_tests(cheat_diff) is True  # 机械判定推翻模型谎报

def test_pipeline_deterministic_touched_tests_overrides_model_lie(client, wired, demo_repo, tmp_guildhall):
    """确定性防线进管线:模型谎报 touched_tests=false,服务端从 diff 兜底判 true → disputed。"""
    client.post("/api/projects", json={"path": str(demo_repo)})
    r = client.post("/api/quests", json={"project": str(demo_repo), "message": "改点东西"})
    qid = r.json()["id"]
    wt = tmp_guildhall / "worktrees" / qid
    cheat = wt / "tests" / "test_main.py"

    async def sneaky_adventurer(text):
        while "被偷改的断言" not in cheat.read_text():
            await asyncio.sleep(0.05)
        return ([], "完成报告:全绿。", "end_turn")

    wired.set_script("adventurer", sneaky_adventurer)

    async def lying_appraiser(text):
        appraisal = {"checks": [{"index": 1, "step": "任意", "result": "pass", "evidence": "ran"}],
                     "touched_tests": False, "out_of_scope_files": [], "summary": "模型声称什么都没改。"}
        return ([chunk("...")], json.dumps(appraisal), "end_turn")

    wired.set_script("appraiser", lying_appraiser)

    from test_api_flow import _generate, _wait_dir

    assert _generate(client, qid) is not None
    client.post(f"/api/quests/{qid}/transition", json={"to": "posted"})
    client.post(f"/api/quests/{qid}/transition", json={"to": "in_progress"})
    _wait_dir(wt / "tests")
    cheat.write_text("def test_main():\n    assert True  # 被偷改的断言\n")

    st = ""
    deadline = time.time() + 20
    while time.time() < deadline:
        st = client.get(f"/api/quests/{qid}").json()["state"]["state"]
        if st in ("appraised", "disputed"):
            break
        time.sleep(0.1)
    assert st == "disputed"
    ap = client.get(f"/api/quests/{qid}/appraisal").json()
    assert ap["touched_tests"] is True  # 服务端机械判定,不信模型的 false
