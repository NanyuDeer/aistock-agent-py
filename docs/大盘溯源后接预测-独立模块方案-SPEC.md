# 大盘溯源后接预测 · 独立模块方案（SPEC）

> 状态：设计辩论收敛，用户验收通过（2026-08-14）
> 覆盖仓库：`aistock-agent-py`（主）+ `aistock-app-api` + `aistock-app-frontend`
> 产出方式：design-debate 三轮回合（正/反 subagent 交锋 + 主 Agent 裁决），含四件套（决策表 / 硬约束 / 分歧记录 / 修订的事实）

## 1. 背景与问题

大盘溯源（review agent）产出因果链后，现状预测（影响持续性推演）**内联**在 review 流程中：

- `agents/workers/review.py` 两个入口（`run()` L1121-1140、`run_review()` L1353-1427）内联调用 `prediction_service.run_predict`，结果嵌入 `ReviewArtifact.prediction`；
- 预测记录 `_persist_prediction_record`（review.py L907-934）写 `prediction_records` 表（`source_id=review:{date}`）；
- 落库写点实际有 **4 个**（run 内联、run_review quick、run_review full、事件入口），全部写同一 `source_id`；LLM 输出非确定 → 互相覆盖且 `verification` 不重置 → 验证数据与展示数据错位；
- 大盘溯源页预判卡片**生产环境当前即空态**（现存 bug，见 §6）：Python 写 `content.market_trace.prediction`，前端读 `trace.prediction`，而 `MarketTraceResult` schema 无 prediction 字段（`extra="forbid"`）。

目标：把预测拆为**独立能力模块**（独立触发、独立执行、独立验证闭环），消除多写点错位，并修复空态 bug。

## 2. 目标与非目标

**目标**
- 预测从 review 内联拆出：独立 `review_done` 事件触发 + 独立消费者组 + 独立执行入口 `predict_from_trace`；
- 单一真值源：`prediction_records`，review 不再产预测；
- 大盘溯源页预判卡片统一读 `prediction_records`；
- 顺带修复 G14 空态 bug；G6/G7 日期缺陷并行修（独立 PR，不 gate 拆分）。

**非目标**
- 不改造 `PredictionResult` 契约与提示词逻辑；
- 不引入新预测模型 / 不改变"影响持续性推演非点位预测"产品红线（B5）；
- 个股溯源 / 事件传导接入预测**不在本期**（但本方案为其留同一入口 `predict_from_trace`）。

## 3. 方案决策表（四件套 · 决策表）

| 决策点 | 采用 | 裁决理由 |
|---|---|---|
| 预测从 review 内联拆出 | ✅ 采用 | 4 写点同 `source_id` + LLM 非确定 → 覆盖错位；拆后单一真值 + 独立闭环 |
| 触发机制 | ✅ 独立 `review_done` 事件 + 独立消费者组 `prediction_chain` | review 已是事件驱动（scheduler 15:30/20:30 发布入站事件）；固定 cron 是架构倒退；`snapshot` 事件语义不符（快照构建非 review 完成） |
| quick/full 谁产预测 | ✅ 仅 full | full 用 20:30 Tushare 完整快照，质量更高；单写者连根拔掉竞态；`source_id` 单值、publicRouter 正则零改动 |
| `run_predict` 返回契约 | ✅ 状态化 | 现状 `None` 无法区分门禁跳过 / LLM 失败 / 解析失败，retry 无落点 |
| 大盘溯源页预判卡片数据源 | ✅ 统一读 `prediction_records` | 修复 G14 现存空态；R9 收敛 |
| G6/G7 节假日修复 | ✅ 独立 PR 并行，不 gate 拆分 | chinese_calendar 只覆盖 2004-2026，long 档已越年；拆分不依赖它，但须显式 `due_dates_failed` |
| 按需 API | ✅ Node 代理 + 鉴权 + 限流 + 仅当日 + 已验证拒覆盖 | Python 触发端点全 internal-token，前端无法直调 |

## 4. 硬约束清单（实施必须遵守）

1. **review_done 双路径覆盖**：预测自动链路必须覆盖事件驱动 + 旧串行两条 full review 路径；生产必须 `QUICK_SNAPSHOT_ENABLED=true`（否则默认配置下自动预测断链，只剩手动兜底）。
2. **同 PR 原子落地**：review.py 内联预测删除与 `run_predict` 契约化必须同一 PR（中间态访问 `run_result.prediction`/`due_dates` 类型即坏）。
3. **落库闭环**：Node `internalRouter POST /` 必须支持 `status` 写入，闭环 `skipped` 落库。
4. **B2.1 口径隔离**：`computeStats` 显式跳过 `status='skipped'`（否则误计 pendingCount）；存量 quick 版记录混入验收时明示接受或一次性标记。
5. **review_done 发布幂等**：event_id 确定性（`review_done_{date}_{trace_id}`），防 at-least-once 重发重复触发 LLM。
6. **review 降级不发 review_done**：`ReviewFullConsumer` 检查 `result.status=="ok"` 才发布。
7. **"永不 500"升级为"不静默缺失"**：无源可读时落 `skipped` 记录 + 告警 + 健康检查项。

## 5. 分歧记录

- **15:30-20:30 空窗期**：正方（空窗可接受 + "预判生成中"文案）vs 反方（原 15:30 有预测，属行为回退）。**裁决：接受空窗**——生产现状预判卡片本来就是空的（G14），full-only 反而统一了质量与展示口径。**记录不裁决项**：若产品要求 15:30 必有预判，需另开"quick 预判 v1 + full 覆盖"增强，留产品拍板。
- **G6 是否拆分前置**：正方 v2 曾设"先修再拆"，反方驳"外部数据发布当门槛不合理"。**裁决：并行 PR**，拆分验收改为"越年显式 `due_dates_failed` + 告警"。

## 6. 修订的事实（辩论中发现）

- **G14（现存 bug，已实证）**：Python 写 `content.market_trace.prediction`（review.py L870-874），前端读 `trace.prediction`（`marketTraceReview.ts` L467），`MarketTraceResult` schema 无 prediction 字段 → 大盘溯源页预判卡片**生产当前即空态**。→ "统一读 records"既是收敛也是修复。
- **"16:00 验证 vs 20:30 full"不冲突**（反方自我修正）：due 偏移 5 交易日起，D 日 16:00 验证的是 D-1/D-5 旧记录，不碰当天记录；冲突仅限补偿重跑边缘场景。
- **EventBus 单消费者组**：`consumer_group="evening_chain"` 固定（event_bus.py L36），`XREADGROUP` 同组消息互斥分发 → 新增预测消费者必须开新组，否则与 Snapshot/Iterate 消费者抢消息。
- **G6（现存缺陷）**：`chinese_calendar` 仅覆盖 2004-2026（date.py L19-31），long 档 120 交易日从 2026-08 起算已越 2027，越年 fallback 只跳周末 → 到期日精度损失（偏短几天），当前即生效。
- **G7（现存缺陷）**：`_compute_due_dates` 失败降级 `{}`（prediction_service.py L215-220）→ 验证扫描空 dict 无档位可验 → 记录永久 pending，B2.1 样本缺失。
- **`quick_snapshot_enabled` 默认 False**（config.py L207），main.py L57 仅其为 True 时启动事件消费者 → 默认配置无事件链路。

## 7. 目标架构

```
15:30 review_quick ──► ReviewQuickConsumer（不发 review_done，仅快照链路）
20:30 review_full ──► ReviewFullConsumer ── status=="ok" ──► review_done{date,trace_id}
                       （幂等 event_id=review_done_{date}_{trace_id}）
                                  │
                                  ▼  （消费组 prediction_chain，与 evening_chain 隔离）
                           PredictionConsumer
                             └─► predict_from_trace(trace_id, trade_date)
                                   ├─ 读取链：缓存 key 直读 → DB content.market_trace 重建 → 失败 skipped+告警
                                   ├─ 强制 artifact.snapshot.trade_date == trade_date（对照 review.py L983 先例）
                                   ├─ run_predict（状态化契约；retry-once 仅 llm_failed/parse_failed）
                                   └─ 落 prediction_records（source_id=review:{date} 单写者）
                                         └─ 16:00 prediction_validate 扫描（现有，零改动）
                                               └─ B2.1 命中率页（统一读 records）
```

## 8. 结构决策 S1-S7（实施必须遵守）

### S1 触发机制（结构决策）
- 新增独立事件 channel `review_done` + 独立消费者组 `prediction_chain`。
- `review_done` **仅由 `ReviewFullConsumer` 在 `run_review` 返回 `status=="ok"` 时发布**（G13 修复：降级不发）；quick 永不发布。
- `EventBus` 多组支持 = 参数化 5 处（`consume` / `_ensure_group` / `ack` / `retry` / `Event.group`），默认组 `evening_chain` 不变，现有 5 消费者零改动。
- 新增 `PredictionConsumer`，经 `start_all_consumers` 注册（main.py lifespan 自动接管启停）。

### S2 `run_predict` 返回契约（结构决策）
```python
@dataclass(frozen=True)
class PredictionRunResult:
    status: Literal["ok", "gate_skipped", "llm_failed", "parse_failed", "due_dates_failed"]
    prediction: PredictionResult | None = None
    due_dates: dict[str, str] = field(default_factory=dict)
    reason: str = ""
```
- `gate_skipped`：attribution 门禁未过；`llm_failed`/`parse_failed`：LLM 侧瞬时失败，**可重试**；`due_dates_failed`：日期计算失败，**不重试**但显式告警（承接 G6/G7）。
- retry-once（指数退避 2s）落在 `PredictionConsumer.handle`，事件级失败再走 EventBus retry → DLQ。
- 现有 2 个调用点（review.py）随 R1 退役删除，新调用点唯一 = `PredictionConsumer`。

### S3 单写者（结构决策）
- 预测**仅 full 完成后执行**，quick 退出预测路径（消灭 quick/full 竞态）。
- `source_id` 保持 `review:{date}` 单值；publicRouter `resolveReportDate` 正则与 B2.1 口径零改动。
- 大盘溯源页 15:30-20:30 显示"预判生成中（今日 20:30 后可见）"。

### S4 展示统一（结构决策）
- 大盘溯源页预判卡片改读 `GET /api/predictions?source_id=review:{date}`。
- Node `PredictionRecordService.list()` 增加可选 `source_id` 过滤；`publicRouter GET /` 支持 `source_id` query（与 `status` 组合，格式校验 `^review:\d{4}-\d{2}-\d{2}$`）。
- 前端 `toMarketTracePresentation` 增加第三参数 `predictionRecord`；`traceability.vue` 并行拉 report + prediction 列表。
- `market_trace.prediction` 退役（review.py L870-874 停写）；测试 fixture 更新（删除"从 market_trace.prediction 读预测"的错误用例，改为传/不传 predictionRecord 两组）。

### S5 日期修复并行（结构决策）
- G6/G7 修复独立 PR（PR-B），与拆分（PR-A）互不 gate。
- `date.py` 增加可注入节假日补充数据源（如 `settings.holidays_extra: list[str]`），`is_trading_day` 优先查补充表再查 chinese_calendar；2027 数据发布 / 库升级后自动恢复精确。
- PR-A 验收门槛：越年时返回显式 `due_dates_failed` + 告警，不承诺 2027 精确节假日；`add_trading_days` fallback 语义与拆分前一致。

### S6 按需 / 补偿 API（结构决策）
- Python 新增 `POST /api/internal/predictions/from-trace`：同步请求-响应，`verify_internal_token` 鉴权，body `{trace_id, trade_date}`，返回状态对象 + record。
- Node 新增 `POST /internal/predictions/regenerate` 代理：仅限当日交易日（`trade_date === 上海今日`）+ 已验证拒覆盖 409 + Redis 限流（每 date 每小时 ≤3 次）+ HTTP client 90s 超时；复用既有 `AGENT_PY_URL` 配置（勿新建 `AGENT_PY_BASE_URL`）。
- 与 `review_done` 自动路径共用 `predict_from_trace` 核心；Python 端点内加同款"已验证拒覆盖"防御。

### S7 status 域扩展（结构决策）
- 新增 `skipped`（语义：门禁未过 / trace 不可重建，无法生成预测）。
- `listPending` 零改动（`WHERE status='pending'` 天然排除 skipped，验证扫描不碰）。
- `list/listAllForStats` 参数与 `publicRouter VALID_STATUSES` 加 `skipped`；`computeStats` 单独 `skippedCount`，**不计入** pending/verified。
- 落库：`internalRouter POST /` 支持可选 `status` 与 `skip_reason`（skip_reason 存 prediction 对象内，免 DB 迁移；`status` 列已存在，无 CHECK 约束）。
- 前端 `PredictionRecordStatus` 类型 + `prediction-detail.vue` badge 加 skipped 分支；`prediction-history.vue` 不加 skipped tab（只在"全部"可见）。

## 9. 仓库改动点清单

### aistock-agent-py
| 文件 | 改动 |
|---|---|
| `agents/workers/review.py` | 删 4 处内联预测（L1121-1126 / L1130-1131 / L1354-1363 / L1371）+ 移除 `_persist_prediction_record`（L907-934）与其调用（L1164 / L1420-1427）；`_build_review_report` 停写 `market_trace.prediction`（L870-874） |
| `services/event_bus.py` | `consume`/`_ensure_group`/`ack`/`retry` 参数化 group + `Event.group` 字段（默认组不变） |
| `services/event_consumers.py` | 新增 `CHANNEL_REVIEW_DONE`；`ReviewFullConsumer` 检查 status 后发布 review_done（幂等 event_id）；`ReviewQuickConsumer` 不发；新增 `PredictionConsumer`；`start_all_consumers` 注册 |
| `services/prediction_service.py` | `PredictionRunResult` 状态化；`run_predict` 返回状态对象（保留 gate 判定）；新增 `predict_from_trace(trace_id, trade_date)`（缓存直读 → DB 重建 → trade_date 校验 → run_predict → 落库） |
| `api/routes.py` | 新增 `POST /api/internal/predictions/from-trace`（verify_internal_token） |
| `services/data_client.py` | save_prediction 支持 status/skip_reason |
| `utils/date.py`（PR-B） | 可注入节假日补充数据源 |
| `config.py` | 部署约束：生产 `QUICK_SNAPSHOT_ENABLED=true`（文档/部署清单） |

### aistock-app-api
| 文件 | 改动 |
|---|---|
| `modules/prediction/PredictionRecordService.ts` | `create` 支持 status 列；`list` 加 `source_id` 过滤；`listAllForStats` 参数扩展 |
| `core/routes/publicRouter.ts` | `VALID_STATUSES` 加 skipped；`source_id` query；`computeStats` skippedCount 隔离 |
| `core/routes/internalRouter.ts` | `POST /` 支持 status；新增 `POST /internal/predictions/regenerate` 代理（仅当日 + 已验证 409 + 限流 + 90s 超时 + AGENT_PY_URL） |

### aistock-app-frontend
| 文件 | 改动 |
|---|---|
| `modules/analytics/pages/traceability.vue` | 预判卡片改读 `predictionApi.list({source_id})` |
| `modules/analytics/utils/marketTraceReview.ts` | `toMarketTracePresentation` 加 `predictionRecord` 参数 + 类型窄化；L467 改读 records |
| `modules/analytics/api/prediction.ts` | `list` 加 `source_id?`；`PredictionRecordStatus` 加 skipped |
| `modules/analytics/pages/prediction-detail.vue` | badge skipped 分支 |
| 测试 | marketTraceReview fixture 更新 |

## 10. 实施顺序与验收标准

**PR-A（拆分主体）** = S1+S2+S3+S4+S6+S7 + review.py 退役 + EventBus 参数化 + PredictionConsumer + from-trace 端点 + Node 代理 + 前端统一读 records。

**PR-B（日期修复）** = S5（date.py 补充源 + due_dates_failed 显式化）。

**PR-A 验收标准**
1. review 生成流程不再产生预测（内联代码退役）；`prediction_records` 仅 `PredictionConsumer` / from-trace 端点写入。
2. 事件驱动模式下：full review `status=="ok"` → 发布 `review_done` → 预测生成并落库；quick 不触发；review 降级不触发。
3. 旧串行链路（`QUICK_SNAPSHOT_ENABLED=false`）下自动预测明确断链并有日志/告警（或强制生产置 true，见硬约束 1）。
4. `run_predict` 状态化后，`gate_skipped`/`llm_failed`/`parse_failed`/`due_dates_failed` 均有测试覆盖；retry-once 仅对可重试状态触发。
5. 大盘溯源页预判卡片显示 records 数据（G14 空态修复）；B2.1 命中率页行为不变（除 skipped 隔离）。
6. skipped 落库闭环（POST 支持 status）；`computeStats` 不把 skipped 计入进行中。
7. 越年时 `due_dates_failed` 显式告警，不抛错、不静默产出错误日期。
8. 全量 A/B 测试：HEAD 失败集 ⊆ BASE（新增清零）；ruff 0；app-api tsc 0；跨仓契约字段级一致。

## 11. 辩论记录摘要（design-debate，3 轮）

- **R1**：正方论证独立拆分的可行性（输入可还原、管道现成、验证已独立、复用与降级继承）→ 反方攻击（UPSERT 不重置 verification、4 写点、DB 兜底跨日污染、鉴权缺失、chinese_calendar 越年、trigger_source 守卫等 G1-G10）→ 裁决：8/10 缺口站得住，进 R2。
- **R2**：正方全盘认领并出修订 v2（R1-R10：唯一写者 / trade_date 校验 / 事件驱动 / Node 代理 / 已验证保护 / 打标制 / 并行修复 / skipped）→ 反方追打（R3 无现成事件可消费且 EventBus 单组、retry 无契约落点、quick/full 竞态、前端字段错位 G14 等 G11-G17）→ 裁决：R2/R6 闭合，R3/R7 未闭合，进 R3。
- **R3**：正方终局 v3（S1-S7：review_done 事件 + 独立组 / 契约状态化 / full-only 单写者 / 统一读 records / 并行修复 / 回调链完整定义 / skipped 域扩展）→ 反方终审：**有条件通过**，G18（`quick_snapshot_enabled=false` 默认下旧串行路径断链）为唯一阻挡项，G19（skipped 落库未闭环）/G20（AGENT_PY_URL 命名）为低项 → 裁决：G18 转硬约束 1（生产强制事件驱动 + 旧链路补发 review_done 双保险），G19/G20 转实现细节，**收敛**。

## 12. 相关文件

- `src/aistock_agent/services/prediction_service.py`（run_predict / run_chat_prediction / render_prediction_markdown）
- `src/aistock_agent/schemas/prediction.py`（PredictionResult）
- `src/aistock_agent/schemas/market_trace.py`（MarketTraceResult / ReviewArtifact）
- `src/aistock_agent/agents/workers/review.py`（内联预测退役点）
- `src/aistock_agent/services/event_bus.py` / `event_consumers.py`（review_done + prediction_chain）
- `src/aistock_agent/services/prediction_validator.py`（16:00 验证扫描）
- `src/aistock_agent/utils/date.py`（G6 修复点）
- `aistock-app-api/src/modules/prediction/PredictionRecordService.ts` / `core/routes/publicRouter.ts` / `internalRouter.ts`
- `aistock-app-frontend/src/modules/analytics/utils/marketTraceReview.ts` / `pages/traceability.vue`
