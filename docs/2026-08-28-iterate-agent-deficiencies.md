# 迭代 Agent（iterate）缺陷记录

> 日期：2026-08-28
> 场景：2026-07-16 存储狙击切片完整闭环测试（`run_case.py --agent review`）
> 状态：缺陷待修（本次已实现定向搜索增强缓解，本文件记录框架级缺陷）

## 背景

用 `case_20260716_review_今日沪深核心指数同步下跌_市场广度明显偏弱_投资者情绪整体偏谨慎`（存储狙击日切片）跑完整迭代闭环。切片按平时溯源流程生成（市场数据完整，但事件证据缺失：财联社电报历史为空、事件库无记录、通用搜索 query 搜不到具体事件）。

## 缺陷 1：GT 残缺导致评估假性达标（score=1.0 失真）

**现象**：基线轮评分 **1.0**，`stopped_reason=score_reached`，第 1 轮即终止，未进入任何变体迭代。`best_gap_analysis=无显著差距`。

**根因**：GT 标准答案因切片缺事件语料而残缺：
- `drivers` 为空数组
- `affected_sectors` 取到的是上涨板块（高压氧舱/猪肉/短剧游戏），而非下跌主因板块
- 评估时：方向命中（bearish）+ **空 drivers vs 空 drivers 被算作完全匹配** → 总分虚高

**影响**：
- 闭环在"评估基准无效"时仍会假性达标，浪费一个 case 的迭代额度，且给负责人错误信号（"溯源满分"）
- 现有防护仅覆盖 `confidence=low`（A-3），未覆盖"drivers 空但 confidence=high"的残缺 GT

**修复建议**：
1. GT 生成后校验：`drivers` 为空或 `affected_sectors` 与现象方向矛盾时，将 confidence 降级为 low（复用 A-3 的达标拦截），或拒绝产片
2. evaluator：GT `drivers` 为空时，drivers 分项不得计满分（改为"无参照，跳过该项并把权重重新归一"，而非空匹配）
3. gap_analysis 应反映"GT 语料不足"，而非"无显著差距"

## 缺陷 2：GT 生成对"切片缺事件语料"无感知

**现象**：切片只有市场数据（指数/广度/涨跌停/板块）+ 宏观新闻，无具体事件新闻；GT 仍标 `confidence=high`。

**根因**：`generate_data_constrained_gt` 基于切片语料生成方向/板块/驱动，语料缺事件时驱动只能空，但 confidence 由 LLM 自评，未与语料覆盖度挂钩。

**影响**：残缺 GT 进入评估链，污染迭代闭环与每日汇总报告。

**修复建议**：
- GT 生成时统计语料中事件类证据（`kind=event_evidence` 且非市场事实）的数量，低于阈值（如 3 条）时强制降级 confidence=low 或标记 `data_insufficient=true`
- 每日报告对 `confidence=low` 的 GT 单独标注，避免负责人误读

## 缺陷 3：假性达标后 case 被标记 iterated，无法自动重试

**现象**：本轮闭环 `score_reached` 终止后自动 `mark_iterated(case_id)`；若后续修复 GT/evaluator，该 case 默认不会再次入选 `list_pending_cases`。

**影响**：修复评估逻辑后需手动删除 `.iterated` 标记才能重测，自动化链路上容易遗漏。

**修复建议**：
- `score_reached` 但 GT `confidence=low` 或 drivers 为空时，走 `mark_failed`（退避重试）而非 `mark_iterated`
- 或在 case 记录中写入 `score_reached_with_degraded_gt` 标记，供报告审计

## 相关链接

- 溯源 agent 缺陷：`docs/2026-08-28-review-agent-deficiencies.md`
- 实现计划：`docs/2026-08-28-iterate-agent-deficiencies.md` 待补（缺陷确认后走 writing-plans）
