# P24 执行包：`evidence_sources` 声明为图状态通道

- packet_id: `2026-11-27-brain-v4-P24-evidence-sources-state`
- 日期：2026-11-27
- 范围：`brain_v4` 图状态（红区）+ 简报集成测试

## 1. 触发问题（真实运行证据）

2026-11-27 早/午/晚报真实重跑，任务文本本应包含指数点位、涨跌幅、涨跌家数
（服务器探针直接调用 `build_analyst_task` 可见 `close=3476.8816`、
`advance_count=5382` 等），但分析师 claim 全部声称：

- “所有条目均未展开点位、涨跌幅、成交额或涨跌家数明细”
- “案卷未内联各证据的具体数值”
- “仅包含索引元数据”

## 2. 根因

`runtime/briefing_live.py::_initial_state` 与 `runtime/graph_run.py::_initial_state`
都把证据正文写入 `evidence_sources`，但 `ResearchGraphState`（TypedDict）**没有声明
该键**。LangGraph 只保留 schema 中声明的通道，未声明的键在 invoke 时被丢弃，
`dispatch_analysis` / `solo_node` 读到 `None`，`build_analyst_task` 回退到
`tool_params`（source_id/title/tags）→ 分析师只看到元数据。

## 3. 改动

1. `ResearchGraphState` 增加单写通道 `evidence_sources: dict[str, str]`。
2. 新增集成用例 `test_briefing_task_includes_evidence_text`：记录分析师收到的
   HumanMessage，断言其中含 `close=` 与 `advance_count=`。

## 4. allowed_files（红区门禁）

- `src/aistock_agent/brain_v4/graph/builder.py`
- `tests/integration/brain_v4/test_briefing_live.py`

## 5. 验证要求

```bash
uv run pytest tests/unit/brain_v4 tests/integration/brain_v4 -q
uv run ruff check src/aistock_agent/brain_v4 tests
uv run mypy src/aistock_agent/brain_v4
```

服务器真实验证：重跑 11-27 三条简报，claim 必须引用具体数值（点位/涨跌幅/家数），
不得再笼统声称缺失。
