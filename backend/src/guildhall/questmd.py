"""quest.md 读写与校验(§2.3)。

quest.md 是人可以直接手改的正文,程序状态一律不进这里(状态在 state.json)。
frontmatter 只含 id / title / project / created 四行,手写解析,不引 yaml 依赖。
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional

REQUIRED_SECTION = "## 验收步骤"


@dataclass
class QuestDoc:
    frontmatter: dict[str, str]
    body: str  # frontmatter 之后的正文(含 ## 目标 等)

    def render(self) -> str:
        lines = ["---"]
        for k, v in self.frontmatter.items():
            lines.append(f"{k}: {v}")
        lines.append("---")
        return "\n".join(lines) + "\n\n" + self.body.strip() + "\n"


def parse(text: str) -> QuestDoc:
    """解析 quest.md。frontmatter 缺失时返回空 dict + 全文 body。"""
    if text.startswith("---"):
        m = re.match(r"^---\n(.*?)\n---\n?(.*)$", text, re.DOTALL)
        if m:
            fm = {}
            for line in m.group(1).splitlines():
                if ":" in line:
                    k, _, v = line.partition(":")
                    fm[k.strip()] = v.strip()
            return QuestDoc(frontmatter=fm, body=m.group(2).strip())
    return QuestDoc(frontmatter={}, body=text.strip())


def acceptance_steps(body: str) -> list[str]:
    """取出「## 验收步骤」一节里的逐条条目(编号行)。"""
    m = re.search(r"^## 验收步骤\s*$(.*?)(?=^## |\Z)", body, re.MULTILINE | re.DOTALL)
    if not m:
        return []
    steps = []
    for line in m.group(1).splitlines():
        line = line.strip()
        if re.match(r"^\d+\.", line):
            steps.append(line)
    return steps


def has_nonempty_acceptance(text: str) -> bool:
    """§2.5 闸门:quest.md 必须含非空「## 验收步骤」。

    非空 = 该节存在且至少有一条编号条目。写「代码质量良好」这类不可判定的条目
    靠 prompt 约束,这里只挡空单。
    """
    return len(acceptance_steps(text)) > 0


def acceptance_gate_error(text: str) -> Optional[str]:
    if not has_nonempty_acceptance(text):
        return "quest.md 缺少可执行的「## 验收步骤」(至少一条编号条目):这是整条流水线唯一防止空单的闸门,放过去了后面全是垃圾。补上验收步骤再张贴。"
    return None


# §2.5.1:负向测试的机械校验(纯字符串检查,不过模型)。
# 提交对提交的比较观察不到工作区改动——负向测试必须作用于工作区当前状态。
_DIFF_HEAD_RE = re.compile(r"\bdiff\s+HEAD(?:\s|$)")
_DIFF_RANGE_RE = re.compile(r"\bdiff\s+\S+\.\.")


def negative_step_violation(text: str) -> Optional[str]:
    """「验收步骤」中标记为负向的条目若用了提交对提交比较,返回拒绝原因;None 表示可过。"""
    for step in acceptance_steps(text):
        if "负向" not in step:
            continue
        for cmd in re.findall(r"`([^`]+)`", step):
            if "..." in cmd or _DIFF_RANGE_RE.search(cmd) or _DIFF_HEAD_RE.search(cmd):
                return (
                    f"负向验收步骤使用了提交对提交的比较(`{cmd}`),它观察不到工作区改动,"
                    "这样的负向测试在结构上永远不会报警,拒绝写盘。"
                    "负向测试的命令必须作用于工作区当前状态,请重写这一条。"
                )
    return None
