# 趋势股评分报告持久化与 PC 展示设计

## 目标

管理员手动生成趋势股评分报告后，报告必须写入 `agent_analysis_reports`，并能在 PC Web 的趋势股评分页中打开、读取和展示。定时任务已有的报告链路必须保持不变。

## 根因

`POST /api/agent/briefing/trend-score/trigger` 构造的 Agent State 使用 `trigger_source = "manual"`。而 `trend_score.run()` 仅在 `trigger_source == "scheduler"` 时调用 `node_api.save_analysis_report()`；因此接口返回 `has_response: true` 只代表 LLM 生成成功，不代表报告已持久化。前端通过 `GET /api/agent/report/trend_score/:date` 查询数据库，因而没有可展示的数据。

PC Web 前端目前也没有此公开报告接口的 API 封装、报告页面、路由或入口。App 前端已有同一报告格式的展示页，可作为数据契约和内容分区的参考。

## 方案选择

### 方案 A（采用）：统一持久化 + 独立报告页

1. Python Agent 将持久化条件扩展为 `scheduler` 和 `manual` 两种受控来源。
2. PC 前端新增只读 `agentReportApi.getReport(intent, date)`，调用已有公开接口。
3. 新增 `/trend/report` 独立页面，按 App 端已采用的双层报告格式展示内容。
4. `TrendScoreView` 的“导出报告（开发中）”替换为“查看 AI 分析报告”，跳转到该页面。

优点：修复真实数据断点，用户可以从评分页明确进入完整报告；不改变评分页的复杂图表逻辑，也不会将 Agent 展示逻辑耦合进单票评分视图。

### 方案 B：仅修复持久化

报告会被写入并可供 App 前端读取，但 PC Web 仍无入口和页面，不能满足当前前端展示需求。

### 方案 C：把报告嵌入 `TrendScoreView`

减少一个路由，但会让负责趋势数据、图表和股票选择的现有大组件同时承担长报告加载与渲染，难以维护，也不符合 App 端已有的独立报告体验。

## 数据流

```text
管理员 curl 触发
  -> trigger_source = manual
  -> trend_score.run()
  -> parse_dual_layer_response()
  -> POST /internal/analysis-reports (report_type=trend_score)
  -> agent_analysis_reports
  -> GET /api/agent/report/trend_score/:date
  -> PC agentReportApi
  -> /trend/report 页面
```

当请求日期不存在时，现有 Node.js 公开接口会返回最近一份报告。PC 页面必须显示响应中的实际 `report_date`，避免把旧报告误标记为当天报告。

## PC 页面行为

- 页面默认按上海日期查询；从评分页传入日期时优先使用该日期。
- 读取 `content.display_report`，显示：结论摘要、维度解读、趋势判断、赛道分析、风险提示、关注建议。
- 报告正文沿用双层协议：`summary`、`details`、`risks`；对旧报告或非结构化内容做安全降级，不因缺少某个分区而报错。
- 加载失败或无报告时展示明确空状态，不伪造 Mock 报告。
- 不提供生成按钮或内部 token；生成仍仅由受控的后台接口完成。

## 测试与验证

1. Python 回归测试：手动触发产生有效回答时，断言 `save_analysis_report()` 用 `trend_score`、传入日期和双层内容调用一次；保留定时来源覆盖。
2. 前端测试：验证 API URL 及报告内容适配／分区提取；若现有 PC 项目无组件测试运行器，则将纯适配逻辑抽为可单测函数，页面通过构建验证。
3. 运行 Python 定向测试、ruff，以及 PC `npm run build`（必要时加 lint）。
4. 部署后重新执行管理员 curl，再请求 `GET /api/agent/report/trend_score/2026-07-31`，最后访问 `/trend/report?date=2026-07-31` 验收。

## 非目标

- 不修改趋势股量化评分算法、Top 列表或单票详情。
- 不改变 Node.js 公开报告接口和数据库表结构。
- 不新增前端直连 Agent Python 服务的请求。
