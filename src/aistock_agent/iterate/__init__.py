"""迭代 Agent 自动闭环（iterate）子包。

对可迭代 agent（review/event_analyst 等）自动跑：历史切片 → 标准答案 →
变体实验 → 归因评估"闭环，输出每日汇总报告。所有逻辑由 adapters.py 的
Agent 适配注册表驱动，不硬编码任何具体 agent。
"""
