# Execution Packet · P1-live-tools · 实时取数 NodeToolClient 与工具白名单

> 本文件是 P1 执行包存档，对应计划
> `docs/brain-v4-live-scheduled-run.md` 的「P1 实时取数层」。

## 1. Packet Metadata

~~~yaml
packet_id: P1-live-tools
objective: 让 V4 ToolGateway 通过 NodeApiClient 取真实数据：显式 as_of、工具白名单、未来数据拒绝、缺 published_at 降级、超时分类与同参数 memo 去重
owner: Tools/Data Owner
prerequisite_repo: aistock-agent-py
prerequisite_commit: 9200c8b
feature_id: brain_v4/tools（实时取数层）
expected_commit_message: feat(brain-v4): 实时取数 NodeToolClient 与工具白名单
~~~

> 派发提示写的是 HEAD `21b5d0e` 的本地等价提交；开工时本地 `git log -1`
> 为 `9200c8b`（main，工作区干净），`21b5d0e` 在本地不可解析，故以实际
> HEAD 记录，未做任何 rebase / reset。

## 2. Scope Lock

~~~yaml
allowed_files:
  - src/aistock_agent/brain_v4/tools/node_client.py
  - src/aistock_agent/brain_v4/tools/tool_map.py
  - src/aistock_agent/brain_v4/tools/gateway.py   # 仅必要时最小改动（本包最终未改）
  - tests/unit/brain_v4/test_node_tool_client.py
  - CHANGELOG.md
  - docs/superpowers/packets/2026-11-27-brain-v4-P1-live-tools.md
forbidden_paths:
  - src/aistock_agent/services/data_client.py
  - src/aistock_agent/brain_v4/features/
  - src/aistock_agent/services/scheduler.py
  - aistock-app-api/
  - aistock-app-frontend/
  - aistock-frontend/
  - pyproject.toml
~~~

实际改动为上述白名单的四个仓库内文件；`gateway.py` 未改，`data_client.py`、
features、scheduler、app-api、前端与 pyproject 均未触碰；未新增依赖、未发网络请求。

## 3. Contracts

### Consumes

- `aistock_agent.brain_v4.tools.gateway.ToolGateway / ToolClient / ToolClientResult`
  （显式 `as_of`、future_data 校验、超时与 memo）。
- `aistock_agent.brain_v4.tools.provenance.QualityLevel / ToolEnvelope`（只读）。
- `aistock_agent.services.data_client.NodeApiClient` 的只读方法：
  `get_quote / get_stock_kline / get_index_kline / get_ths_index_map /
  get_intraday_sectors / get_last_close_snapshot / get_close_snapshot /
  get_calendar_events / get_review_analysis_report`（全部 async）。

### Produces

- `tools.tool_map.TOOL_MAP / ToolSpec / resolve_tool / validate_params /
  UnknownToolError / InvalidToolParamsError`。
- `tools.node_client.NodeToolClient.call / fetch` 与 `NodeToolPayload`。

## 4. 关键不变量

- 未登记工具与未登记参数在触达客户端之前被拒绝；路径参数再做字符集校验。
- `as_of` 只来自网关；NodeToolClient 不读取系统时间，只解析 payload 自带的
  `published_at` / `publish_time` / `created_at`。
- 发布时间缺失或非法 → `time_confidence="low"`、`quality="low"`，绝不默认 high；
  `published_at > as_of` 由既有网关返回 `future_data` 且 `data is None`。
- `legacy_review` 的 `source_kind` 恒为 `legacy_output`，上层不得直接当 evidence。
- 超时沿用网关 `asyncio.wait_for` 分类；同参数并发调用只触达 Node 一次（memo）。
- 默认 `NodeApiClient` 延迟到首次取数才构造，导入与对象创建不读环境。

## 5. 验证结果（真实运行）

- 红态（实现前）：`uv run pytest tests/unit/brain_v4/test_node_tool_client.py -q`
  → 收集失败 `ModuleNotFoundError: No module named 'aistock_agent.brain_v4.tools.node_client'`。
- 绿态（focused）：同命令 → `17 passed`。
- V4 单测：`uv run pytest tests/unit/brain_v4 -q` → `159 passed`。
- `uv run ruff check src/aistock_agent/brain_v4 tests/unit/brain_v4` → `All checks passed!`。
- `uv run mypy src/aistock_agent/brain_v4` → `Success: no issues found in 61 source files`。

## 6. 关键断言测试名

- `test_unknown_tool_is_rejected_without_calling_client`
- `test_direct_unknown_tool_raises_before_touching_client`
- `test_published_at_after_as_of_is_rejected_as_future_data`
- `test_missing_published_at_degrades_quality_to_low`
- `test_published_at_is_extracted_from_supported_payload_keys`
- `test_client_timeout_is_classified`
- `test_memo_deduplicates_concurrent_identical_calls`
- `test_success_envelope_carries_provenance_fields`
- `test_legacy_review_is_marked_as_legacy_output`
- `test_unknown_param_is_rejected` / `test_missing_required_param_is_rejected`
- `test_stock_and_index_kline_translate_param_names`
- `test_market_snapshot_uses_date_or_last_close`
- `test_calendar_events_expands_date_to_range`
- `test_no_param_tools_reject_extra_params`

## 7. 说明

- `market_snapshot` 的 `slim` 参数当前只入白名单与载荷审计：Node 侧暂无
  slim 端点，不在 P1 伪造语义；有 date 走 `get_close_snapshot`，无 date 走
  `get_last_close_snapshot`。
- 测试全部使用假 `NodeApiClient`，离线、确定性、不读系统时间、不触网。
- 本包不写生产简报表、不改 V4 拓扑、不切 active owner。
