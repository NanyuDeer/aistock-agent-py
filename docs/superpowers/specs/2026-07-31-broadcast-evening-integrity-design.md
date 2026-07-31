# 双人播报来源完整性与晚报语境修复设计

**日期：** 2026-07-31  
**范围：** `aistock-agent-py` 的双人播报 Agent、晚报调度与相关测试  
**不在范围内：** 放宽 Node.js 报告校验、调整数据库 schema、修改前端 API 契约。

## 背景与已证实问题

晚报调度已能依次持久化 `review`、`market_snapshot`、`iterate`、`brief_evening` 和
`broadcast_evening`。但生产数据表明，`broadcast_evening.data_source` 为 `NULL`。

Node.js 在生成音频以及公开读取播报时，要求播报报告的 `data_source` 为非空字符串。
因此该记录会被判定为无效：音频生成接口返回 404，公开
`/api/agent/broadcast/evening/:date` 返回 `data: null`。

同一份晚报的对话文本还出现了“早上好”“盘前播报”“隔夜外围”等盘前语境。根因是
晚报调度虽传入 `brief_type="evening"`，但 `broadcast.run()` 在构造 LLM Prompt 时始终：

- 读取 `morning`、`wind_leader`、`hot_burst`、`trend_score`；
- 使用仅面向盘前内容的 `BROADCAST_ANALYST_PROMPT`；
- 以通用“生成今日播报”发起模型调用。

`brief_type` 目前只用于保存记录类型和音频生成请求，没有影响内容生成。

## 现有契约

### Brief v1（前端文字事实层）

`brief_morning` 和 `brief_evening` 的持久化内容均为 `brief.v1`：

```json
{
  "schema_version": "brief.v1",
  "brief_type": "morning | evening",
  "as_of": "ISO-8601",
  "items": [
    {
      "title": "...",
      "conclusion": "...",
      "evidence": [{"report_type": "...", "id": "...", "data_source": "...", "created_at": "..."}],
      "as_of": "ISO-8601",
      "confidence": "unknown",
      "uncertainty": "..."
    }
  ],
  "degraded": false,
  "missing_sources": []
}
```

晨报的必需来源为 `morning`、`wind_leader`、`hot_burst`、`trend_score`；晚报的必需来源为
`review`、`market_snapshot`、`iterate`。

### Broadcast v1（前端播报与音频事实层）

LLM 只生成 `dialogue` 数组；Agent 代码负责构造并持久化以下可验证字段：

```json
{
  "schema_version": "broadcast.v1",
  "brief_type": "morning | evening",
  "source_brief": {
    "id": 123,
    "report_type": "brief_morning | brief_evening",
    "report_date": "YYYY-MM-DD",
    "as_of": "ISO-8601"
  },
  "degraded": false,
  "missing_sources": [],
  "dialogue": [
    {"role": "host", "content": "..."},
    {"role": "analyst", "content": "..."}
  ],
  "audio_path": null
}
```

数据库记录的 `data_source` 不属于 JSON 内容，但必须为非空字符串；本设计统一使用
`broadcast_agent`。

## 前端消费链路

### 早点听页

`pages-sub-app/briefing/index.vue` 根据 `type=morning|evening` 和日期并行读取：

```text
GET /api/agent/brief/{type}/{date}
GET /api/agent/broadcast/{type}/{date}
```

- `brief.v1.items` 驱动头条与洞见卡片；
- `broadcast.v1.audio_path` 驱动播放按钮；
- 即使音频未就绪，已验证的 Brief 仍可展示文字卡片。

### 双人播报详情页

`pages-sub-app/briefing-detail/index.vue` 只读取：

```text
GET /api/agent/broadcast/{type}/{date}
```

它显示 `dialogue`，并在有 `audio_path` 时播放 MP3。Node 和前端均要求播报的
`brief_type`、日期、`source_brief`、降级信息和对话行符合 `broadcast.v1`。

## 设计

### 1. 保持 Node.js 校验不变，修正 Agent 的报告来源

所有由 scheduler 持久化的双人播报在调用 `save_analysis_report()` 时显式传入：

```python
data_source="broadcast_agent"
```

这使 `broadcast_morning` 与 `broadcast_evening` 均满足 Node 的完整性检查。不会给旧记录
补写字段，也不会在 Node 中为缺失来源开例外；重新生成对应日期播报即可用新内容覆盖旧记录。

### 2. 让内容生成按 `brief_type` 选择上下文和 Prompt

保留早间播报的既有业务输入：

| 类型 | LLM 事实输入 | 播报目标 |
|---|---|---|
| `morning` | 晨报、长线风口、机构调研热点、趋势股评分 | 开盘前的关注方向与风险 |
| `evening` | 受控整理的 `brief_evening.items`：收盘复盘、市场快照、迭代分析 | 收盘后的市场现象、归因、风险与下一交易日观察重点 |

晚报不得重新混入盘前上游报告。为此增加一个仅把 `brief.v1` 中受控字段转换为 Prompt 文本的
辅助函数：按 `items` 顺序输出标题、结论、置信度和不确定性；不传递原始快照 JSON，也不让 LLM
生成或修改证据 ID、来源、降级状态。

### 3. 页面事实层与播报对话层分离

“早点听”页面的卡片结构不能由 LLM 对话决定。该页面的稳定事实层必须始终是由代码聚合的
`brief_evening`，而不是 `broadcast_evening.dialogue`。

晚报 Brief 的固定页面映射为：

| Brief 条目 | 上游报告 | 早点听展示位置 | 用途 |
|---|---|---|---|
| `收盘复盘` | `review` | 今日头条 | 今日市场现象与归因 |
| `市场快照` | `market_snapshot` | Agent 洞见 | 市场覆盖、板块或快照完整度 |
| `迭代分析` | `iterate` | Agent 洞见 | 异常维度、数据局限和后续观察 |

这些条目的 `title`、`conclusion`、`evidence`、`as_of`、`confidence`、`uncertainty` 均由
`build_brief("evening")` 按 `brief.v1` 合约构造。LLM 不会写入、修改或覆盖这些字段。

页面读取逻辑保持为：

```text
GET /api/agent/brief/evening/:date
    └─ 成功：渲染晚报头条和洞见卡片（不依赖音频是否成功）

GET /api/agent/broadcast/evening/:date
    └─ 成功：提供音频与双人播报详情
    └─ 未就绪：早点听仍展示 Brief 卡片，并显示“语音生成中”
```

因此，修复 `data_source` 的目标是恢复播报音频和详情页，而不是把晚报卡片绑定到音频成功。
晚报页面可见性的关键前置条件是：`review`、`market_snapshot` 和 `iterate` 全部拥有可追溯的
已完成记录，进而成功保存 `brief_evening`。该前置条件已由晚报 scheduler 在生成播报前检查。

### 4. 晚报 Prompt 只生成 Broadcast 对话内容

晚报 Prompt 必须承认其边界：它不是 Brief 页面结构的生产者。它接收上节所述的受控
`brief_evening` 文本投影，仅生成 `dialogue`。

新增晚报 Prompt 的最终约束为：

```text
最终回复只能是 JSON 数组，不能输出 Markdown、标题、解释、schema_version、
source_brief、audio_path、degraded 或 missing_sources。

[
  {"role": "host", "content": "..."},
  {"role": "analyst", "content": "..."}
]
```

其中：

- `host` 开场采用“晚上好，欢迎收听今日收盘播报”，概括当日结果并串联问题；
- `analyst` 只能基于对应 Brief 条目的 `conclusion` 解释；
- 对话按“收盘复盘 → 市场快照 → 迭代分析/风险 → 下一交易日观察”组织；
- 输入不包含可靠市场事实时，输出“当前数据不足以判断”，不得填补缺失数据；
- 禁止“早上好”“盘前播报”“隔夜外围带动高开”“今日开盘”等盘前措辞；
- 最后一轮包含“仅供参考，不构成投资建议”。

代码继续解析该数组，并独占生成 `broadcast.v1` 的追溯、降级和音频字段。

### 5. 双 Prompt，单一 LLM 对话输出契约

早间 Prompt 继续使用盘前语言。新增晚间 Prompt，要求：

- 开场使用“晚上好”“收盘播报”；
- 顺序覆盖收盘复盘、市场快照、迭代分析；
- 只依据输入事实，数据不足时明确说明；
- 禁止“早上好”“盘前播报”“隔夜外围带动高开”“今日开盘”等盘前表述；
- 以“下一交易日观察重点”结束，并包含投资风险提示。

两种 Prompt 都应强制 LLM 最终仅输出 JSON 数组：

```json
[
  {"role": "host", "content": "..."},
  {"role": "analyst", "content": "..."}
]
```

约束：输出 4 至 6 轮，角色只能是 `host` 或 `analyst`，内容均为非空字符串。LLM 不得输出
`broadcast.v1` 的系统控制字段；Agent 使用既有 `_parse_dialogue()` 和代码生成的元数据封装。

模型调用的用户提示分别为“生成今日盘前播报”和“生成今日收盘播报”。

### 6. 错误处理

- 无有效 `brief_evening` 时，晚报仍按既有降级路径保存，但只使用结构化降级内容，不回退到盘前输入；
- 保存失败时不请求音频生成；
- 音频请求仅在含 `data_source="broadcast_agent"` 的播报保存成功后进行；
- Node 继续拒绝来源为空、源 Brief 不一致、音频路径不匹配的记录。

## 测试策略

在 `tests/integration/test_broadcast_agent.py` 新增或扩展用例：

1. **来源回归**：scheduler 触发的晚报保存调用含
   `data_source="broadcast_agent"`。
2. **晚报上下文**：传入 `brief_type="evening"` 时，LLM 系统提示包含受控的
   `brief_evening` 条目和收盘语境，且不含早间输入占位内容。
3. **Brief 页面事实层**：无论音频生成是否成功，只要三份晚报上游报告有效，
   `build_brief("evening")` 都生成包含“收盘复盘、市场快照、迭代分析”且具有证据字段的
   `brief.v1`。这保证早点听页可独立显示晚报卡片。
4. **晚报持久化契约**：保存 `broadcast_evening`、绑定 `brief_evening`、调用音频接口时
   `brief_type="evening"`。
5. **早报回归**：既有 `broadcast_morning` 输入、来源绑定和音频调用保持不变。

测试先在当前实现上失败：它不传 `data_source`，晚报仍读取并提示晨报数据。随后以最小实现使其通过。

## 验收标准

1. 新生成的 `broadcast_evening` 数据库记录 `data_source='broadcast_agent'`。
2. 同日 `POST /internal/briefing/generate-audio` 返回 `code: 0` 并写入标准晚报 MP3 路径。
3. `GET /api/agent/broadcast/evening/:date` 返回非空 `data`。
4. 即使音频生成失败，`GET /api/agent/brief/evening/:date` 仍返回可渲染的三条晚报事实卡片。
5. 晚报对话使用盘后语境和晚报 Brief 内容，不再出现盘前开场或盘前事实输入。
6. 晨报播报行为和前端 API 字段保持兼容。
