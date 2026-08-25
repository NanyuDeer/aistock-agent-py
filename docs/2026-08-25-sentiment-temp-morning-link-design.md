# 短线情绪温度与晨报联动（冰点次日早报引用预判）设计

- 日期：2026-08-25
- 范围：`aistock-agent-py`（方案 A：agent-py 独立模块 + 文件落盘）
- 状态：已与需求方确认（六指标口径 / 冰点阈值 ≤20 连冰 2 日 / quick_think 预判 / 晨报三档注入）

---

## 1. 背景与问题

当前项目已有一套 **韭圈儿恐贪指数**（app-api `/api/fear-greed`，宏观均衡型：波动率 / 北上资金 / 市场宽度 / 股指期货升贴水 / 股债回报差 5 指标，500 日百分位，0-100），前端已有温度计展示。

但项目**没有「短线情绪温度」**——基于涨跌停 / 炸板 / 连板 / 涨跌家数 / 主力净额的短线情绪周期指标。短线情绪冰点（跌停潮、炸板潮）后次日往往存在修复反弹，这一预判若能进入次日晨报，将提升晨报的实战参考价值。

**本需求**：收盘后计算当日短线情绪温度（0-100）→ 冰点判定（≤20，连续 2 日升级连冰）→ 冰点触发 LLM 生成修复预判 → 次日 08:50 晨报生成时将预判注入 prompt 引用演绎。

## 2. 目标与非目标

### 目标

1. 每日收盘后（15:45）计算当日短线情绪温度，落盘归档。
2. 冰点（温度 ≤ 20）判定与连冰升级；冰点时生成 1-2 句修复预判并落盘。
3. 次日晨报在「板块与市场情绪」环节引用冰点预判（LLM 结合外盘/消息演绎）。

### 非目标（明确不做）

- **不改动现有恐贪指数逻辑**（app-api / fear-greed 模块、前端温度计、PG 表 `fear_greed_snapshot` 全部不动）。短线情绪温度与恐贪指数语义不同、数据源不同、消费方不同，完全解耦。
- 不新增 A 股取数逻辑：全部复用 `data_client` 回调 `/internal/market/close-snapshot` 的既有字段。
- 不落地前端展示（温度历史供未来单独任务消费）。
- 不做盘中实时情绪温度（只做收盘后）。
- 不做情绪温度的「修复命中率」回测验证体系（预留字段，不实现）。
- 不把预判写入 prediction_records（语义绑定溯源报告链路，不污染）。

## 3. 术语

| 术语 | 含义 |
|---|---|
| 短线情绪温度 | 0-100 综合分，基于六指标加权，越高越热 |
| 冰点 | 温度 ≤ 20（短线情绪跌停潮/炸板潮等极端低迷） |
| 连冰 | 连续 ≥2 个交易日温度 ≤ 20，升级标记 |
| 预判 | 冰点触发时 LLM 生成的 1-2 句修复预判（反弹概率 + 关注方向 + 风险） |

## 4. 端到端数据流

```
交易日 15:45  sentiment_temp 定时任务
        │  data_client → /internal/market/close-snapshot（当日完整快照，15:30 门禁）
        ▼
services/sentiment_temp.py
   compute_sentiment_temp()   六指标 → 0-100
   judge_ice()                温度 ≤ 20 → 冰点；连续 ≥2 日 → 连冰
   generate_ice_prediction()  仅冰点时 quick_think 生成预判（失败降级模板话术）
        │
        ▼
落盘 docs/agent-outputs/sentiment/YYYY-MM-DD.json + latest.json
        │
次日 08:50  morning.py
        │  读 latest.json（最近已收盘交易日，周一自然取周五）
        │  冰点 → 注入 {{SENTIMENT_ICE_CONTEXT}}（预判全文+指标概览）
        │  非冰点 → 注入一行温度概览
        │  无文件/异常 → 不注入（占位符替换为空，行为零变化）
        ▼
晨报 LLM 结合外盘/消息演绎引用预判
```

## 5. 设计明细

### 5.1 模块与文件

| 文件 | 职责 |
|---|---|
| `services/sentiment_temp.py`（新增） | 温度计算、冰点判定、预判生成、文件读写，全部逻辑收敛于此 |
| `config.py`（修改） | 新增阈值/权重配置（见 5.3） |
| `services/scheduler.py`（修改） | 注册 `sentiment_temp` 定时任务（15:45） |
| `agents/workers/morning.py`（修改） | 读取 latest.json，注入 `{{SENTIMENT_ICE_CONTEXT}}` |
| `prompts/workers/morning.py`（修改） | 新增占位符说明（「板块与市场情绪」步骤引用指示） |
| `docs/agent-outputs/sentiment/`（运行时产物） | 温度归档 + latest.json |

### 5.2 温度计算（六指标加权，固定标定映射）

每个指标先映射为 0-100 分段分（来源以 close-snapshot 的 `limits` / `breadth` / 主力净额为准），再按权重加权求和，最终 0-100：

| 指标 | 数据字段 | 方向 | 权重 |
|---|---|---|---|
| 涨停数 | `limits.up_count` | 越多越热 | 25% |
| 跌停数 | `limits.down_count` | 越多越冷 | 25% |
| 炸板率 | `broken_count / (up_count + broken_count)` | 越高越冷 | 15% |
| 连板高度 | `limits.highest_board` | 越高越热 | 15% |
| 涨跌家数比 | `breadth.advance_ratio` | 越高越热 | 15% |
| 主力净额 | 大单净额（元 → 亿） | 净流入越热 | 5% |

权重和为 100%。每个指标的分段映射采用模块常量阈值表（v1 固定标定、可解释；冰点阈值与连冰天数进配置，见 5.3）。

### 5.3 冰点判定与配置

| 配置项 | 默认值 | 说明 |
|---|---|---|
| `sentiment_ice_threshold` | 20 | 温度 ≤ 20 判冰点 |
| `sentiment_ice_consecutive_days` | 2 | 连续 ≥2 日 ≤ 阈值升级连冰 |

权重与各指标分段阈值作为模块常量维护（v1 固定标定，不依赖历史样本）。**说明**：若未来需要自适应，可扩展为「近 N 日百分位」口径，但不改变落盘 schema（保留 `level` 字段）。

### 5.4 预判生成（仅冰点）

- 模型：`quick_think`（省 token，1-2 句预判无需深推）。
- 输入：温度分、六指标、冰点天数。
- 输出：1-2 句修复预判（反弹概率表述 → 关注方向 → 风险）。
- 失败降级：代码模板话术（"昨日情绪冰点，短期修复概率较高，注意超跌方向反弹机会"），不阻断链路。
- 语义纪律：预判是**概率性参考**（"修复概率较高"），晨报 LLM 引用时须带"参考历史规律，非确定性指令"表述，叠加既有免责声明。

### 5.5 落盘 schema

`docs/agent-outputs/sentiment/YYYY-MM-DD.json` + `latest.json`（同一结构）：

```json
{
  "date": "2026-08-22",
  "is_trading_day": true,
  "score": 18,
  "level": "冰点",
  "ice": { "is_ice": true, "consecutive_ice_days": 2 },
  "metrics": {
    "up_count": 22, "down_count": 96, "broken_count": 31,
    "highest_board": 3, "advance_ratio": 0.21, "main_force_net_yi": -128.5
  },
  "prediction": {
    "generated": true,
    "text": "昨日情绪冰点（温度18，连续2日），历史规律看短期修复概率较高，关注超跌方向的反弹机会，注意弱势板块补跌风险。"
  }
}
```

- `level` 分档（对齐恐贪分档习惯）：`冰点 ≤20` / `低迷 20-45` / `常温 45-55` / `活跃 55-80` / `亢奋 ≥80`（边界以配置为准，v1 恒定）。
- `prediction.generated=false` 表示 LLM 失败走模板话术；`ice.is_ice=false` 时 `prediction` 可为空对象或缺失。

### 5.6 晨报联动注入（三档）

`agents/workers/morning.py` 构建 system_prompt 时：

1. 读 `latest.json`，解析失败 → 不注入（占位符替换为空串，行为零变化）；
2. 命中冰点 → 替换 `{{SENTIMENT_ICE_CONTEXT}}` 为预判全文 + 指标概览，prompt 指示 LLM 在「板块与市场情绪」步骤结合外盘/消息演绎引用（示例话术见 5.4）；
3. 非冰点 → 替换为一行温度概览（"昨日短线情绪温度 XX（常温）"）；
4. `prompts/workers/morning.py` 的 `{{SENTIMENT_ICE_CONTEXT}}` 占位符说明需明确「仅当占位符被替换内容时引用，空串则忽略」。

缓存语义：晨报 Redis 缓存按日 key，注入只发生在首次生成，缓存命中路径不受影响。

### 5.7 调度

- `services/scheduler.py` 注册 `sentiment_temp`：`45 15 * * 1-5`（15:45，紧随 snapshot_builder 15:35）。
- 交易日守卫 `is_trading_day`，非交易日跳过。
- 任务独立 try/except，失败不阻塞后续链路（对齐既有模式）。

## 6. 错误处理对照

| 场景 | 处理 |
|---|---|
| close-snapshot 数据异常/缺失（含非交易日 409） | 告警日志 + 跳过当日落盘；晨报侧无文件不注入，行为不变 |
| 指标字段缺失（如 limits 缺连板） | 该项按中性分（50 或标定表中位）参与加权，不整体失败；全部关键字段缺失才跳过 |
| LLM 预判失败/超时 | 降级为模板话术落盘（`generated=false`） |
| 文件写入失败 | 告警日志，不阻断 |
| morning 读 latest.json 失败 | 不注入，晨报主链路不变 |

## 7. 测试计划（TDD）

| 层 | 文件 | 覆盖 |
|---|---|---|
| unit | `tests/unit/test_sentiment_temp.py` | 六指标→温度映射纯函数（正例/边界/缺失字段中性化）；冰点判定（20 边界）；连冰判定（连续 2 日）；level 分档 |
| integration | `tests/integration/test_sentiment_temp_scheduler.py` | 定时任务读 snapshot（mock close-snapshot）→ 落盘 + latest.json 更新；LLM 失败降级模板话术 |
| unit | `tests/unit/test_morning_ice_injection.py` | 晨报注入三档：冰点注入预判 / 非冰点一行概览 / 无文件不注入（占位符空串） |

实施时须检查 `prompts/workers/morning.py` 与 `agents/workers/morning.py` 是否被既有测试字节锁定，若有则同步更新。

## 8. 验收标准

1. 交易日收盘后自动生成 `sentiment/YYYY-MM-DD.json` 与 `latest.json`，结构符合 5.5 schema。
2. 温度 ≤20 落 `ice.is_ice=true`；连续 2 日 ≤20 时 `consecutive_ice_days=2`。
3. 冰点次日晨报正文可读到预判引用（含"参考历史规律"式表述）；非冰点晨报仅见温度概览；无数据晨报与现状逐字节一致。
4. 全量测试：新增测试全绿，既有测试失败集 ⊆ 基线（无新增回归）。
5. 现有恐贪指数（app-api）零改动，前端温度计行为不变。

## 9. 不做的事（防 Scope 蔓延）

- 不新增 `prediction_records` 落库、不做命中率回测。
- 不接入前端温度计/详情页（独立后续任务）。
- 不做盘中实时温度、不做板块级情绪温度。
- 不引入新依赖、不新增 A 股取数接口。

## 10. 开放问题（实施前确认）

1. 指数数据缺失时的中性分取值（50 或标定表中位）——实施时在单元测试中固化。
2. `level` 分档边界与恐贪分档（25/45/55/80）是否对齐 —— 已对齐（冰点 ≤20 略有差异，属短线口径，可接受）。