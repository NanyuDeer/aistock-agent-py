# LLM 前缀缓存命中率观测调研（设计辩论产出）

- 日期：2026-08-25
- 范围：`aistock-agent-py`（观测层，Python 侧；不动 Node 计费表）
- 来源：design-debate 技能正反方辩论（R1 立论/攻辩 + R2 辩护/追打 + 主 Agent 裁决，2 轮收敛）
- 状态：**任务已重定义为「观测调研」而非「降成本优化」**——先量化再谈优化，不承诺收益

---

## 1. 背景与问题

LLM 调用层（`services/llm.py`，ChatOpenAI / OpenAI 兼容端点，deep_think 可切 DeepSeek）依赖 provider 的**自动前缀缓存**（OpenAI 自动前缀缓存 / DeepSeek 上下文缓存）降低输入 token 成本与首 token 延迟。但当前**命中率零可观测**：

- `observability/callback.py::_extract_token_usage`（L341-384）只提取 prompt/completion/total 三字段，丢弃了 OpenAI `usage.prompt_tokens_details.cached_tokens` 与 DeepSeek `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`。
- `observability/metrics.py::record_llm_tokens`（L66-82）与 `services/token_usage.py` 累加器均无缓存口径。

**辩论结论**：直接做"prompt 重排 / 冻结前缀"提命中是低 ROI 的——缓存是 provider 自动行为、定时 worker（morning 每日 1-2 次）间隔 24h 远超缓存 TTL 无法跨天命中、chat 链路前缀（`qa_router.py` SYSTEM_PROMPT 在前、单版本内字节稳定）本就已满足命中条件。真正值得做的是**可观测先行**：用数据决定是否立项后续优化。

## 2. 目标与非目标

### 目标

1. callback 层提取缓存命中字段（归一化 + provider 标签），写入既有 metrics 通道，**不落库、不新搭存储**。
2. 建立命中率观测基线（7 天，p50/p90），按 provider 分口径。
3. 输出可决策的命中率报告，触发后续立项评审（闭环消费者 = 本项目组）。

### 非目标（明确不做）

- **不做 prompt 重排 / 前缀冻结 / registry 快照**（辩论裁决不采用，过度设计）。
- **不动计费链路**：`token_usage.py` 累加器、`ws.py:232-245` → `node_api.save_token_usage` 跨仓库透传全部零改动；`chat_token_usage` 表不加列。
- **不做金额折算**（DeepSeek 缓存命中价 ≠ 未命中价，约 10 倍差，折算无意义）。
- **不给低频 worker 设统计告警**（样本量不足，n<20 不告警）。
- **不承诺收益**：观测结束命中率高则优化工作不立项即为正确结论。

## 3. 设计明细

### 3.1 callback 提取与归一化（唯一代码改动）

`observability/callback.py`：

- `_extract_token_usage` 增加缓存字段提取：
  - OpenAI：`usage.prompt_tokens_details.cached_tokens`
  - DeepSeek：`usage.prompt_cache_hit_tokens` / `usage.prompt_cache_miss_tokens`
- 归一化为 `cached_input_tokens`（缺失/不适用 → 0）+ **provider 标签**（按 base_url/model 前缀判定 quick/deep 或 provider 名）。
- 返回值扩展为：`prompt_tokens / completion_tokens / total_tokens / cached_input_tokens / provider`。
- `TokenUsageCallback.on_llm_end`：`cached_input_tokens` 进 MetricsCollector（新增 `record_llm_cache_hit(prompt, cached, provider)` 计数）；**不写 token_usage contextvar**（累加链零改动）。

### 3.2 metrics 指标（复用既有累加器模式）

`observability/metrics.py` 新增：

- `cache_hit_total{prompt_tokens, cached_input_tokens, provider}` 双字段累计（命中率 = cached / prompt 聚合计算）。
- snapshot 输出扩展 `cached_input_tokens` + `provider`（既有 `record_llm_tokens` 签名不变，向后兼容）。

### 3.3 基线观测（7 天）

- 收集周期：7 个自然日（含非交易日，低流量日样本标注）。
- 统计口径：按 provider 分开，命中率 = Σcached / Σprompt（调用粒度样本聚合，不用累加器快照）。
- 最低样本门槛：单 provider 累计样本 n<20 时**不产出统计告警**，仅记录。
- 告警规则：仅"命中率低于基线 p50"触发（无硬目标），部署/注册表变更后首几请求标注预热期不计告警。

### 3.4 报告与闭环

- 观测结束后输出命中率报告（按调用类型：chat 图三节点 / 各 worker / quick vs deep），
- 报告消费方 = 项目组；决策动作 = 立项评审"是否值得做进一步优化"（如 morning {{DATE}} 首行后移、synth_answer summary 后移——**均为需质量评审的可选项，不在本次范围**）。

## 4. 硬约束

1. 命中率统计独立于 `token_usage` 累加链与 `ws.py` 计费透传，零改动。
2. 复用现有 metrics 通道，禁止新搭存储 / 日志风暴（每个 LLM 调用只多两计数，不新增逐调用日志行）。
3. 归一化只映射字段名、不归并语义；指标/告警按 provider 分开、各自基线。
4. `cached` 计入 `prompt_tokens` 与既有账单对齐（不改变已落库的 prompt_tokens 数值）。
5. 不改动任何 prompt 拼接顺序 / SYSTEM_PROMPT 常量字节 / 短会话字节不变硬约束。
6. 只报 token 数，不折算金额。

## 5. 验收标准

1. callback 能同时识别 OpenAI `prompt_tokens_details.cached_tokens` 与 DeepSeek `prompt_cache_hit_tokens`/`prompt_cache_miss_tokens`，归一化输出 provider 标签；未知 provider 字段缺失时安全降级为 0，不抛异常。
2. metrics snapshot 含 `cached_input_tokens` 与 `provider`；`record_llm_tokens` 旧签名调用方零改动。
3. `token_usage.py` / `ws.py` / `node_api.save_token_usage` 字节零改动（git diff 验证）。
4. 全量测试通过，新增单测覆盖：OpenAI 字段提取 / DeepSeek 字段提取 / 缺字段降级 / provider 归一化。
5. 既有 byte-lock 测试（test_qa_router / test_chat_multiturn 等）零回归。

## 6. 不做的事（防 Scope 蔓延）

- 不重排 prompt、不冻结 registry、不加版本指纹。
- 不动 Node 侧 `chat_token_usage` 表 / `save_token_usage` 接口。
- 不做金额折算、不做命中率硬目标、不为低频 worker 设告警。
- 不把 morning {{DATE}} / synth_answer summary 后移纳入本次（可选项，等观测报告立项评审）。

## 7. 开放问题（观测后决策）

1. 若观测显示命中率已高（静态段自动命中）→ 优化工作不立项，报告归档即可。
2. 若显示命中率低且集中于某类调用 → 立项评审针对该类的最小干预（需单独辩论/评审）。
3. provider 双口径下基线差异显著时，是否按 provider 分别设定后续优化优先级。

---

> 辩论记录：R1 反方攻 R-1.1~R-1.5 + G1~G4（跨仓库改动面、双 provider 口径、worker 收益≈0、计费口径、黑盒不可控）；R2 正方认领为主、方案缩窄为观测优先，反方撤 5 缺口、提 N1~N6（目标脱钩、无闭环、样本成本、低频统计意义、价格口径、过度设计）。裁决：N1~N6 由反方自给条件闭合 → 收敛。完整四件套见对话记录。

## 修订的事实（2026-08-25 实现期）

- §3.1 原述「在 `_extract_token_usage` 内增加缓存字段提取并扩展返回值」→ 实现改为**独立 `_extract_cache_usage` + `_get_raw_token_usage`**：`_extract_token_usage` 重构为复用 raw 提取（行为不变），缓存提取走独立函数并返回 `{"prompt_tokens", "cached_input_tokens", "provider"}`；`on_llm_end` 在 `record_llm_tokens` + `record_token_usage` 之后追加 `record_llm_cache_hit`。语义等价，计费路径零改动更清晰。
- §3.2 的 `cache_hit_total{prompt_tokens, cached_input_tokens, provider}` → 实现为 `get_metrics()["llm_cache"][provider] = {"prompt_tokens", "cached_tokens", "hit_rate"}`（hit_rate = cached/prompt，prompt=0 取 0.0）。
- 单测数为 38（两测试文件全部用例），其中新增 7 个缓存命中用例。
