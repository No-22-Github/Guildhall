"""状态机(§2.5)。七个状态、封闭转移表。任何表外转移抛 InvalidTransition。

硬约束:
- 进入 posted 必须先校验 quest.md 含非空「## 验收步骤」(在 routes 层执行,状态机只管封闭性)。
- appraised 不自动进 settled——人 review 是流水线的最后一段。
"""

from __future__ import annotations

from typing import Optional

DRAFTING = "drafting"
POSTED = "posted"
IN_PROGRESS = "in_progress"
APPRAISING = "appraising"
APPRAISED = "appraised"
DISPUTED = "disputed"
SETTLED = "settled"
FAILED = "failed"
WITHDRAWN = "withdrawn"

TERMINAL = {SETTLED, FAILED, WITHDRAWN}

# 封闭转移表(§2.5 表格逐条)。表外即异常,不许宽容处理。
TRANSITIONS: dict[str, set[str]] = {
    DRAFTING: {POSTED, WITHDRAWN},
    POSTED: {IN_PROGRESS},
    IN_PROGRESS: {APPRAISING, FAILED},
    APPRAISING: {APPRAISED, DISPUTED},
    APPRAISED: {SETTLED},
    DISPUTED: {SETTLED, WITHDRAWN},
    SETTLED: set(),
    FAILED: set(),
    WITHDRAWN: set(),
}

ALL_STATES = set(TRANSITIONS)


class InvalidTransition(Exception):
    """表外转移。HTTP 层必须回 409。"""

    def __init__(self, from_state: Optional[str], to: str):
        self.from_state = from_state
        self.to = to
        super().__init__(f"illegal transition: {from_state!r} -> {to!r}")


def assert_transition(from_state: Optional[str], to: str) -> None:
    """from_state 为 None(新建)只允许进入 drafting。"""
    if from_state is None:
        if to != DRAFTING:
            raise InvalidTransition(None, to)
        return
    if from_state not in TRANSITIONS:
        raise InvalidTransition(from_state, to)
    if to not in TRANSITIONS[from_state]:
        raise InvalidTransition(from_state, to)
