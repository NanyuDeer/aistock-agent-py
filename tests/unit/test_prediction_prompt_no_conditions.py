"""Task 6 生成侧退役：两个 prediction prompt 不再要求产出 conditions。

背景：条件化预判要求「条件先成立、预判才生效」，但条件需要时间才跑得出来，
导致大量样本永远停在 pending，且稀释「方向判断本身准不准」的验证信号。
决策：只给方向，条件分支退出验证环。本文件锁定**生成侧**契约
（验证侧下线为 Task 7；`PredictionResult.conditions` 字段保留，仅兼容旧记录）。
"""


def test_prediction_prompts_do_not_request_conditions():
    from aistock_agent.prompts.workers.prediction import (
        PREDICTION_CHAT_PROMPT,
        PREDICTION_PROMPT,
    )

    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "条件化预判核心" not in prompt
        # 注：brief Step 1 原断言 `assert "conditions：" not in prompt` 与 Step 3 要求补写的
        # 兼容说明行 `- conditions：**不再产出**…` 直接冲突（后者含 "conditions："）。
        # 故按语义收窄：锁定「旧的必须产出要求已删 + 兼容说明已补」。
        assert "必须非空" not in prompt  # 旧「必须非空，2-3 条」要求段已删
        assert "不再产出" in prompt       # 输出契约补兼容说明（字段保留但不产出）


def test_rhythm_master_prompt_untouched():
    """护栏：退役范围限预测 prompt，节奏 prompt 不得被牵连。"""
    from pathlib import Path

    text = Path("src/aistock_agent/prompts/workers/rhythm_master.py").read_text(encoding="utf-8")
    assert "分支点位由 engine 确定性计算" in text
