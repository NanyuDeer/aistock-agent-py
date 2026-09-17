from aistock_agent.prompts.workers.light_predict import PREDICTION_LIGHT_PROMPT
from aistock_agent.prompts.workers.prediction import (
    PREDICTION_CHAT_PROMPT,
    PREDICTION_PROMPT,
)


def test_prompt_covers_three_horizons():
    for key in ("short", "mid", "long"):
        assert key in PREDICTION_PROMPT


def test_prompt_forbids_fabrication():
    assert "evidence_ids" in PREDICTION_PROMPT
    assert "禁止" in PREDICTION_PROMPT


def test_prompt_requires_mechanism_on_direction_switch():
    assert "切换" in PREDICTION_PROMPT
    assert "驱动力" in PREDICTION_PROMPT


def test_prompt_requires_structured_evolution_steps():
    # B2 前端时间轴：演化路径须输出结构化步骤（label+text 按档位切分），而非仅一段叙事
    assert "evolution_steps" in PREDICTION_PROMPT
    assert "label" in PREDICTION_PROMPT
    assert "text" in PREDICTION_PROMPT


def test_prompt_instructs_schema_version():
    # B2 回归：PREDICTION_PROMPT 必须指示输出 schema_version（PredictionResult 必填字段），
    # 否则 LLM 输出缺字段 → pydantic 校验失败 → 大盘溯源预测恒丢失（生产实测 2026-08-12）
    # Spec A §3.3：schema_version 升 "3.0"（条件化预判）
    assert "schema_version" in PREDICTION_PROMPT
    assert "3.0" in PREDICTION_PROMPT


def test_prediction_prompts_declare_anchor_event_ref():
    # spec §13.2 / Task 0.3：anchor 键清单须登记 event_ref，LLM 不产则状态锚无法落地。
    # 反之 prompt 多吐该键而 schema 未收时，extra="forbid" 会整条拒绝（parse_failed / None）。
    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "event_ref" in prompt
        assert "仅事件类条件填写" in prompt
    # 轻量预判（LightForecast 复用 PredictionCondition）的 anchor 键清单同批同步
    assert "event_ref" in PREDICTION_LIGHT_PROMPT
    assert "仅事件类条件填写" in PREDICTION_LIGHT_PROMPT


def test_prediction_prompt_horizon_policy_semantics():
    # spec 2026-09-03-动态档位：影响时长分流——short 必产 / 白名单 required+optional /
    # optional 有据才产并写 omitted_horizons / 禁越白名单产档（两处 prompts 同语义）
    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "omitted_horizons" in prompt
        assert "必须产出" in prompt            # short 必产
        assert "required" in prompt and "optional" in prompt
        assert "禁止输出白名单之外的档位" in prompt
        assert "{driver_type}" in prompt        # 白名单由系统注入的运行时占位（Task4 注入）


def test_prediction_prompt_removes_force_three_horizons():
    # 旧"强制三档并列"引导句必须移除（不再默认产出 short/mid/long 三档）；
    # 段落中"不再默认三档"是否定式说明（允许存在），故断言只禁旧引导句片段
    assert "三档补充持续性判断" not in PREDICTION_PROMPT        # 旧任务引导句
    assert "三档分别输出" not in PREDICTION_CHAT_PROMPT         # 旧任务引导句
    assert "为三档持续性判断" not in PREDICTION_PROMPT          # 旧 horizons 引导
    assert "把三档串成" not in PREDICTION_PROMPT                # 旧 evolution_narrative
    assert "把三档串成" not in PREDICTION_CHAT_PROMPT
    assert "若三档方向" not in PREDICTION_PROMPT
    assert "若三档方向" not in PREDICTION_CHAT_PROMPT


def test_prediction_prompt_closing_sentence_required_optional():
    # spec §5.4 final fix（2026-09-03）：收束句去歧义——"某档位无法可靠判断时 confidence
    # 用 low" 未区分档位属性（会诱导 LLM 省略 required 档）；现区分：
    # required 档无法可靠判断 → confidence low（档位仍须产出）；optional 档无证据 → 省略
    # 并写 omitted_horizons（两处 prompts 同语义，防旧句回潮）。
    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "required 档无法可靠判断时 confidence 用" in prompt
        assert "optional 档无证据则省略并写入 omitted_horizons" in prompt
        assert "某档位无法可靠判断时" not in prompt


def test_prediction_prompts_declare_input_event_refs_as_system_filled():
    # spec §4.2 / Task 3.1：input_event_refs 为系统填充的留痕字段（非 LLM 产出）——
    # 顶层键清单须登记，否则模型自发产出非法值会让整条预判 extra="forbid" 校验失败。
    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "input_event_refs" in prompt
        assert "由系统填充" in prompt
        assert "不得产出" in prompt


def test_prediction_prompts_require_event_driven_statement():
    # spec §4.2：预判结论须说明"是否受事件驱动"（输入含事件块时点明依据；无事件禁止编造）
    for prompt in (PREDICTION_PROMPT, PREDICTION_CHAT_PROMPT):
        assert "chain_events" in prompt
        assert "warehouse_events" in prompt
        assert "是否受事件驱动" in prompt
        assert "禁止编造事件" in prompt
