#!/usr/bin/env bash
# 迭代 Agent 沙盒初始化（服务器一次性执行）
# 用法：bash scripts/setup_iterate_sandbox.sh <repo_root>
set -euo pipefail

REPO_ROOT="${1:?usage: setup_iterate_sandbox.sh <repo_root>}"
SANDBOX="/home/aistock/iterate-sandbox"
BRANCH="experiment-iterate"

cd "$REPO_ROOT"
if [ ! -d "$SANDBOX/.git" ]; then
  git worktree add "$SANDBOX" -b "$BRANCH"
fi

cd "$SANDBOX"
cp "$REPO_ROOT/.env.example" .env 2>/dev/null || true
echo "--- 请在 .env 中配置: ITERATE_ENABLED=true 与 ITERATE_SMTP_* ---"
echo "--- 数据目录已在 .gitignore 中（data/cases|ground_truths|experiments|reports）---"
echo "--- 手动跑一次闭环: python -m aistock_agent.iterate.run_case review <case_id> ---"
