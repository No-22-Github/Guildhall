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
