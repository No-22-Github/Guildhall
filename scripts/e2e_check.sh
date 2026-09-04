#!/bin/zsh
# M5 验收脚本(规格书 §7):
#   1) 全部单元/管线测试(含 M1 负向:M1 状态机、M5 两条闸门负向,以假 ACP 服务器确定性覆盖)
#   2) 真模型 M5 负向一:手工构造偷改测试的 diff,真 appraiser 必须报 touched_tests=true
# 前置:sandbox-agent server 运行中(或由 guildhall-server 自动拉起)
set -e
cd "$(dirname "$0")/../backend"

echo "== 1. pytest(确定性部分)"
uv run pytest tests/ -q

echo
echo "== 2. 真模型 M5 负向(appraiser 抓作弊)"
uv run python scripts/m5_negative.py
