# 智慧大脑 V4 设计规格：目标驱动研究内核

> 日期：2026-11-26；V4.4 更新：2026-11-27  
> 状态：V4.4 设计定稿；`brain` 错误原型删除，`brain_v3` 不落地，V4 独立建设  
> V4.1 决策：采用“方案 B · 案卷制分析师议会”，议事层是正式运行层，不是自由群聊  
> V4.2 决策：保留现有产品入口，打碎独立分析竖井；早午晚报采用三种运行 Profile；首批试点改为“早午晚报日内链 + 市场溯源”  
> V4.4 决策：采用“公共架构与议会先完成 -> 两个试点校正 -> 冻结接口 -> 逐功能绞杀迁移”；首阶段以通用静态 HTML 评测台验收，不修改产品 App/Web 前端  
> 试点：早报、午报、晚报、市场溯源

## 0. 核心结论

V4 不再用“增加 agent 数量”代表智能升级。它把一次性报告流水线改成可验证、可回溯、可回改的研究循环：

~~~text
研究目标 -> 竞争假设 -> 证据计划 -> 取证与计算 -> 充分性审查
         -> 补证 / 修改假设 / 否决假设 -> 反方审查 -> 研判落账
         -> 到期验证 -> 错误分类 -> 离线方法改进 -> 回放 A/B 晋级
~~~

市场溯源六因子链仍然保留，但只作为最终产出形态。内部先形成带分支、反证和版本记录的因果图；只有通过深度门禁的路径，才允许压缩成六因子链。

V4 的五条硬承诺：

1. 没有证据的因果边必须标为假设，不能伪装成事实。
2. 后面发现矛盾时，能定位并解锁受影响的上游假设，而不是整篇重写。
3. 不同时间档位拥有独立快照、预算、状态和 judgment owner。
4. 旧 agent 先拆成 tool、skill、adapter 或 profile，按功能逐步迁移。
5. “越用越聪明”由真实结果和离线评测驱动，不允许线上自动修改方法。

V4.1 新增五条协作承诺：

1. 分析师可以并行提出立场、异议和补证请求，但不能并行改写正式假设图和 judgment。
2. 议事围绕带 ID 的 claim、causal edge、evidence 和 agenda item 展开，禁止无对象的自由聊天。
3. 裁决者只能比较已有证据和论证；发现新缺口必须退回研究循环，不能在裁决时创造证据。
4. 未解决分歧、少数意见和 insufficient 必须保留，不能为了形成一致结论而抹平冲突。
5. 分析师观点可以相互影响，但每次影响必须落成 BeliefUpdate 和 InfluenceEdge；同伴意见本身不是证据。

V4.2 新增五条产品重构承诺：

1. 保留 App 与 Web 两套在用前端，但统一读取 judgment、evidence 和 briefing 契约。
2. 早报、午报、晚报不是同一个流程换 slot，而是共享内核的三种 Research Profile。
3. 同一自然日的三次运行以 parent_run_id 和 relation 串联，前一时点结论不可被后一次静默覆盖。
4. 事件先统一为逻辑主记录，再逐步迁移物理存储；统一事件不等于立即合并数据库表。
5. 产品 owner 负责 Profile、输出投影和评测集；公共取数、账本、编排、议事和验证由平台 owner 负责。

V4.4 新增五条迁移与评测承诺：

1. 组长先完成公共契约、状态、证据、控制器、主图、议会、影子归档和评测基础设施，再让业务负责人接入功能。
2. 当前生产链始终是回滚基线；V4 在同一冻结快照上独立研究，旧链结果不能作为 V4 evidence 或正确答案。
3. 公共架构先通过合成案例和冻结案例验收；随后用市场溯源和早午晚报两个真实试点校正抽象，试点通过后才冻结 V4.1 接入接口。
4. 首阶段生成组长可见的通用静态 HTML 影子评测台，不修改 `aistock-app-frontend` 或 `aistock-frontend`，不向普通线上用户暴露影子结果。
5. 接口冻结后，其他功能按受限执行包和脚手架在自己的绿区目录接入；评测契约、配对运行、影子归档和晋级门禁长期保留。

## 1. 对旧方案与现有生产链路的审计

### 1.1 保留的方法与生产资产

| 可保留资产 | V4 处理 |
|---|---|
| app-api 是数据 owner | 保留；Python 不复制业务数据服务 |
| Evidence / Claim / Judgment | 保留并扩展成版本化研究对象 |
| as_of、snapshot_id、published_at | 保留并成为全部研究请求的硬约束 |
| 工具 provenance 信封 | 保留；工具必须返回来源、三时间、质量和错误 |
| 多空、冲突、验证、方法演化 | 保留，但重新放入研究循环和离线学习闭环 |
| 旧 worker 的工具与业务经验 | 作为 adapter、skill 和评测素材迁移 |
| 影子运行和 A/B 方法 | 保留；对照对象是当前真实生产链路，不是 `brain` 原型 |

### 1.2 旧 `brain` / V3 方案的结构性缺口

| 缺口 | 直接后果 | V4 修正 |
|---|---|---|
| 串行子图跑完才收口 | 不知道证据是否充分，也不能定向补证 | ResearchController 每轮决定 Gap、Revise 或 Stop |
| 六因子链兼任思考流程 | 模型倾向把每个格子填满，形成表面归因 | 内部先建 CausalGraph，出口再投影六因子链 |
| claim 没有依赖和版本 | 后面发现错误只能改文字，无法局部重算 | HypothesisVersion + RevisionEvent + ImpactAnalysis |
| 五个能力包都趋向 agent | 取数、计算、判断边界混乱 | 工具优先，多步歧义任务才临时升级为专项 agent |
| 方法先验是隐式共享字典 | 不同功能、时间和版本容易相互污染 | 独立 METHOD 版本，按 profile、horizon、主体读取 |
| 学习闭环未形成运行机制 | 系统不会从真实错误中稳定进步 | VerificationRecord -> ErrorTags -> MethodCandidate -> A/B |

### 1.3 错误原型、生产旧链与 V4 的关系

- `src/aistock_agent/graph/brain/` 是未上线且错误落地一半的原型，不作为影子基线、回滚目标或复用底座；保存一次可追溯现场后从主干彻底删除。
- `brain_v3/` 从未落地，不再创建该目录；V3 文档只保留为历史决策材料，并明确标记 `superseded`。
- V4 使用独立 `brain_v4/` 目录、状态、归档和评测集，不导入 `graph/brain`，也不兼容其内部 state、builder 或 judgment schema。
- V4 的影子对照与回滚目标是当前真实生产功能链：morning、midday、review、evening/broadcast、wind、event 等现网路径。
- V4 与生产旧链使用同一录制快照配对运行，按功能通过门禁后灰度；旧链在对应功能 100% 切换且稳定后再退役。

### 1.4 仓库边界与首阶段改动范围

| 仓库 | V4 职责 | 首阶段改动 |
|---|---|---|
| `aistock-agent-py` | 研究内核、LangGraph 编排、分析师议会、profile、renderer、验证与离线评测 | 新建 `src/aistock_agent/brain_v4/` 和 `brain_v4_evals/`；删除错误 `graph/brain` 原型及其入口、测试和脚本 |
| `aistock-app-api` | A 股数据唯一 owner、快照录制、V4 影子归档、正式账本、灰度路由、统一读接口 | 新建独立 `modules/brain-v4/` 与 V4 migration；提供影子归档和只读对比 API；不得沿用旧 `brain_run_archives` 的 `DATE as_of` 和无 profile 结构 |
| `brain_v4_evals` | 配对回放、统一评测信封、静态 HTML 评测台、盲评和晋级报告 | 生成可离线打开的索引、双栏报告和 JSON 数据；查看器不承载业务判断 |
| App/Web 前端 | 最终消费同一 `BriefingEnvelope`、judgment 与错误态 | 首阶段不修改两个产品前端；通过门禁准备灰度时再增加正式双读和灰度适配 |
| 旧生产 worker | 灰度前继续承担正式出口 | 只通过 legacy adapter 被 V4 影子读取；V4 不反向依赖其内部 state |

因此 V4 会同时修改 `aistock-agent-py` 和 `aistock-app-api`。前者负责“怎么研究和形成结论”，后者负责“权威数据、持久化、版本分配、灰度和对外读取”。

### 1.5 方案 A 具体化：架构先行、试点校正、逐功能绞杀

方案 A 不是“新旧系统长期并存”，也不是先让各功能边做边修改公共层。组长先完成可运行的公共架构与议会，再用两个真实试点校正接口，最后由各功能按同一套门禁完成可回滚替换：

~~~text
冻结当前生产基线
  -> 完成 contracts / state / profiles / registry
  -> 完成 Evidence Gateway / snapshot / provenance
  -> 完成 ResearchController / sufficiency / revision / stop policy
  -> 完成 LangGraph 主图与案卷制议会
  -> 完成影子归档、EvaluationEnvelope 和静态 HTML 评测台
  -> 公共架构合成案例验收
  -> 接入市场溯源试点
  -> 接入早报 -> 午报 -> 晚报试点
  -> 根据两个试点修正并冻结 V4.1 公共接口
  -> 其他功能负责人按受限执行包并行接入
  -> 冻结案例配对 A/B 与人工盲评
  -> 单功能灰度 1% -> 10% -> 50% -> 100%
  -> 旧链只读观察 -> 删除旧入口、写入和 adapter
~~~

公共架构阶段允许暂不产生正式业务报告，但必须用合成案卷和冻结案例证明完整状态流、议会、单写入、停止、归档和评测链可以运行，不能只提交空接口或目录骨架。两个试点是公共接口冻结前的强制校正门禁；试点之后红区进入兼容优先状态。一个出口在任一时刻只能有一个 active owner；V4 在晋级前只能是 shadow owner。

复用边界如下：

- 可以复用权威数据接口、冻结快照、provenance、确定性计算、触发时点和只读 renderer 组件。
- 必须重建研究状态、竞争假设、证据计划、充分性门禁、回改、judgment 和验证闭环。
- 旧 worker 的最终结论、内部 state、完整 prompt 和一次性报告编排不能进入 V4 研究内核。
- 旧链输出只进入 `legacy_output` 对照栏；除非它引用的原始事实重新经过 Evidence Gateway，否则不能成为 V4 evidence。

## 2. V4 总体架构

### 2.1 九个边界

| 层 | 负责什么 | 不负责什么 |
|---|---|---|
| Trigger / Profile | 选择档位、预算、工具权限和输出契约 | 不做业务判断 |
| Research Context | 固定主体、时点、快照、horizon 和目标 | 不读取系统当前时间替代 as_of |
| Research Controller | 假设、证据计划、Gap、回改、停止 | 不直接取业务数据 |
| Evidence Fabric | 数据工具、计算器、检索、历史适配 | 不写最终结论 |
| Analyst Kernel | 解释不确定事实，形成结构化 claim | 不做确定性算术，不直接落 judgment |
| Sufficiency / Collaboration Router | 判断证据深度、冲突和协作模式 | 不生成业务论点 |
| Deliberation Layer | 独立立论、异议、补证、答辩、裁决 | 不自由群聊，不直接写正式图 |
| Judgment / Output | 单写回改、研判落账和六因子投影 | 不重新研究，不创造 evidence |
| Verification / Method Learning | 到期验证、错误分类、方法候选、回放和晋级 | 不在线修改生产方法 |

### 2.2 主流程

~~~text
Trigger -> ProfileSelector -> ResearchContext -> ResearchController
                                              |
                         +--------------------+--------------------+
                         |                                         |
                         v                                         v
                 Hypothesis Plan                              Tool Router
                         |                                         |
                         +--------------------+--------------------+
                                              v
                                      Evidence Fabric
                                              |
                                              v
                                       Analyst Kernel
                                              |
                                              v
                                      Sufficiency Gate
                                    /                  \
                                  gap                 pass
                                   |                   |
                                   v                   v
                          Revision Controller  Collaboration Router
                                                   /    |    |    \
                                                solo consult council red-team
                                                   \    |    |    /
                                                    Deliberation Layer
                                                           |
                                                    Decision Gate
                                                    /    |      \
                                                revise  commit  insufficient
                                                   |      |
                                                   +------+
                                                          v
                                                    Judgment Commit
                                      /              \
                                Output             Verification
                                                       |
                                                       v
                                                Offline Learning
~~~

### 2.3 Agent 形态

V4 采用“一个分析师内核、多套 profile、少量临时专项 agent”的方案：

- market、sector、stock、rhythm 是 profile，不是四套完全独立系统。
- event、capital、fundamental、wind、trend 默认是工具或 skill。
- 只有存在多来源冲突、检索路径不确定或需要隔离上下文时，控制器才创建受限专项子 agent。
- lead、alternative、timeline、mechanism、quant、risk 是动态议事席位；其中 risk 只做审查，所有席位都不能直接落账。
- 所有 profile 共用 ResearchController、EvidenceLedger、RevisionController 和验证闭环。

这比“所有能力包都是 agent”更便宜、更稳定，也比“一个巨大 agent 拿全部工具”更容易审计和隔离上下文。

### 2.4 V4.1 案卷制分析师议会层

V4.1 在 `SufficiencyGate` 与 `JudgmentCommit` 之间增加正式的 `DeliberationLayer`。研究循环负责“找到足够的证据”，议事循环负责“在多个合理解释之间形成可审计决定”，两者不能合成一个自由对话节点。

~~~text
ResearchController
  -> Hypothesis / Evidence / Gap 循环
  -> SufficiencyGate
  -> CollaborationRouter
       |- SOLO_DEEP
       |- LEAD_WITH_CONSULTANTS
       |- ANALYST_COUNCIL
       `- MANDATORY_RED_TEAM
  -> DeliberationLayer
  -> RevisionController（需要时退回局部研究）
  -> DecisionGate
  -> JudgmentCommit
~~~

正式议事遵循“案卷公开、草稿隔离、产物协作、唯一提交”：

- 全体席位读取同一 `CaseFile`、`as_of`、`snapshot_id` 和冻结证据投影；
- 每个分析师拥有独立私有上下文，不读取其他人的 scratch 或原始推理；
- 首轮协作只交换 `PositionPaper / Objection / ResearchGap`；第二轮开始可交换经过控制器裁剪的 `PeerBrief`；
- 分析师读取同伴观点后必须提交 `BeliefUpdate`，明确保持、增强、削弱、切换、拆分或合并了什么；
- 同伴观点可以改变检索方向、假设权重和议题优先级，但不能直接充当 evidence；
- 所有正式修改先形成 `RevisionProposal`，再由唯一 `RevisionCommitter` 写入规范状态；
- Judge 只提交 `CouncilDecision`，最终 judgment 仍由确定性 `JudgmentWriter` 校验和落账。

### 2.5 四种协作模式与路由

| 模式 | 触发条件 | 组织形式 | 正式逻辑 owner |
|---|---|---|---|
| `SOLO_DEEP` | 问题窄、证据方向一致、因果链耦合强 | 一个主分析师持续维护完整假设图；专家只供证据 | Lead Analyst |
| `LEAD_WITH_CONSULTANTS` | 主链基本成立，但存在一至两个专业缺口 | Lead 定向派发咨询，收到 ClaimPacket 后自行修订 | Lead Analyst |
| `ANALYST_COUNCIL` | 至少两个高材料性竞争假设接近，或市场/板块/个股结论冲突 | 独立立论、正式异议、补证、答辩、裁决 | Council Judge + Revision Committer |
| `MANDATORY_RED_TEAM` | 高影响、高置信、历史上易错，或准备对外发布 | 红队只寻找击穿条件和替代解释，不重做全量研究 | 原 Lead；红队无写权 |

`CollaborationRouter` 使用确定性特征选择模式：`materiality`、`hypothesis_entropy`、`evidence_conflict_score`、`cross_layer_conflict`、`novelty`、`revision_count`、`source_quality`、剩余预算和 deadline。模型可以建议模式，但不能覆盖硬规则。

默认路由：

~~~text
低冲突 + 窄问题                         -> SOLO_DEEP
明确专业缺口                            -> LEAD_WITH_CONSULTANTS
两个以上竞争解释接近 / 跨层冲突          -> ANALYST_COUNCIL
高影响且准备提交 / 高置信但反证覆盖不足    -> MANDATORY_RED_TEAM
重大未解冲突 / 数据泄漏风险 / 合规边界     -> HUMAN_ESCALATION
~~~

### 2.6 多轮议事精进状态机

~~~text
CASE_FREEZE
  -> AGENDA_BUILD
  -> R0_INDEPENDENT_POSITIONS
  -> FACT_NORMALIZE
  -> PEER_BRIEF_REVEAL
  -> CROSS_EXAM_AND_OBJECTIONS
  -> BELIEF_UPDATE
  -> CONVERGENCE_GATE
       |- MATERIAL_GAP -> TARGETED_RESEARCH -> CASE_VERSION++ -> PEER_BRIEF_REVEAL
       |- MATERIAL_BELIEF_CHANGE -> NEXT_DELIBERATION_ROUND -> PEER_BRIEF_REVEAL
       |- STABLE -> COUNCIL_JUDGE -> DECISION_GATE
       |- STALL / BUDGET -> INSUFFICIENT
  DECISION_GATE
       |- COMMIT
       |- LOCAL_REVISE -> CASE_VERSION++ -> PEER_BRIEF_REVEAL
       |- INSUFFICIENT
       `- HUMAN_ESCALATION
~~~

阶段规则：

1. `CASE_FREEZE`：固定问题、时点、horizon、事实、预算和裁决标准。
2. `AGENDA_BUILD`：每个议题只能包含一个可裁决问题，不允许“讨论一下市场”这类开放议题。
3. `R0_INDEPENDENT_POSITIONS`：分析师在看到同伴立场前独立提交初始判断，降低锚定和从众。
4. `FACT_NORMALIZE`：证据去重、时间校验、来源冲突和 future-data 检查由规则节点完成。
5. `PEER_BRIEF_REVEAL`：控制器为每个席位生成最小必要同伴摘要，只包含观点、证据引用、分歧点和开放问题，不包含原始推理。
6. `CROSS_EXAM_AND_OBJECTIONS`：分析师可以让同伴观点影响自己的下一轮判断；异议必须指向具体 ID，并声明击穿条件或所需证据。
7. `BELIEF_UPDATE`：每个席位声明本轮观点变化、变化来源、置信变化和仍需补查的 Gap；不能静默改口。
8. `CONVERGENCE_GATE`：计算假设排序变化、最大信念变化、高严重度异议和新增证据，决定继续研究、继续讨论或进入裁决。
9. `TARGETED_RESEARCH`：只解决高材料性 Gap；新证据使 `case_version` 增加，仅重新唤醒受影响席位。
10. `COUNCIL_JUDGE`：比较最终解释力和观点演化过程，不创造新事实；新缺口一律退回。
11. `DECISION_GATE`：允许保留少数意见、主因未定和 evidence insufficient。

多轮不是把同一问题反复改写。每一轮必须至少发生一种有效变化：新增有效 evidence、关闭高严重度异议、假设拓扑变化、假设排序变化或明确降低不确定性。否则该轮记为 `no_progress`。

### 2.7 议会席位

| 席位 | 责任 | 禁止事项 |
|---|---|---|
| Chair / ResearchController | 冻结案卷、建议程、派席位、控制轮次和预算 | 不提出业务立场，不投票 |
| Lead Thesis Analyst | 维护当前最完整的主因链和失效条件 | 不隐藏不利证据 |
| Alternative Hypothesis Analyst | 提出真正不同的解释和可区分证据 | 不能只做措辞相反的“反方” |
| Evidence & Timeline Auditor | 检查 provenance、三时间、重复、未来数据和因果先后 | 不评价价格方向 |
| Mechanism Analyst | 检查中间变量、暴露对象和传导路径 | 不把相关性直接写成因果 |
| Magnitude / Quant Auditor | 检查影响范围、量级、基准和反事实 | 不让 LLM 代替确定性计算 |
| Risk / Red Team | 寻找击穿条件、遗漏驱动和模型失效状态 | 不直接改主假设 |
| Council Judge | 按量表裁决、保留少数意见和未解问题 | 不新增 evidence，不落库 |
| Revision Committer | 对通过的 RevisionProposal 做影响分析并写入规范图 | 不改变提案语义 |

席位按议题动态选择，不要求每次全员出席。市场溯源通常启用 Lead、Alternative、Timeline、Mechanism、Quant 和 Judge；个股异动必须启用 Timeline；节奏大师在快路径不启议会，在深路径冲突时启用 Mechanism、Quant、Risk 和 Judge。

### 2.8 公共状态、私有状态与单写规则

父图只保存公共、可回放的规范状态：

~~~text
CaseFile / AgendaItem[] / EvidenceLedger
HypothesisGraph / CausalGraph
PositionPaper[] / Objection[] / Rebuttal[]
PeerBrief[] / BeliefUpdate[] / InfluenceEdge[]
RevisionProposal[] / CouncilDecision / ProgressLedger
~~~

每个分析师子图保存私有状态：`messages`、scratch、临时检索计划、工具轨迹和上下文摘要。私有状态不得复制到共享 state，也不存储原始 chain-of-thought；只归档结构化理由、引用和决策。

并行节点只允许追加以下 reducer 通道：

- `submissions[]`：按 `submission_id` 去重并稳定排序；
- `objections[]`：按 `objection_id` 去重并稳定排序；
- `belief_updates[]`：按 `belief_update_id` 追加，禁止覆盖上一轮观点；
- `influence_edges[]`：记录哪个同伴产物影响了哪个观点变化；
- `usage[]`：成本、token、时间相加；
- `errors[]`：分类追加，不覆盖成功结果。

以下字段禁止并行写：`HypothesisGraph`、`CausalGraph`、`CouncilDecision`、`JudgmentDraft`。它们分别由 `RevisionCommitter`、`CouncilJudge` 和 `JudgmentWriter` 单写。相同 ID 出现不同内容时必须生成 conflict，不能采用“最后到达者覆盖”。

### 2.9 议事终止与计划重置

议事默认包含 R0 独立立论和一轮观点交换；只有新直接证据、未闭合高严重度异议或材料性观点变化，才允许进入下一轮。终止条件由代码组合判断：

- 所有 `severity=high` 的异议已关闭或转为明确未解问题；
- 主假设连续两次 DecisionGate 排名不变；
- 所有活跃席位的最大 `belief_delta` 低于 profile 阈值；
- 关键 Gap 已闭合，新增证据不再改变结论；
- 连续两轮没有有效新增，`stall_count >= 2`；
- 达到轮次、token、工具、时间或费用预算；
- 同一假设在 A/B 之间振荡，标记 `unstable`；
- deadline 到达，按现有证据产出 partial 或 insufficient。

`ProgressLedger` 每步记录：research_round、deliberation_round、case_version、议题是否满足、是否有进展、未解高严重度异议、最大 belief delta、排名是否改变、下一席位、下一指令、stall、plan reset 和预算。连续停滞时允许重建议程一次；重置后仍停滞则停止，不得无限换角色讨论。

### 2.10 三层在线循环与一层离线循环

| 循环 | 目的 | 谁控制 | 典型上限 |
|---|---|---|---:|
| Analyst Inner Loop | 单个分析师在授权工具内拆题、取证、形成结构化产物 | analyst harness + step budget | 2-6 steps |
| Research Refinement Loop | 围绕结构化 Gap 补证、修改假设和局部回改 | ResearchController | 1-5 rounds |
| Deliberation Refinement Loop | 让分析师观点相互影响、交叉质询并收敛 | CollaborationRouter + ConvergenceGate | 0-3 rounds |
| Verification Learning Loop | 用真实结果改进方法、路由和席位选择 | 离线 Learning / Eval | 跨 run |

三个在线循环使用独立计数器和预算，禁止用一个 `max_rounds` 同时控制。盘中快路径可以完全跳过后两层；深度研究可以运行多轮研究和议事，但任何一层触发硬停止都会向外层返回 partial 或 insufficient。

### 2.11 产品功能重构边界

V4 不把现有产品页面删除，也不把每个产品功能继续包装成独立 Agent。每个功能必须被拆成以下五种角色之一：

| 角色 | 职责 | 现有功能举例 |
|---|---|---|
| Product Entry | 用户入口、交互和展示，不自行拼研究结论 | 首页、个股页、市场页、事件页、节奏页、App、Web |
| Research Profile | 规定研究目标、必答问题、截止时点、预算和输出契约 | 早报、午报、晚报、市场溯源、个股溯源、节奏深研 |
| Skill / Tool | 取数、计算、检索、实体链接和局部专业判断 | quote、trend、wind、rotation、forecast、institution、MCRE |
| Renderer / Notifier | 把已落账 judgment 投影成页面、文字、音频或推送 | 播报、音频、卡片、SSE、push |
| Verification / Learning | 验证、错误分类、回放和方法候选 | prediction、review、snapshot、iterate |

产品 owner 可以修改自己的 Profile、输出投影和评测集，但不能自建另一套行情抓取、事件身份、正式 judgment 或方法发布流程。公共层修改必须经过对应 owner 和跨层契约 review。

### 2.12 早午晚报是三种 Profile，不是一个 slot

三种报告共享 Evidence Fabric、ResearchController、Judgment Ledger 和 Renderer，但研究目标、数据截止时间、必答问题、协作强度和正文 schema 不同。

| Profile | 核心问题 | 数据截止与依赖 | 研究方式 | 主要输出 |
|---|---|---|---|---|
| `briefing_morning` | 今天开盘前应如何建立基线预期 | 只读盘前 cutoff 前可见信息，不得读取当日未来行情 | 轻量研究；主链不清时咨询，重大冲突才开轻量议事 | 隔夜变化、今日基线、关键事件、风格与板块假设、三情景、失效条件 |
| `briefing_midday` | 上午实际走势验证或推翻了哪些盘前判断 | 读取 morning judgment，加上午盘增量快照 | 增量复核；只补查发生变化的假设，默认不重跑全量 | 已确认、已证伪、新驱动、下午机会/风险、相对早报的 relation |
| `briefing_evening` | 全天为何这样走，哪些解释成立，明天要验证什么 | 读取 morning、midday 和完整收盘快照 | 深度复盘；直接复用 `market_trace`，可运行多轮研究与议事 | 收盘归因、六因子链、预测对账、错误与未解问题、次日验证清单 |

三者形成日内版本链：

~~~text
briefing_morning judgment
  -> briefing_midday judgment  (confirm | weaken | revise | reverse)
  -> briefing_evening judgment  (explain | verify | revise | carry_forward)
~~~

每次运行有独立 `run_id / snapshot_id / as_of / data_cutoff / ledger_namespace`。午报和晚报只能新增版本并声明与父判断的关系，不能覆盖早报。现有“收盘播报”不再单独研究，它与晚报共用 `briefing_evening` judgment，只是不同发布时间和 renderer channel。

输出采用“公共信封 + Profile 专属正文”的判别联合，而不是三套互不兼容文本：

~~~text
BriefingEnvelope
  briefing_id / trading_date / profile / as_of / data_cutoff
  run_id / parent_run_id / parent_judgment_ids[] / relation
  headline / summary / evidence_ids[] / risk_flags[]
  body: MorningBody | MiddayBody | EveningBody
  render_targets[] / schema_version
~~~

Renderer 只能压缩、扩写和换渠道，不能新增因果结论。App 和 Web 可以保留不同组件，但必须消费同一信封和相同 judgment_id。

## 3. 研究状态与契约

### 3.1 ResearchRequest

~~~text
request_id / run_id / profile
subject_type / subject_code
research_goal / required_questions
horizon / as_of / snapshot_id
budget_policy / stop_policy
~~~

### 3.2 Hypothesis

~~~text
hypothesis_id / version / statement
status: open | supported | weakened | rejected | frozen
prior_confidence / assumptions[] / disconfirmers[]
required_evidence_types[] / parent_ids[] / scope
~~~

假设修改必须创建新版本。例如：

~~~text
hyp_liquidity:v1  资金收缩是本轮下跌的结构根因
hyp_liquidity:v2  资金收缩是风险偏好切换后的放大器
~~~

旧版本不能被覆盖，因为系统需要知道“什么时候、因为什么证据改变了结论”。

### 3.3 EvidenceLedger

每条证据必须记录：

- evidence_id、source、source_type；
- event_time、published_at、fetched_at、as_of；
- 直接证据、间接证据或模型推断；
- 支持和反驳的 hypothesis IDs；
- 数据质量、时间置信度、重复和冲突关系；
- 工具、参数、响应摘要和 snapshot_id。

### 3.4 CanonicalEventRecord

“事件统一为一个逻辑主记录”是指：系统先识别现实世界中的同一件事，再把新闻、公告、研报、财经日历和抓取结果作为该事件的来源或观察记录挂上去。它不是把每篇文章都当成一个事件，也不要求第一阶段立即把三套物理表合并。

~~~text
event_id / canonical_type / canonical_title
event_time / first_seen_at / last_updated_at / valid_time
status: scheduled | active | occurred | corrected | cancelled | expired
source_refs[] / observation_ids[] / entity_refs[]
dedupe_fingerprint / parent_event_id / supersedes_event_id
time_confidence / source_conflicts[] / lifecycle_version
~~~

逻辑关系如下：

~~~text
CanonicalEventRecord（一件现实事件）
  |- EventObservation（新闻 A）
  |- EventObservation（公告 B）
  |- EventObservation（研报 C）
  `- EventObservation（日历中的计划时间）
~~~

它带来六个直接收益：

1. 同一事件只分配一个 `event_id`，首页、事件页、板块、个股和播报引用同一对象。
2. 可区分“事件发生时间”和“消息发布时间”，避免用晚到新闻解释早先价格。
3. 更正、取消和后续进展只增加 lifecycle version，相关研究能定向失效或复查。
4. 事件热度、存活度、实体链接和传导起点只计算一次，各功能不再各算一套。
5. 多来源冲突被显式保留，分析师能看到“公告与媒体说法不一致”，而不是随机选一条。
6. 三套旧库可以先由 Adapter 映射到统一契约，再逐表迁移，降低一次性重构风险。

`CanonicalEventRecord` 只保存事实身份、时间、状态和来源，不保存“利好某板块”这类分析结论。影响方向、传导链和存活度判断分别进入 Evidence、Claim 和 Judgment，防止事件事实与分析意见混在一条记录里。

### 3.5 CausalGraph

每条因果边必须包含：

~~~text
edge_id / from_node / to_node / mechanism
supporting_evidence_ids[] / contradicting_evidence_ids[]
edge_type: fact | inference | hypothesis
confidence / valid_time
magnitude_check / counterfactual_check
~~~

没有 evidence 的边可以保留为待验证假设，但不能进入高置信主因权重。

### 3.6 RoundLedger

每轮结束记录：

~~~text
round_no / new_evidence_ids[] / changed_hypothesis_ids[]
unresolved_gaps[] / contradictions[] / next_action
progress_score / stall_count / revision_budget_left / stop_reason
~~~

它是机器可执行的研究草稿，防止上一轮思考被下一轮覆盖。

### 3.7 CaseFile

`CaseFile` 是议事期间唯一案卷，所有席位读取同一版本：

~~~text
case_id / run_id / profile / case_version
question / decision_type / materiality
subject / horizon / as_of / snapshot_id
frozen_fact_ids[] / hypothesis_refs[] / open_issue_ids[]
deadline / budget_policy / decision_rubric_version
~~~

案卷变更必须增加 `case_version`。独立立论阶段结束前不得向某一席位提前暴露其他席位观点。

### 3.8 AgendaItem

~~~text
issue_id / case_id / question
candidate_hypothesis_refs[] / decision_criteria[]
materiality / owner_role / status
opened_at / resolved_at / unresolved_reason
~~~

`status` 只能是 `open | researching | deliberating | resolved | unresolved | escalated`。

### 3.9 PositionPaper

~~~text
position_id / issue_id / analyst_role / hypothesis_ref
thesis / mechanism_edge_ids[] / evidence_ids[]
assumptions[] / confidence / disconfirmers[]
unknowns[] / requested_checks[] / created_at
~~~

独立立论必须至少包含一个可推翻条件；没有 evidence 引用的立场只能作为新 hypothesis proposal，不能进入裁决评分。

### 3.10 Objection 与 Rebuttal

~~~text
Objection:
  objection_id / issue_id / raised_by
  target_type: position | claim | edge | evidence
  target_id / objection_type / severity
  argument / required_evidence[] / kill_criterion
  status: open | answered | accepted | unresolved

Rebuttal:
  rebuttal_id / objection_id / answered_by
  response: accept | partially_accept | reject_with_evidence | request_more_evidence
  evidence_ids[] / revision_proposal_id / residual_uncertainty
~~~

`objection_type` 固定为 `chronology | provenance | mechanism | magnitude | alternative | leakage | scope`。自由文本只解释异议，不承担路由语义。

### 3.11 RevisionProposal

~~~text
proposal_id / issue_id / proposer
target_refs[] / action
reason / triggering_evidence_ids[]
expected_impact_refs[] / status
~~~

`action` 与正式回改动作一致：`add_evidence | reweight | revise | split | merge | reject`。只有 `RevisionCommitter` 可把 accepted proposal 转成 `RevisionEvent`。

### 3.12 CouncilDecision

~~~text
decision_id / issue_id / rubric_version
selected_hypothesis_refs[] / rejected_hypothesis_refs[]
criterion_scores[] / reasoning_summary
resolved_objection_ids[] / unresolved_objection_ids[]
minority_opinions[] / invalidators[]
decision: accept | revise | insufficient | escalate
confidence / created_at
~~~

裁决采用逐项量表，不做简单多数投票。默认维度是证据质量、时间一致性、机制完整性、量级匹配、替代解释覆盖、可证伪性和跨层一致性；不同 profile 只能通过版本化 rubric 调权。

### 3.13 ProgressLedger

~~~text
step_no / phase / agenda_status
has_progress / new_evidence_count / changed_ranking
open_high_objection_ids[] / next_role / next_instruction
stall_count / plan_reset_count / budget_remaining
stop_reason
~~~

`ProgressLedger` 是 run 级编排摘要，串联当前处于研究、议事、裁决还是回改阶段；研究轮细节写 `RoundLedger`，议事轮细节写 `DeliberationRoundLedger`。三者都 append-only，不能互相覆盖。

### 3.14 CollaborationDecision

~~~text
mode / trigger_codes[] / selected_roles[]
lead_role / judge_role / max_objection_rounds
max_rebuttal_rounds / human_required
~~~

相同输入特征和配置版本必须得到相同模式。任何模型建议都写入 `suggested_mode`，确定性路由结果写入 `mode`，便于离线比较。

### 3.15 PeerBrief

~~~text
peer_brief_id / case_id / case_version / deliberation_round
target_role / source_artifact_ids[]
competing_claims[] / evidence_ids[]
material_disagreements[] / open_questions[]
omitted_sections[] / generated_at
~~~

`PeerBrief` 由控制器根据席位职责裁剪，不是把所有分析师全文互相广播。首轮独立立论结束前禁止生成；后续轮次只包含与当前议题和目标席位相关的公开产物。

### 3.16 BeliefUpdate

~~~text
belief_update_id / issue_id / analyst_role
case_version / deliberation_round
prior_hypothesis_ref / prior_confidence
action: hold | strengthen | weaken | switch | split | merge
posterior_hypothesis_refs[] / posterior_confidence[]
influencing_artifact_ids[] / evidence_ids[]
changed_assumptions[] / requested_gap_ids[] / reason_summary
~~~

每个读取 `PeerBrief` 的席位都必须提交 `BeliefUpdate`，即使选择 `hold`。`strengthen` 不能只引用同伴观点；若没有新增 evidence 或机制修正，置信度不得上升，只能记录“论证得到同伴支持但证据未增强”。

### 3.17 InfluenceEdge

~~~text
influence_edge_id / source_artifact_id
target_belief_update_id / target_hypothesis_ref
influence_type: search_direction | assumption | mechanism
                | confidence | hypothesis_topology | objection
effect: reinforce | weaken | redirect | split | merge | reject
evidence_backed / materiality / created_at
~~~

`InfluenceEdge` 让系统回答“谁的哪一个想法改变了谁的哪一条结论”。它不代表 source artifact 是事实证据；`evidence_backed=false` 的影响只能改变研究计划或提出假设，不能增加正式证据分。

### 3.18 DeliberationRoundLedger

~~~text
deliberation_round / case_version / active_issue_ids[]
peer_brief_ids[] / belief_update_ids[] / influence_edge_ids[]
new_evidence_count / closed_high_objection_ids[]
max_belief_delta / ranking_changed / topology_changed
affected_role_ids[] / next_action / stop_reason
~~~

案卷因补证升级版本后，只有 `affected_role_ids` 中的席位重跑；其他席位沿用上一轮立场并标注所基于的 `case_version`，避免每次全员重算。

## 4. 溯源深度门禁

### 4.1 六因子链只负责展示

最终仍可展示：

~~~text
结构根因 -> 触发 -> 传导 -> 暴露 -> 重定价 -> 观察结果
~~~

进入主链前必须满足：

1. 至少存在两个竞争假设；
2. 每条主链边有直接证据或明确标记为推断；
3. 至少一个替代解释经过反证比较；
4. 根因和触发在时间上早于结果，且当时可见；
5. 传导边说明中间变量和受影响对象，不能同义改写；
6. 原因影响范围与价格反应做量级检查；
7. 结论带可观测的失效条件；
8. 未达门禁时输出 evidence insufficient 或主因未定。

### 4.2 深度等级

| 等级 | 含义 | 能否作为主因 |
|---|---|---:|
| D0 表面 | 只有现象和叙述 | 否 |
| D1 有证据 | 有直接证据，但机制不完整 | 低置信备选 |
| D2 有机制 | 说明中间变量、暴露对象和重定价 | 可以 |
| D3 有替代解释 | 比较并处理竞争假设 | 优先 |
| D4 可验证闭环 | 有失效条件、验证时间和结果回填 | 最优 |

DepthScore 由确定性规则计算：证据覆盖、机制具体性、反方排除、时间一致性、量级一致性、反事实检查、跨层验证，减去未证实边和重复证据惩罚。模型自评不能直接通过门禁。

## 5. 后验发现前面不对时如何回改

### 5.1 回改流程

~~~text
COLLECT -> BUILD CLAIMS -> CHECK LOGIC
                              |
                         contradiction
                              v
                       IMPACT ANALYSIS
                              |
          解锁受影响的上游 hypothesis
          保留未受影响的 evidence 和 claims
          生成定向 ResearchRequest
          补证或重建局部因果边
          重新计算受影响的后代 claims
~~~

### 5.2 允许动作

| 动作 | 用途 |
|---|---|
| add_evidence | 针对结构化 Gap 补证 |
| reweight | 调整证据或先验权重 |
| revise_hypothesis | 修改假设并创建新版本 |
| split_hypothesis | 把混合故事拆成竞争解释 |
| merge_hypothesis | 合并重复假设 |
| reject_hypothesis | 反证击穿或无法支持 |

### 5.3 边界

- 单假设最多 revision 2 次，单 run 最多 4 次。
- 同一工具和相同参数不能重复调用，除非 as_of 或请求原因变化。
- 连续两轮无新增有效 evidence，记为 stall 并停止。
- 假设在 A 和 B 之间振荡，标记 unstable，保留分歧并降置信。
- 已冻结的高置信节点只有新的直接反证才能解锁。
- RevisionEvent 必须记录“哪条新证据使哪条旧结论失效”。

## 6. 功能示例与首批试点

### 6.1 早午晚报日内链

早午晚报是首批试点中的一条连续链，但不是一个 run 从早挂到晚。每个时点创建独立 ResearchRun，通过父子关系串联，并严格冻结自己的数据截止时间。

**早报：建立可被验证的盘前基线。**

- 必答：隔夜发生了什么、今日主要变量、市场基线、关键事件日历、可能受益/受损方向、三种情景和失效条件。
- 禁止：读取 cutoff 后行情；把“可能发生”写成“已经发生”；为了内容丰富而全量重跑所有专项能力。
- 协作：默认 `LEAD_WITH_CONSULTANTS`；只有宏观、政策、全球风险和资金信号形成材料性冲突时才开轻量议事。

**午报：用上午事实检验早报，而不是重写一篇新晨报。**

- 输入：早报 judgment、上午增量行情、板块轮动、事件和自选异动。
- 必答：哪些盘前假设已确认、哪些被削弱或证伪、上午新增驱动是什么、下午最重要的机会与风险是什么。
- 研究：只重开受影响假设；不变部分直接引用早报 evidence 和 claim。deadline 前仍有冲突时允许 partial，不为了完整强行开大会。
- 输出：每条结论必须带 `parent_judgment_id` 和 `relation`，页面优先展示“相较早报变化了什么”。

**晚报：把完整收盘事实送入市场溯源，并对日内判断做对账。**

- 输入：早报、午报、完整收盘快照、全天事件时间线和历史相似日。
- 必答：市场为什么这样走，主因/触发/放大器分别是什么，早午报哪里正确或错误，哪些解释被否，明天验证什么。
- 研究：复用 `market_trace` 的竞争假设、Gap、回改和议会，不另建一套“晚报归因 Agent”。
- 输出：收盘归因、六因子投影、预测对账、未解问题、次日观察项。现有收盘音频与晚报文字只是同一 judgment 的两个 renderer。

因此，V4 同时解决两个现状问题：三种内容不再被错误地塞进同一模板，三种内容也不再各自维护互不相认的分析逻辑。

### 6.2 市场溯源

第一轮生成全球风险、国内政策、产业供需、仓位流动性等候选。工具并行取得指数分化、涨跌广度、成交额、行业风格、资金流、全球市场、事件时间线和历史相似日。

默认先走 `LEAD_WITH_CONSULTANTS`；当两个根因分数接近、资金与风格信号相反，或市场/板块层结论不一致时升级为 `ANALYST_COUNCIL`。议程按“结构根因”“触发器”“放大器”拆开裁决，避免不同层次的原因争夺同一个主因位置。

如果初始假设认为“流动性收缩是根因”，但证据显示防御板块上涨、成长板块下跌，V4 会认为流动性可能只是放大器：

1. 创建 hyp_liquidity:v2；
2. 把 root cause 改成 amplifier；
3. 解锁风险偏好切换假设；
4. 补查利率、全球风险资产、期指和 ETF 资金、高 beta 相对表现；
5. 只重建受影响的根因和传导边。

最终输出主因、备选、被否解释、深度等级、证据、失效条件和 revision history。它不会为了填满六个节点而制造因果链。

### 6.3 个股异动溯源

V4 先计算个股相对市场、板块和同业的偏离，再并行检查事件、资金、成交量、龙虎榜、趋势位置、基本面和产业链。

普通异动使用 `SOLO_DEEP`，由个股 Lead 保持时间线和逻辑连续性；出现“价格先动、消息后到”“公告与板块行为冲突”“资金证据与基本面解释冲突”时升级议会，并强制加入 Evidence & Timeline Auditor。

如果新闻发布时间晚于价格启动时间：

- 新闻不能成为最初触发；
- 它可以成为后续强化或市场解释；
- 系统回查启动前可见的公告、资金和同板块异动；
- 找不到时明确输出“启动前触发尚未证实”。

出口分层展示主因、放大器、背景和未解问题。例如：

~~~text
主因：个股独立事件 / D3
放大器：资金集中 / D2
背景：板块偏弱 / D1
未解：启动前触发尚未确认
失效条件：公告澄清、资金转负、相对强度跌破阈值
~~~

### 6.4 节奏大师

节奏大师分成两条路径：

- alert_hot 快路径：只用确定性指标计算广度、波动、量能、风格、趋势和涨跌停结构，不启动深度循环。
- rhythm deep 路径：识别趋势扩散、抱团、退潮、震荡、风险释放，对照历史 verified case，生成基准、改善、恶化情景。

`alert_hot` 永不启动议会。deep 路径只有在趋势、广度、情绪、量能或风格至少两类关键状态冲突时进入 `ANALYST_COUNCIL`；高置信进攻或防守建议在提交前必须经过 `MANDATORY_RED_TEAM`。

如果恐贪值乐观，但市场广度恶化且成交集中于少数权重股，V4 会显式记录冲突，降低置信度，并把“广度恢复”设为改善条件，而不是简单给出积极进攻结论。

## 7. 为什么 V4 更像资深分析师

| 资深分析师行为 | V4 机制 |
|---|---|
| 先定义问题、时点和周期 | ResearchRequest 和 profile |
| 同时保留多个解释 | Hypothesis set |
| 优先查能区分假设的证据 | ResearchPlan 和 information-gain 排序 |
| 区分事实、推断和意见 | Evidence / CausalEdge 类型 |
| 发现不一致时回到假设层 | ImpactAnalysis + RevisionController |
| 不用晚到信息解释早期行为 | as_of 和三时间校验 |
| 明确什么情况证明自己错 | invalidators 和 VerificationRecord |
| 复盘结果后修改方法 | ErrorTags、MethodCandidate、回放 A/B |
| 知道什么时候停止 | sufficient、budget、stall、unstable |
| 承认证据不足 | insufficient 是合法结果 |
| 先独立判断再听同伴观点 | Independent Positions |
| 听取同伴后明确说明自己改变了什么 | PeerBrief + BeliefUpdate |
| 能追踪一条想法如何改变另一条结论 | InfluenceEdge |
| 反驳必须指向具体逻辑 | Objection target ID |
| 保留团队中的少数意见 | Minority Opinion |
| 讨论后由唯一负责人改稿 | Revision Committer 单写 |

V4 的智能来自这些可执行约束，不来自更长的 prompt 或更多自由聊天 agent。

## 8. LangChain 与 LangGraph 落位

| 需求 | 采用方式 |
|---|---|
| 有限研究循环 | LangGraph StateGraph 条件边 |
| 多假设并行取证 | Send 动态 fan-out |
| 局部回改和路由 | Command 返回状态更新和下一节点 |
| 分析师私有上下文 | 独立 subgraph state；父图只接收结构化产物 |
| 并行立论和异议 | Send fan-out + append-only reducer channel |
| 同伴观点定向披露 | 控制器生成 role-scoped PeerBrief |
| 观点变化和影响追踪 | BeliefUpdate + InfluenceEdge + DeliberationRoundLedger |
| 议事收敛或继续 | ConvergenceGate 条件边 |
| 正式图单写 | Revision Committer / Council Judge / Judgment Writer |
| 议事恢复 | 父图 checkpointer；副作用工具 task 化并使用幂等键 |
| 长任务恢复 | checkpointer |
| 人工确认 | 仅必要场景使用 interrupt |
| 分析师 | LangChain create_agent + structured response |
| 权限、预算、追踪 | model/tool middleware |
| 计划、Gap、Revision、Judgment | Pydantic structured output |
| 数字、阈值、门禁、统计 | 普通 Python 节点 |
| 离线反思 | 独立 LangGraph 或 batch，不进入盘中主链 |

V4 禁止使用 create_react_agent。旧 worker 只能经带删除日期的 legacy adapter 进入影子链路。

## 9. 时间档位与资源隔离

| Profile | 场景 | Research rounds | Deliberation rounds | 主要约束 |
|---|---|---:|---:|---|
| alert_hot | 盘中异动提醒 | 0-1 | 0 | 秒级；不读案例记忆，不跑深循环 |
| briefing_morning | 盘前基线与早报 | 1-2 | 0-1 | 硬死线；只读盘前 cutoff 前证据；必须输出情景和失效条件 |
| briefing_midday | 上午验证与午报 | 1-2 | 0-1 | 只做增量研究；必须读取 morning 父判断并输出 relation |
| briefing_evening | 收盘归因与晚报 | 2-4 | 1-2 | 复用 market_trace；对早午判断做对账；可读完整当日快照 |
| market_trace | 市场深度溯源 | 2-5 | 1-3 | 竞争假设、Gap、局部回改、六因子投影 |
| intraday | 其他盘中复核 | 1-2 | 0-1 | 分钟级；只允许咨询或轻量红队 |
| on_demand_deep | 用户深度分析 | 2-5 | 1-3 | 可读方法版本和完整案例投影 |
| verify | 到期验证 | 0 | 0 | 只读原 run 与后续真实结果 |

每次 run 独立拥有 run_id、snapshot_id、ledger namespace、预算、超时、profile 和 config_version。跨 run 只共享不可变 evidence、版本化方法、权限过滤的历史 judgment 和只读缓存。

## 10. 越用越聪明的闭环

### 10.1 六类记忆

| 类型 | 内容 | 写入规则 |
|---|---|---|
| FACT | 证据、来源和时点 | append-only |
| CLAIM | 假设、论点和因果边 | 版本化 |
| JUDGMENT | 最终研判和失效条件 | 版本化 |
| CASE | 历史状态、判断和真实结果 | 验证后生成 |
| METHOD | 方法、阈值和适用条件 | 离线发布，线上只读 |
| GOV | 模型、prompt、配置、成本和评测 | 全量审计 |

### 10.2 学习流程

~~~text
Judgment -> 到期验证 -> VerificationRecord -> ErrorTags
         -> 离线反思 -> MethodCandidate -> 固定回放集 + 配对 A/B
         -> 置信校准 -> 发布 method_version -> 新 run 读取
~~~

错误标签至少包括 direction_wrong、magnitude_wrong、timing_wrong、cause_wrong、missing_driver、unsupported_edge、data_quality_issue 和 overconfidence。

任何方法只有在同一冻结评测集上提升质量，且成本、时延和降级率不过门槛，才允许晋级。线上不因单个案例自动改 prompt 或阈值。

## 11. 现有功能全量迁移矩阵

### 11.1 产品入口

| 现有功能 | V4 位置 | 保留内容 | 重构动作 |
|---|---|---|---|
| 首页 / 早点听 | Briefing Workspace | 早午晚入口、头条和卡片形态 | 改读 BriefingEnvelope；卡片只展示 judgment 投影，不在前端拼结论 |
| 自选 / 个股详情 | Stock Workspace | 行情、K 线、趋势、洞见、异动组件 | 行情组件继续直读事实；所有分析卡引用同一 stock judgment 和 claim graph |
| AI 对话 | Ledger Reader + ResearchRequest | WebSocket、REST、会话交互 | 先查账本；现有研判不足时才发起限额新研究，并把新结论落账 |
| 市场 / 风口 / 板块 | Market / Sector Workspace | 风口榜、轮动、龙头、板块详情 | 榜单由确定性 Skill 生成；解释、冲突和失效条件来自共享研判 |
| 事件 / 时间线 | Event Workspace | 列表、详情、时间线、传导图 | 所有页面引用 canonical event_id；事件事实和影响判断分层存储 |
| 节奏 / 恐贪 / 业绩 | Rhythm Workspace + Fundamental Projection | 读数、四时段展示、业绩和机构卡 | 恐贪与趋势下沉计算；节奏快慢分流；业绩和机构作为共享证据 |
| App 与 Web | 两个 Presentation Adapter | 两端继续使用 | 不强制合并工程；统一 schema、judgment_id、错误态和灰度开关 |
| 播报 / 推送 | Renderer + Notifier | 音频、文字、推送渠道 | 只读 judgment；收盘音频和晚报共用 evening judgment；禁止新增结论 |
| 选股页 | Quant Candidate + Judgment Projection | 榜单、筛选、信号、推荐 | 排序继续由量化/规则 owner 负责；LLM 只解释、反证和定义失效条件 |

### 11.2 app-api 能力

| 现有模块 | V4 位置 | 重构动作 |
|---|---|---|
| quote / kline / flow / index / global | Evidence Gateway 公共工具 | app-api 继续做数据 owner；统一复权、TTL、时间和 provenance |
| monitor 综合监控 | 拆成 detector、calculator、evidence adapter | 风口、异动、趋势、机构、业绩、新闻不再共用一个杂糅 schema |
| crawler | Document Ingestion | 只负责抓取和正文，不直接生成事件判断；与其他抓取入口统一去重 |
| event-entities / calendar / agent event_store | CanonicalEventRecord + Legacy Adapters | 第一阶段统一逻辑 ID 和契约，之后逐表迁移；日历是 scheduled event 来源 |
| fear-greed / rotation | Deterministic Skills | 公式、版本和输入快照可复现；被午报、晚报、市场溯源和节奏共用 |
| stock-trace / insight | Stock Trace Profile + Projection | 归因进入研究循环；单条洞见 worker 只负责触发和展示，不独立下结论 |
| prediction | Verification Service | 记录预测承诺、到期结果和错误标签，不与选股排序混在一起 |
| stock-selection | Quant Candidate Engine | 继续独立计算候选；候选结果作为 evidence，不允许 LLM 任意改榜 |
| causal / attribution / sector-insight / MCRE | Causal Skills | 路由层临时拼装改为可复用工具，输出 CausalEdge 和 ClaimPacket |
| agent proxy / chat / push / user | Brain V4 Gateway | 从纯反代升级为 ResearchRequest、账本查询、双读和灰度入口 |

### 11.3 旧 Agent、Worker 与调度

| 旧模块 | V4 位置 | 迁移动作 |
|---|---|---|
| morning | `briefing_morning` Profile | 保留盘前触发和领域经验；不再自己完成抓取、分析、报告和播报全链 |
| midday_broadcast / midday report | `briefing_midday` Profile + renderer | 读取 morning 父判断，只做上午增量验证；统一文字和音频信封 |
| evening / close / broadcast | `briefing_evening` Profile + renderer | 晚报负责研究；broadcast 只做文字转音频；close 不是第四套研究逻辑 |
| stock | stock analyst profile | 工具和领域提示拆成 skills；后续阶段迁移 |
| sector / wind_leader | sector profile + wind skill | 算法工具化，解释交给 analyst |
| event | event extraction / conduction skill | 只使用 canonical event_id；传导边进入 CausalGraph |
| alert | alert_hot profile | 保留秒级快路径；材料性异动才升级增量研究 |
| hot_burst | sentiment / resonance skill | 输出证据和共振特征，不再生成独立报告 |
| trend_score | deterministic trend skill | 评分、TOP 榜和阶段计算继续保留，但不作为独立 Agent |
| forecast | fundamental / forecast skill | 数字计算与叙事分离 |
| rhythm_master | rhythm profile + calculator | 快路径规则化，深路径进循环；后续阶段迁移 |
| insight worker | Research trigger + stock projection worker | Redis Stream 继续触发，但实际分析进入统一 stock run |
| general / chat | ledger reader | 新结论创建 ResearchRequest |
| review / snapshot / iterate | verify + replay + method candidate | 文件产物改成结构化记录；禁止线上自动改生产方法 |
| APScheduler 30+ jobs | Trigger Gateway | 现有 cron 可暂时保留，但只创建幂等 ResearchRequest，不再直调 Agent 函数 |

### 11.4 迁移纪律

1. 先建立统一契约和 Adapter，再改功能内部，不做一次性大爆炸迁移。
2. 同一功能在迁移期间允许“现有生产链/V4”双跑，但一个出口只能有一个 active owner；V4 只能作为 shadow owner 或 active owner 二选一。
3. 旧模块若仍被调用，必须标记 `legacy_adapter`、owner、调用方和删除条件。
4. 任何共享工具升级都要回放早报、午报、晚报和市场溯源四组样本，防止不同时间功能被连带破坏。
5. 产品 owner 不拥有公共事实；平台 owner 不替产品 owner 决定领域必答问题和输出优先级。

## 12. Owner 与开发规范

| Owner | 负责 | 禁止越界 |
|---|---|---|
| Contract | schema、版本、生成物 | 不改领域逻辑和数据源 |
| Orchestration | graph、profile、预算、reducer | 不改领域阈值和出口文案 |
| Evidence | 数据接口、snapshot、provenance、memo | 不写研究结论 |
| Domain / Product | 早午晚报、市场、个股、节奏 Profile，输出投影和案例 | 不改主图骨架、公共事实和他人 schema |
| Deliberation / Judgment | collaboration router、议程、异议、裁决、revision、verification | 不实现数据源，不在 Judge 中创造 evidence |
| Learning / Eval | 回放、A/B、校准、方法发布 | 不在线改方法 |
| Output | render、API payload、前端投影 | 不重新研究 |

每个 PR 必须提供：契约变化、确定性单测、正常和降级路径、trace 示例、其他 profile 不受影响的验证、回放或评测样本、成本和时延变化、owner review 和跨层契约 review。

### 12.1 分散 manifest 与生成注册表

每个功能或共享能力在自己的目录维护 `manifest.yaml`，不得要求所有负责人共同编辑一个中央注册文件。manifest 至少声明：`id`、`owner`、`kind`、输入输出契约、依赖 capability、适用 profile、预算、cutoff、降级策略、active/shadow owner、legacy adapter 与删除条件。

CI 汇总所有 manifest，生成禁止手改的 `generated/registry.lock`，并检查重复 ID、缺失依赖、循环依赖、非法 profile、跨层 import、owner 缺失和拓扑指纹变化。主图只读取生成结果，功能负责人不修改公共 builder。

### 12.2 红黄绿改动区域

| 区域 | 典型路径 | 修改规则 |
|---|---|---|
| 红区 | `contracts/`、`graph/state.py`、reducers、主图、ledger、gateway | 平台 owner 主改；跨层评审和完整回放必需 |
| 黄区 | `capabilities/`、公共工具契约、公共 renderer、事件主记录 | 能力 owner 修改；平台 owner 联审，必须验证所有依赖 profile |
| 绿区 | `features/<feature>/`、本功能 projection、fixtures、评测案例 | 功能 owner 自主修改；不得反向 import 红区内部实现或其他 feature |

功能之间禁止直接 import；协作只能经过稳定契约、capability ID、ledger 或只读 projection。CODEOWNERS 由 manifest 的 owner 信息生成或校验，避免两处手工维护。

### 12.3 组长与功能负责人的落地边界

组长先交付公共地基和两个样板切片，不替各功能负责人迁移全部领域逻辑：

1. 固定 V4 contracts、state、reducers、profile、snapshot、ledger、registry、ResearchController、主图、议会、影子归档和评测信封。
2. 打通市场溯源纵向切片，证明同快照研究、回改、影子归档和静态 HTML 对比可以端到端运行。
3. 打通早午晚报日内链，证明多时点隔离、父子判断和市场溯源复用成立。
4. 发布样板 manifest、adapter、projection、fixtures、测试和 PR 模板。
5. 审核红区和跨层契约变更，维护晋级与回滚门禁。

其他功能负责人只在公共地基稳定后接入自己的垂直切片：

1. 盘点现有入口、调用方、数据源、写入、输出、超时和降级行为。
2. 将旧 worker 拆成 Trigger、Capability、Evidence Adapter、Profile、Renderer 和 Verification，不整块复制。
3. 在 `features/<feature>/` 建立 manifest、profile、adapter、projection、fixtures 和 tests；默认 `active_owner=legacy`、`shadow_owner=brain_v4`。
4. 将旧链和 V4 输出转换为统一评测信封，使用同一冻结快照双跑并生成静态 HTML 对比。
5. 达到本功能样本量、质量、时间泄漏、时延、成本和失败率门禁后，按 1% -> 10% -> 50% -> 100% 灰度。
6. V4 稳定成为 active owner 后，旧链进入只读观察期；满足删除条件后移除旧 cron、路由、写入、adapter 和配置。

功能负责人不得为了接入而修改公共 builder、共享 state 或其他 feature。确需公共能力时先提出 capability/contract 变更，由对应 owner 单独合并后再继续功能 PR。

### 12.4 面向较弱模型的受限执行包

V4 后续实现不得只把整份 Spec 和总 Plan 交给模型自由发挥。每个开发任务必须生成一个独立 Execution Packet，模型一次只执行一个包。执行包必须包含：

~~~text
packet_id / objective / prerequisite_commit
allowed_files[] / forbidden_paths[]
contracts_consumed[] / contracts_produced[]
exact_test_to_write / expected_failure
implementation_steps[] / verification_commands[]
fixtures[] / rollback_command / completion_evidence[]
~~~

强制规则：

1. `allowed_files` 之外出现修改，范围检查直接失败；红区默认禁止，除非执行包由对应平台 owner 签发。
2. 每个包只引入一个可评审行为，先写失败测试，再做最小实现，再运行指定测试和受影响回放。
3. 执行模型不得自行新增公共抽象、修改契约名称、改变目录、扩大依赖或顺手重构；发现计划缺口必须停在提案，不得猜测实现。
4. 每个包完成后输出机器可检查的 evidence：变更文件、测试命令与结果、契约版本、snapshot SHA、未解决问题。
5. 功能接入通过 `scaffold_feature` 生成 manifest、profile、adapter、projection、fixtures 和测试骨架；通过 `verify_change_scope`、registry 校验和 contract test kit 后才允许提交。
6. 公共架构阶段由能力较强的模型和组长完成并冻结；较弱模型主要在绿区执行已签发的功能包，不能直接修改主图和共享 state。

标准执行包模板保存于 `docs/superpowers/plans/brain-v4-task-packet-template.md`。公共层每个 Task 和后续每个 feature 接入都必须先从模板派生具体包，禁止把模板中的说明当成可直接执行任务。

## 13. A/B 协议

- 一次只改变工具集合、prompt、方法版本、循环策略或 profile 中的一项。
- A/B 使用同一 case_id、snapshot_id、as_of、模型版本和录制响应。
- 输出随机化后进行人工盲评。
- 工程指标：P50/P95、token、成本、失败率、insufficient 率、重复工具率。
- 质量指标：主因命中、证据覆盖、深度等级、替代解释覆盖、失效条件可验证率、未支持边比例和过度自信率。
- 协作指标：高严重度异议闭合率、有效异议率、少数意见后验命中率、议事后假设排序变化率、有效 BeliefUpdate 率、证据支持的 InfluenceEdge 比例、Judge 退回补证率、无效讨论率和从众率。
- 路由指标：SOLO / CONSULT / COUNCIL / RED_TEAM 的质量收益、增量成本和误路由率分别统计，禁止用总体均值掩盖某一模式退化。
- 简报链指标：早报时间泄漏率、午报有效变化率、父子 relation 正确率、晚报对账覆盖率、文字/音频/App/Web 跨渠道结论一致率。
- 早报、午报、晚报必须分别出质量、时延和成本报告，禁止用合并均值掩盖某一时段退化。
- 样本少于 20 只能作为 smoke，不得宣布优胜。
- 晋级必须保存 manifest、样本 SHA-256、配置版本和所有失败案例。

### 13.1 通用静态 HTML 影子评测台

第一阶段评测台由 `brain_v4_evals` 生成静态文件，不修改产品 App/Web 前端，不引入新的常驻前端服务。默认输出：

~~~text
brain_v4_evals/reports/
  index.html
  <run-date>/
    <feature-id>.html
    data/<comparison-id>.json
~~~

每个接入功能必须产出统一 `EvaluationEnvelope`，至少包含：

~~~text
feature_id / profile / subject / comparison_id
legacy_run_id / v4_run_id / snapshot_id / snapshot_sha
as_of / data_cutoff / model_version / config_version / method_version
legacy_output / v4_output
evidence_summary / hypotheses / revisions / unresolved_questions
latency_ms / token_usage / cost / quality_metrics / reviewer_score
~~~

通用页面负责日期、功能、案例和版本筛选，以及旧链/V4 双栏、快照一致性、证据、trace、成本、时延和人工评分展示。功能可提供只读 `evaluation_projection` 增加领域视图，但不得修改评测核心或在查看器中重新研究。没有专用 projection 的功能仍必须能用通用 JSON/section renderer 完整查看。

盲评模式随机隐藏旧链/V4 标签；只有评审提交后才能揭示来源。影子报告不得进入普通用户导航、正式 briefing 查询或推送链路。

## 14. 第一阶段试点

试点采用两条业务链并行、一个公共底座：

1. 早午晚报日内链验证多时点隔离、父子判断、增量修正和多渠道渲染。
2. 市场溯源验证竞争假设、深度门禁、多轮补证、分析师议会和局部回改。
3. 晚报直接调用市场溯源，不再另做一套盘后归因；这样可以验证两个试点是否真正复用同一个研究内核。

| 任务 | Owner | 交付物 | 验收重点 |
|---|---|---|---|
| V4-00 契约与状态 | Contract + Orchestration | contracts、state、profile、manifest schema、registry.lock | 现有生产链不受影响；不得引用已删除 brain |
| V4-01 证据与事件网关 | Evidence | snapshot、provenance、memo、CanonicalEventRecord Adapter | as_of 可复现，同 run 去重，同事件统一 ID |
| V4-02 控制器与回改 | Judgment | plan、sufficiency、impact、revision、stop policy | 可以产生 Gap、补证、局部回改、stall 和 insufficient |
| V4-03 案卷制议会 | Deliberation | CollaborationRouter、CaseFile、Objection、BeliefUpdate、CouncilDecision | 同伴观点不冒充 evidence；正式图单写；合成案卷可完整运行 |
| V4-04 LangGraph 主图 | Orchestration | SOLO/CONSULT/COUNCIL/RED_TEAM 路由、预算与降级 | 公共架构端到端测试通过，不依赖具体产品 worker state |
| V4-05 影子归档与评测台 | Output + Learning/Eval | shadow schema、EvaluationEnvelope、paired runner、静态 HTML | 合成结果可双栏查看；影子不污染正式出口；不修改产品前端 |
| V4-06 公共架构验收 | Platform Owners | 冻结合成案例、契约矩阵、执行包与范围检查 | 公共层完整可运行，但此时接口仍允许由两个试点校正 |
| V4-07 市场溯源试点 | Market | 竞争假设、因果图、议会、六因子投影、双栏报告 | 旧 review 结论不进入 evidence；验证深度研究与局部回改 |
| V4-08 早午晚报试点 | Briefing + Market | 三 Profile、parent relation、市场溯源引用、对账 | 多时点隔离；晚报不复制归因；文字和音频同源 |
| V4-09 接口校正与冻结 | Platform Owners | V4.1 contracts、样板 feature、task packets、迁移基线 | 两个试点暴露的问题已关闭；红区进入兼容优先状态 |
| V4-10 配对 A/B 与逐功能灰度 | Learning/Eval + Feature Owners | Verification、盲评、晋级报告、active owner 切换与 legacy 删除 | 小样本不得宣布优胜；一次只晋级一个功能 |

### 14.1 早午晚报日内链门槛

- 至少 10 个完整交易日，三次运行均使用可重放 snapshot。
- 早报不得引用盘中或盘后数据；时间泄漏率必须为 0。
- 午报每条变化都能追溯到早报 judgment 和新增 evidence；未变化部分不得无意义重写。
- 晚报引用市场溯源的 judgment/claim，不复制生成第二套主因；早午判断必须逐条标记 confirmed、weakened、rejected 或 unresolved。
- 首阶段通过同一 `BriefingEnvelope` 的 App/Web/text/audio projection 契约测试证明 headline、relation、evidence_ids 和风险等级一致；准备灰度时再修改产品前端并做端到端展示验收。
- 早午晚分别统计准时率、P95、成本、空结果率和降级率，禁止用日均值掩盖某一时段失效。

### 14.2 市场溯源门槛

至少 10 个历史案例，每例至少 3 个竞争假设；至少 3 个已知浅层或错误案例；证据截面可复现；至少一个案例触发局部 revision；允许 insufficient。

### 14.3 后续阶段：个股异动门槛

覆盖市场共振、板块共振、个股独立和事件滞后；新闻晚于启动时不得作为初始触发；至少引用市场、板块、个股三层中的两层；无法确认时展示未解问题和下一证据计划。

### 14.4 后续阶段：节奏大师门槛

快路径 P95 不受深路径影响；单一恐贪值不能单独决定结论；输出绑定 horizon 和失效条件；盘中与盘后 judgment owner 不冲突。

## 15. 明确不做

- 不把所有模块改成 agent。
- 不把分析师议会实现成自由群聊、投票群或所有角色全量常驻。
- 不因“多个分析师都同意”自动增加置信度；置信提升必须来自新增证据、机制修正或反证关闭。
- 不允许多个分析师直接修改同一个 hypothesis、causal graph 或 judgment state key。
- 不保存或共享原始 chain-of-thought；只保存结构化理由、证据引用和决策。
- 不做无限自主循环或端到端强化学习。
- 不在线自动修改 prompt、阈值和方法版本。
- 不让 LLM 计算可确定的数字。
- 不让 renderer 重新研究。
- 不让一个共享 state 覆盖所有时间档位。
- 不把文字更长当作溯源更深。
- 不用旧链路方向作为新链路正确答案。
- 不保留 `graph/brain` 或创建 `brain_v3` 作为 V4 的兼容层、回滚层或影子层。
- 不在首阶段为影子评测修改产品 App/Web 前端或建设独立常驻后台；先使用可替换的静态 HTML 查看器。

## 16. 配套文件

- 交互图：<code>docs/智慧大脑-V4-交互式架构图.html</code>
- 实施计划：<code>docs/superpowers/plans/2026-11-26-smart-brain-v4-pilot.md</code>
- 受限执行包模板：<code>docs/superpowers/plans/brain-v4-task-packet-template.md</code>
- 团队接入规范：<code>docs/智慧大脑-V4-团队接入与迁移规范.md</code>
- 历史材料（superseded，仅供追溯）：<code>docs/智慧大脑-架构与研判契约.md</code>、<code>docs/智慧大脑-V3-架构改进方案.md</code>

交互图必须覆盖六个视图：全景分层、研究循环、分析师议会、状态与写权限、两条试点链、验证学习与迁移。任何后续协议变更先改本规格，再同步图和实施计划。


## 附录 A：V4.1.1 运行层增补与已知缺口（2026-11-27 实跑登记）

本附录记录试点代码落地后对既有设计的**补充说明**，不改变正文已定架构；所有条目均为加性实现，未修改冻结契约的既有字段。详细经验与处置见 `docs/智慧大脑-V4-团队接入与迁移规范.md` §12–§14。

### A.1 与正文一致的部分

| 正文要求 | 实际落地 |
|---|---|
| SOLO_DEEP 由一个主分析师维护完整假设图 | `graph/builder.py::solo_node` 在注入模型时派发主分析师；无模型时保持确定性离线路径 |
| 所有 run 显式 `as_of` / `snapshot_id` / 版本号 | `runtime/runner.py` 全部由入参传递，不读系统时间 |
| 影子数据只进影子表 | 仅写 `brain_v4_runs` / `brain_v4_comparisons`，`is_shadow` 恒为 true |
| 旧链结果只能进 `legacy_output` | `runtime/inputs.py` 采集后写入 payload 的 `legacy_output`，未进入 V4 evidence |
| 时段独立 run / parent 关系 | `runtime/runner.py` 早/午/晚独立 run_id、cutoff、ledger namespace，午报带 parent |
| 预算可配置且不越界 | `RunProfile.token_budget` + 真实 usage 回调硬上限 |

### A.2 正文未覆盖、本次补入的实现机制

1. **实时取数层**：`tools/node_client.py` + `tools/tool_map.py`，把 app-api 内部接口映射为受限工具（组名→原子工具、参数白名单）。
2. **运行层**：`runtime/` 新增 inputs/runner/graph_run/briefing_projection/archive/bootstrap/cli，负责真实数据编排与 `briefing.v1` 投影。
3. **成本控制**：真实 usage 回调 + 席位均分剩余预算 + 主循环预留 10% 给终局推理；工具结果进模型前结构化压缩。
4. **推理深度**：工具循环之外追加一次无工具的 thinking 综合调用（规避 DeepSeek thinking 不支持强制 `tool_choice` 的限制）。
5. **字段派生**：快照 `indexes` 为 list（`ts_code`/`pct_chg`）；`style` 由指数价差确定性派生并标 `derived:indexes`。
6. **调度**：`config.py` 的 `brain_v4_*` 与 `services/scheduler.py` 注册四个影子任务，默认关闭，与旧链并行。

### A.3 已知缺口（必须在灰度前补齐）

1. **评测台尚未与线上 run 联动**：真实 run 只写影子表并生成单栏审计页，未构造 `EvaluationEnvelope`、未调用 `generate_report`，因此缺旧链/V4 双栏与盲评字段。这对应正文 §10 完成定义第 6 条，需独立执行包补齐。
2. 灰度晋级门禁仍未满足（时间泄漏、insufficient 率、人工盲评、回滚演练），当前 `active_owner` 仍为 legacy。
