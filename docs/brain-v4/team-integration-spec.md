# 智慧大脑 V4 团队接入与迁移规范

> 日期：2026-11-27  
> 状态：V4.4 接入方案；briefing 与 market_trace 两条试点已完成，公共接口冻结为 V4.1 接入基线  
> 适用范围：`aistock-agent-py`、`aistock-app-api`、App/Web 输出适配

## 1. 目标

本规范用于组长完成 V4 公共架构和两条试点链后，各功能负责人将现有功能逐步接入 V4。目标是：

1. 功能负责人只修改自己的垂直目录，不争抢公共 builder、state 或契约文件。
2. 数据、研究、研判、持久化和表达边界明确，不把旧 worker 整块搬入新架构。
3. 每个功能都能独立测试、影子运行、灰度、回滚和退役旧链。
4. 同一产品出口始终只有一个 active owner，不产生两套正式结论。

`graph/brain` 是待删除的错误原型，`brain_v3` 不落地。二者都不是接入目标、依赖或回滚层。

## 2. 双仓职责

| 仓库 | 负责 | 禁止 |
|---|---|---|
| `aistock-agent-py` | ResearchRequest、研究循环、分析师 profile、议事、回改、renderer、验证、评测 | 复制 app-api 业务数据服务；直接写正式数据库；从旧 worker state 继承隐式字段 |
| `aistock-app-api` | 权威数据、快照录制、影子归档、正式 ledger、版本分配、灰度路由、统一读取接口 | 执行 LLM 研究循环；在 API 层重新生成结论 |
| App/Web | 灰度后展示统一 briefing/judgment 投影 | 首阶段为影子评测修改产品前端；前端拼接多个报告形成新结论；影子数据进入正式页面 |

## 3. 目录和所有权

~~~text
src/aistock_agent/brain_v4/
  contracts/                 # 红区：跨模块契约和 schema 生成源
  graph/                     # 红区：state、reducers、profiles、主图装配
  controller/                # 红区：计划、充分性、回改和停止策略
  deliberation/              # 红/黄区：协作路由、议事和单写提交
  tools/                     # 黄区：统一工具网关、snapshot、provenance、memo
  capabilities/              # 黄区：可复用领域能力
    trend/
    event/
    wind/
    fundamental/
  features/                  # 绿区：产品功能垂直切片
    briefing/
      manifest.yaml
      profiles.py
      adapter.py
      projection.py
      tests/
      fixtures/
    market_trace/
      manifest.yaml
      ...
  generated/
    registry.lock            # CI 生成，禁止手改
~~~

### 3.1 红黄绿规则

| 区域 | 修改权限 | 合并门禁 |
|---|---|---|
| 红区 | 平台 owner 主改 | 平台 owner + 跨层契约 review + 全量核心回放 |
| 黄区 | capability owner 主改 | 平台 owner 联审 + 所有依赖 profile 回放 |
| 绿区 | 功能 owner 自主修改 | 本功能 contract、fixtures、降级和影子测试通过 |

功能负责人不得修改公共 builder 来“挂节点”，也不得直接 import 另一个 `features/<feature>/`。跨功能复用必须下沉为 capability、ledger 读取或稳定契约。

## 4. 功能 Manifest

每个 feature 和 capability 在自己的目录维护 `manifest.yaml`。中央 `registry.yaml` 不存在，避免多人同时修改同一个热点文件。

~~~yaml
id: briefing_midday
kind: feature
owner: briefing-team
status: shadow
profiles: [briefing_midday]
input_contract: ResearchRequest.v1
output_contract: MiddayBody.v1
capabilities: [market_snapshot, event_evidence, trend]
depends_on_features: []
data_cutoff_policy: midday_1130_cn
timeout_s: 90
token_budget: 18000
active_owner: legacy_midday
shadow_owner: brain_v4
degradation:
  missing_parent: insufficient
  optional_capability_failure: partial
legacy:
  adapter: midday_legacy_adapter
  callers: [scheduler_midday, admin_midday_trigger]
  delete_when: v4_100_percent_stable_10_trading_days
~~~

CI 聚合所有 manifest，生成 `generated/registry.lock`，并验证：

- ID、owner、输入输出契约和状态均存在。
- 依赖项真实存在且没有循环。
- profile 与 capability 组合合法。
- 同一出口只有一个 active owner，最多一个 shadow owner。
- legacy adapter 有调用方、删除条件和到期检查。
- 实际目录与 manifest 双向一致。
- registry 指纹变化在 PR 中可见。

## 5. 契约规则

1. Pydantic 是跨语言契约唯一事实源；JSON Schema 和 TypeScript 类型由 CI 生成，禁止手改生成物。
2. feature 只返回约定契约，不向公共 state 塞私有临时字段。
3. 并行节点只能追加被批准的 reducer 通道；HypothesisGraph、CausalGraph、CouncilDecision 和 JudgmentDraft 必须单写。
4. Evidence 必须包含来源、`event_time`、`published_at`、`fetched_at`、`as_of`、质量和错误分类。
5. run 必须记录 `run_id`、`profile`、`snapshot_id`、`as_of`、`config_version`、模型版本、prompt 版本、method 版本和工具版本。
6. `run_status` 只表达结论完整性：`ok | partial | insufficient`；`stop_reason` 独立记录 sufficient、budget、timeout、stall、unstable 或 error。

## 6. 功能拆分方法

每个旧功能必须先拆成以下角色，不能整块复制 worker：

| 角色 | 说明 | 示例 |
|---|---|---|
| Trigger | 何时启动和幂等键 | cron、事件、用户请求 |
| Deterministic Capability | 可计算、可复现的规则或算法 | 趋势评分、恐贪、榜单、日期计算 |
| Evidence Adapter | 把权威数据转成 provenance envelope | 行情、新闻、事件、财务 |
| Analyst Profile | 解释不确定事实，形成结构化 claim | 市场归因、事件影响、基本面解释 |
| Product Feature | 必答问题、profile、输出优先级 | 早报、午报、晚报、市场溯源 |
| Renderer/Notifier | 只投影已有 judgment | App、Web、文字、音频、推送 |
| Verification | 到期验证和错误标签 | direction、timing、cause、confidence |

同一个旧 worker 可以被拆到多个角色；一个角色也可以被多个产品复用。

## 7. 标准接入流程

1. **领取执行包**：从冻结的 V4.1 基线签发一个 Execution Packet，固定 prerequisite commit、allowlist、禁止目录、契约、测试和验证命令。
2. **盘点**：列出旧入口、cron、调用方、数据源、写库、缓存、输出、超时和降级行为。
3. **分类**：把旧逻辑映射到 Trigger、Capability、Adapter、Profile、Renderer、Verification。
4. **脚手架生成**：运行 `scripts/brain_v4/scaffold_feature.py` 生成本功能 manifest、profile、adapter、projection、fixtures 和 tests；不得复制其他 feature 的领域逻辑。
5. **定义契约**：确认输入、输出、as_of、cutoff、幂等键和 owner；需要公共契约变化时停止功能包，另开平台执行包。
6. **登记 manifest**：状态先为 `draft`，active owner 仍指向生产旧链，shadow owner 为 `brain_v4`。
7. **测试驱动实现**：严格按执行包先写失败测试，每个包只实现一个可评审行为。
8. **独立测试**：功能包能在不修改主图的情况下运行 contract suite、fixtures 和降级测试。
9. **影子运行**：状态改为 `shadow`，同一录制快照并行执行，V4 只写影子表并生成通用 `EvaluationEnvelope`。
10. **静态评测台**：生成旧链/V4 双栏 HTML；旧链不是标准答案，质量比较使用冻结案例和随机标签盲评。
11. **灰度**：按 1% -> 10% -> 50% -> 100% 晋级，每级均可切回旧 active owner。
12. **只读旧链**：V4 成为 active owner 后，旧链停止写正式结果，仅保留回滚观察期。
13. **删除**：稳定达到 manifest 的删除条件后，停 cron、删路由、删 adapter、删旧测试和配置。

### 7.1 受限 Execution Packet

模板位于 `docs/superpowers/plans/brain-v4-task-packet-template.md`。执行较弱模型时只提供该包列出的 Required Context，不把整个仓库和总计划交给模型自行决定实现范围。

每个执行包必须满足：

- 一个目标、一个提交、一个 owner。
- 精确到文件的 allowlist 和 forbidden paths。
- 精确的消费/产出契约与版本。
- 一个先失败的目标测试和准确的预期失败原因。
- 固定验证命令、fixture/snapshot SHA 和完成证据格式。
- `verify_change_scope` 通过；实际改动文件不得超出 allowlist。

接口冻结后，普通功能执行包默认只能修改：

~~~text
src/aistock_agent/brain_v4/features/<feature>/
tests/unit/brain_v4/features/<feature>/
tests/integration/brain_v4/features/<feature>/
brain_v4_evals/cases/<feature>/
~~~

公共 contracts、state、builder、ledger、gateway、registry 生成器和 app-api schema 属于红区。功能负责人或实现模型发现必须修改红区时，应提交最小契约提案和失败案例，由平台 owner 签发独立执行包；不得在功能 PR 中顺带修改。

## 8. 并行开发防冲突

- 每个负责人只写自己的 `features/<feature>/` 或被授权的 capability 目录。
- 每个 PR 必须关联一个已填完整的 Execution Packet；CI 校验 prerequisite commit、修改范围和最大文件数。
- 不允许 PR 同时修改 prompt、工具集合、预算和循环策略；一次只改变一个可归因维度。
- 公共契约变更单独 PR，先合并契约与生成物，再并行开发消费者。
- registry.lock 由 CI 生成；冲突通过 manifest 内容解决，不手工合并 lock。
- 跨功能需求先提出 capability 接口，不把公共代码复制进多个 feature。
- feature PR 必须证明早报、午报、晚报和市场溯源中的受影响 profile 没有回归。

## 9. PR 必备材料

- manifest diff 和 owner。
- Execution Packet、scope 校验结果和准确 prerequisite commit。
- 输入/输出契约变化及兼容策略。
- 正常、partial、insufficient、timeout 路径测试。
- 固定 snapshot 的回放结果。
- trace 示例和 evidence 引用完整性。
- P50/P95、token、成本和工具重复率变化。
- 影子开关、灰度范围和一键回滚方法。
- legacy 调用方列表和删除条件。
- 其他 profile 不受影响的验证结果。

## 10. 完成定义

一个功能只有同时满足以下条件才算完成接入：

1. manifest 和 registry 校验通过。
2. Execution Packet 的 allowlist、失败测试、范围检查和完成证据齐全。
3. 契约、reducer、降级、时间泄漏和幂等测试通过。
4. 影子数据不进入任何正式查询。
5. 同一冻结快照可重放，关键 evidence ID 和输出关系可复现。
6. 通用静态 HTML 评测台可以查看旧链/V4 双栏结果和全部指标。
7. 达到该功能规定的样本量、质量、时延、成本和失败率门槛。
8. active owner 已唯一切换到 V4，旧链进入只读观察期。
9. 删除条件到期后，旧 cron、路由、写入、adapter 和配置全部移除。

试点完成后，组长以 briefing 和 market_trace 两个目录作为样板。其他负责人复制接入流程和文件结构，不复制具体领域逻辑。

## 11. V4.1 接口冻结基线（2026-11-27）

两条试点完成后的冻结范围：

| 区域 | 已冻结契约 / 版本 |
|---|---|
| 简报 | `BriefingEnvelope.v1`、`MorningBody.v1`、`MiddayBody.v1`、`EveningBody.v1`、`Scenario.v1`、`BriefingRelation.v1` |
| 研判 | `V4Judgment.v1`、`EvidenceItem.v1`、`EvidenceLedger.v1`、`Hypothesis.v1`、`CausalEdge.v1` |
| 运行 | `RunProfile.v1`（8 档阈值冻结）、`FeatureManifest.v1`、`generated/registry.lock` |
| 评测 | `EvaluationEnvelope.v1`（`is_shadow` 恒为 `True`，旧链证据不得复用为 V4 证据） |

冻结后的变更规则：

1. **仅允许加性变更**：新增可选字段必须带默认值，或新增独立契约版本号；删除、重命名、收紧必填、改变既有语义，必须另开红区契约执行包并升版本号，不得在 feature PR 中顺带修改。
2. **红区兼容优先**：`contracts/`、`graph/`、`controller/`、`deliberation/`、`generated/` 的修改先保证既有消费者按旧版本可运行，再引入新版本；红区变更必须附失败市场案例 ID。
3. **registry.lock 只由 CLI 生成**：manifest 变更后必须运行 `uv run python -m aistock_agent.brain_v4.registry --write-lock` 并提交生成物；禁止手改 lock。
4. **feature 只消费冻结契约**：新 feature 需要新字段时，先提交失败案例与最小契约提案，由平台 owner 签发独立执行包。
5. **时段隔离不变量**：早/午/晚必须使用独立 `run_id`、`snapshot_id`、`data_cutoff` 与 ledger namespace；午报必须携带 `parent_judgment_id`，晚报只按 ID 引用 market_trace judgment，不得另起盘后归因。

## 12. 实测经验与常见坑（2026-11-27 试点沉淀）

本节只记录**已经在服务器真实跑过并踩到**的问题与处置，供后续功能负责人直接复用。每条都对应一次真实故障。

### 12.1 独立进程运行（CLI / 离线脚本 / 一次性任务）

1. **HTTP 池只在 FastAPI lifespan 初始化**。独立进程必须先 `ensure_app_env()` 再 `await ensure_runtime_pools()`；否则所有上游取数抛 `HttpClientPool not initialized` 并被降级成 `None`，表现为 `missing_fields` 全空、`archive` 报 `empty_response`，而不是明显报错。
2. **`settings` 读 `.env.{APP_ENV}`**。独立进程未设 `APP_ENV=production` 会读到 development 默认值（`NODE_API_BASE_URL=http://localhost:3000`），端口与线上不符。
3. **一次 `asyncio.run` 跑完整条链**。`httpx.AsyncClient` 绑定创建它的事件循环；先用 `asyncio.run(run_live())` 再 `asyncio.run(archive_run())`，第二个循环里归档必然静默失败。

### 12.2 app-api 快照字段陷阱

1. `market/close-snapshot` 与 `last-close-snapshot` 的 `indexes` 是 **list**，元素用 `ts_code`（如 `000300.SH`）与 `pct_chg`；不要按 dict 或 `change_pct` 读取，否则该字段永远被判缺失。
2. 快照**没有** `style` 字段。大小盘风格必须由指数涨跌幅确定性派生（`沪深300 − 中证1000`），标注 `provider="derived:indexes"`、`method="index_spread_v1"`，禁止编造。
3. 当日收盘前 `close-snapshot` 返回 409/404，必须回退 `last-close-snapshot` 并在审计里标 `data_source=last_close`；不得把缺失当异常上抛。

### 12.3 LLM 预算与推理深度

1. **预算必须来自真实 usage**：只能通过 callback 读取 `llm_output["token_usage"]` 或 `AIMessage.usage_metadata`；依赖中间件 `response.result` 的路径在真实 ChatOpenAI/DeepSeek 下可能拿不到，预算会形同虚设（实测 32k 预算跑出 228k）。
2. **并行席位会放大预算**：席位并行扇出时必须按席位数均分剩余预算，否则一轮消耗 = 剩余 × 席位数。
3. **给终局推理预留额度**：token 上限只能在一次模型调用结束后检查，天然存在一次溢出；主工具循环压到 90%，余量留给终局推理，run 总量才稳定落在 1.2 倍内。
4. **DeepSeek thinking 模型不支持强制 `tool_choice`**（`ToolStrategy` 会 400）。需要深度时不要在全程关 thinking，而是“工具循环正常跑 + 无工具的一次终局推理”（不触发 tool_choice）。
5. **工具结果必须压缩后再进上下文**：原始行情/板块 JSON 单次可达 1.5 万 token。压缩时保留全部 `evidence_id` 与关键数值，截断长尾并标 `truncated=true`，不影响结论口径。

### 12.4 影子归档

1. `v4_run_id` 必须**幂等 upsert**（`ON CONFLICT (v4_run_id) DO UPDATE`），否则同日重跑直接 500。
2. 归档失败不得吞错：`NodeApiClient.post` 失败时必须用 `error_out` 取真实原因并写进审计，不能只报 `empty_response`。
3. 只写 `brain_v4_runs` / `brain_v4_comparisons`，`is_shadow` 恒为 `true`；读取端只查影子表。

### 12.5 运维（pm2）

`pm2 restart <app> --update-env` 会用**当前 shell 环境**覆盖进程原有环境。从裸 SSH 会话执行会丢 `DATABASE_URL`、Redis 与各类密钥，表现为进程 online 但迟迟不监听端口。正确做法：

~~~bash
cd ~/aistock-app-api && set -a && . ./.env && set +a && pm2 restart aistock-app-api --update-env
~~~

或直接 `pm2 restart <app>`（不加 `--update-env`，保留原环境）。Python 侧因 `BaseSettings` 读取 `.env.production` 文件，受影响较小，但仍应避免无谓的 `--update-env`。

## 13. 运行层增补登记（V4.1.1，加性）

以下为实现过程中新增的运行层能力，**未修改 §11 冻结契约的既有字段**：

| 机制 | 位置 | 目的 | 契约影响 |
|---|---|---|---|
| 实时取数 `NodeToolClient` / 工具白名单 | `brain_v4/tools/{node_client,tool_map}.py` | 让 V4 通过 app-api 内部接口取真实数据 | 无（冻结契约字段未变） |
| 运行器与投影 | `brain_v4/runtime/{inputs,runner,graph_run,briefing_projection,archive,bootstrap,cli}.py` | 单次真实 run 的编排、briefing.v1 投影、影子归档、静态审计 | 新增绿区层，不进入红区 |
| 真实 usage 回调与预算公式 | `brain_v4/analysts/kernel.py` | 硬性 cost 上限；席位均分 + 终局预留 | 实现层，无契约变更 |
| thinking 终局推理（可选注入） | `brain_v4/analysts/{kernel,model_factory}.py` | 在工具循环之外恢复推理深度 | `RunProfile` 未加字段 |
| 大小盘风格派生 | `brain_v4/runtime/inputs.py` | 补齐快照缺失的 `style` | `SourceRecord` 复用既有 `provider`/`source_level`，未改 schema |
| 影子归档幂等 | `aistock-app-api/src/modules/brain-v4/BrainV4ArchiveService.ts` | 同日重跑覆盖而非 500 | 表结构未变 |
| 定时调度开关 | `config.py`（`brain_v4_*`）、`services/scheduler.py` | 与旧链并行的 08:51/12:06/15:36/15:37 四个影子任务 | 默认关闭，不影响旧链 |

上述均属“加性实现”。若后续要改动 §11 的契约字段，仍须按 §11 第 1、2 条另开红区执行包。

## 14. 评测台联动状态（重要缺口）

当前真实 run 的审计链路为：

~~~text
run_live -> brain_v4_runs（影子表）
         -> GET /internal/brain-v4/runs | /briefings（只读）
         -> runtime/archive.write_daily_report（单栏每日审计页）
~~~

**已联动**：影子归档、只读 JSON 出口、每日单栏审计页、`legacy_output` 已被采集并存入 payload。

**未联动**：真实 run 目前**没有**构造 `EvaluationEnvelope`，也**没有**调用 `brain_v4_evals.reporting.generate_report`。因此：

1. 没有旧链/V4 **双栏**对比页；
2. 没有 `snapshot_sha` 一致性校验下的盲评字段；
3. 只有离线 fixture 试点（Task 8/9）产出的评测台页面，与线上信号无关。

补齐方式（建议独立执行包 P13）：在 `runtime/` 增加 `envelope` 适配器，把一次 `LiveRunResult` + 当天 `legacy_output` 映射为 `EvaluationEnvelope`，再调用 `generate_report` 输出 `reports/live/<date>/`。**这是当前唯一未满足的架构承诺**（§10 完成定义第 6 条）。

## 15. 红区变更流程（跨工具强制）

### 15.1 为什么要跨工具

组内工具不统一（Trae、Claude Code、Codex、WorkBuddy、Cursor）。`.trae/rules/` 只对 Trae 生效；其他工具普遍读取仓库根 `AGENTS.md`。因此：

- **规则正文写 `AGENTS.md`**（跨工具基线）；
- **Trae 额外在 `.trae/rules/aistock-workflow.md` 引用同一份红区清单**，不重复定义；
- **强制手段用机器门禁**（pre-commit + CI），不依赖模型自觉。

### 15.2 红区清单（唯一来源）

以 `aistock-agent-py/AGENTS.md` 的「红区清单」为准，代码侧同源实现为 `scripts/brain_v4/pre_commit_guard.py` 中的 `RED_ZONE_PREFIXES`。修改清单必须两处同步。

### 15.3 六步强制流程

1. 识别到红区路径 → **停止编码**。
2. 在 `docs/brain-v4/packets/` 建执行包，写全 `packet_id`、目标、`prerequisite_commit`、`allowed_files`、`forbidden_paths`、消费/产出契约、**先失败的测试**、实现步骤、验证命令、完成证据。
3. 红区（contracts / graph / controller / deliberation）改动必须附**失败案例 ID 或失败测试名**；没有失败证据不得改。
4. 只改 allowlist；提交前用 `python -m scripts.brain_v4.pre_commit_guard` 或 `git diff --name-only` 自查。
5. 提交信息正文含 `Packet: <packet-id>`。
6. 冻结字段的语义变更必须升版本、单独提交。

### 15.4 机器门禁

~~~bash
# 本地：安装 pre-commit 门禁
cp scripts/git-hooks/pre-commit .git/hooks/pre-commit && chmod +x .git/hooks/pre-commit
~~~

门禁行为：暂存文件命中红区前缀，且 `docs/brain-v4/packets/` 中没有任何执行包的 allowlist 覆盖这些路径时，**直接拒绝提交**并列出缺失路径。

安装后可用以下命令自测（预期被拒绝）：

~~~bash
echo 'touch src/aistock_agent/brain_v4/graph/_probe.py' >/dev/null
~~~

### 15.5 违规处理的既定动作

发现"已合并但未走包"的红区改动时，不 revert 线上可用代码，而是：

1. 补建追溯执行包，注明 `retroactive: true` 与实际提交 SHA；
2. 在包内补写"该改动影响的 profile 与失败案例"；
3. 由平台 owner 复核后合并追溯包；
4. 下一个红区改动必须走完整前置包，不得再追溯。

## 16. 运行不变量（V4.1.1 细化）

以下不是新架构能力，而是让 §11 已声明的不变量**真正成立**的实现规则：

### 16.1 run 级 token 预算

- 预算口径：`RunProfile.token_budget` 为**单次 run 累计**上限，硬上限 = 1.2 × 预算。
- 计数来源：必须取真实 usage（`llm_output["token_usage"]` 或 `AIMessage.usage_metadata`），不得使用模型自报值。
- 并行扇出：席位并行时，剩余预算必须**按席位数均分**下发；每个席位不得拿到"剩余全额"。
- 终局推理：启用 thinking 终局推理时，主工具循环只分配 90%，剩余 10% 留给终局调用。
- 未达标判据：run 结束若 `total_tokens > 1.2 × token_budget`，视为预算不变量被破坏，必须修实现而不是调大预算。

### 16.2 上下文与工具结果

- 工具结果进入模型上下文前必须结构化压缩，保留全部 `evidence_id` 与关键数值，截断长尾并标 `truncated=true`。
- 压缩只影响"给模型看的文本"，不得改变 `ToolEnvelope.data` 与证据账本。

### 16.3 时段与 parent

- 早/午/晚和市场溯源使用独立 `run_id`、`snapshot_id`、`data_cutoff`、ledger namespace；parent 关系只用于串联，不用于覆盖。

## 17. 已知流程缺口（待补齐）

1. **CI 尚未接入红区门禁**：目前只有本地 hook，可被 `--no-verify` 绕过；建议在 CI 增加同一脚本。
2. **CODEOWNERS 未配置**：红区目录尚无强制 reviewer。
3. **评测台未与线上 run 联动**：见 §14；规格见 `docs/brain-v4/evaluation-console-spec.md`。
