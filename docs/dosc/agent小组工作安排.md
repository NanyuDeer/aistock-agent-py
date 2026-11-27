# Agent 开发工作安排 — 2026-07-06

> 日期：2026-07-06 | 项目负责人

---

## 一、团队分工总览

本周参与 Agent 开发的 4 人：

| 成员 | 角色 | 负责 Agent | 额外任务 |
|------|------|-----------|---------|
| **尹辰** | 项目负责人 | `wind_leader_agent`（长线风口）+ `broadcast_agent`（播报生成） | App 前端页面设计（长线风口页 + 播报效果实现） |
| **王昌泽** | 核心开发 | `morning_agent`（晨报）+ `review_agent`（交易复盘） | - |
| **吴涵晶** | 新人 | `hot_burst_agent`（机构调研热门股） | App 前端接入（组长先做页面） |
| **李俊良** | 新人 | `alert_agent`（异动提醒） | App 异动捕手页面数据接入 |

**本周不参与 Agent 开发**：
- **徐思云**：正在设计事件传导逻辑文档（Word），下周再做 `event_chain_agent`
- **陈菲**：正在研究趋势股始涨点识别逻辑（Word），下周再做 `tenx_agent`

**备注**：
- `forecast_agent`（业绩预测）暂不做，网页端已有数据展示，无需 Agent 增强
- 组长先做 App 前端页面，吴涵晶和李俊良负责 Agent + 数据接入

---

## 二、详细任务分配

### 尹辰 — 长线风口 + 播报生成 + 前端

| 任务 | Agent/页面 | 优先级 | 说明 |
|------|-----------|--------|------|
| **核心任务 1** | `wind_leader_agent`（长线风口） | P0 | 风口龙头筛选 + 持续性研判 |
| **核心任务 2** | `broadcast_agent`（播报生成） | P0 | 汇聚各 Agent 结果 → 双人对话格式播报，参考涨乐AI |
| **前端任务 1** | App 长线风口页面 | P0 | 泡泡图 + 龙头卡列表，参考网页端 WindLeaderPanel |
| **前端任务 2** | App 播报效果实现 | P0 | 双人播报 UI + TTS 语音合成 |
| **协助任务** | Node.js `/internal/*` 新增接口 | P0 | 需新增接口供 Python 调用 |

**工作量**：最大，需完成 2 个 Agent + 2 个前端页面 + Node.js 接口。

**播报 Agent 设计要点**（参考涨乐AI）：
- 输入：各 Agent 分析结果（morning/alert/wind_leader 等）
- 输出：双人对话格式 JSON（AI分析师 + AI主持人）
- 前端联动：TTS 语音合成 + 分角色朗读

### 王昌泽 — 晨报 + 交易复盘

| 任务 | Agent | 优先级 | 说明 |
|------|-------|--------|------|
| **核心任务 1** | `morning_agent`（晨报） | P0 | Phase 3 已有基础，需完善 SSE 流式输出 + Redis 缓存 |
| **核心任务 2** | `review_agent`（交易复盘） | P2 | 新增 Agent，设计交易行为分析框架 |
| **协助任务** | Node.js `/internal/*` 新增接口 | P1 | 配合新增的数据接口 |

**工作量**：负责 2 个 Agent + 接口协助。

### 吴涵晶 — 机构调研热门股 Agent

| 任务 | Agent/页面 | 优先级 | 技术匹配点 |
|------|-----------|--------|-----------|
| **主要任务** | `hot_burst_agent`（机构调研热门股） | P1 | 四信号源共振模型 |
| **前端任务** | App 机构调研热门股页面接入 | P1 | 组长先做页面，吴涵晶对接 Agent API |


**参考资源**：
- 网页端：`aistock-frontend/src/modules/news/views/HotBurstView.vue`
- 后端：`aistock-app-api/src/modules/monitor/HotBurstService.ts`
- 现有 API：`GET /api/cn/institution-research`

**工作量**：1 个 Agent + 前端接入，标准新人任务量。

### 李俊良 — 异动提醒 + 数据接入

| 任务 | Agent/页面 | 优先级 | 技术匹配点 |
|------|-----------|--------|-----------|
| **主要任务** | `alert_agent`（异动提醒） | P1 | 异动识别 + AI解读 |
| **前端任务** | App 异动捕手页面数据接入 | P0 | App 已有前端，只需对接 Agent |


**对接 Node.js 接口**：
- 现有接口：`GET /api/cn/trend-hotspots/events/:stockCode`（按股票获取异动事件）
- 后端服务：`aistock-app-api/src/modules/monitor/service.ts` → `StockMonitorService.getEventsByStockCode()`
- Python Agent 需调用：`GET /internal/monitor/:symbol`（需组长在 internal.ts 新增此接口）

**Agent 增强点**（参考吴涵晶）：
- 现有后端只提供异动数据列表，Agent 可以加 AI 解读：
  - 异动类型分类（短线/中线/长线）
  - AI 解读"发生了什么→为什么→怎么办"
  - 持续性判断

**工作量**：1 个 Agent + 数据接入。

---

## 三、开发路径

### Week 1（2026-07-06 ~ 07-12）

#### 尹辰
1. **Agent 开发**：
   - 完成 `wind_leader_agent` 代码实现
   - 开始 `broadcast_agent` 设计（参考涨乐AI双人播报）
   - 在 `tools/sector_tools.py` 新增 `get_wind_leaders` 工具
   - 编写 pytest 测试

2. **Node.js 接口**：
   - 在 aistock-app-api 新增 `GET /internal/wind-leaders` 接口

3. **App 前端**：
   - 设计长线风口页面（泡泡图 + 龙头卡列表）
   - 参考 `aistock-frontend/src/modules/home/components/WindLeaderPanel.vue`
   - 开始播报效果设计（双人播报 UI）

#### 王昌泽
1. **晨报完善**：
   - 完善 `morning_agent` SSE 流式输出
   - 添加 Redis 缓存逻辑（TTL=2小时）
   - 测试 `/briefing/morning` 接口

2. **交易复盘设计**：
   - 阅读 `docs/refactor-plan.md` 第 7.9 节（review_agent 设计）
   - 设计交易行为分析框架
   - 定义所需的数据接口

#### 吴涵晶
1. **熟悉项目**：
   - 阅读 `aistock-agent-py/AGENTS.md`
   - 阅读网页端 `HotBurstView.vue`（机构调研热门股）
   - 阅读后端 `aistock-app-apiHotBurstService.ts` 理解现有共振模型

2. **开始 hot_burst_agent**：
   - 设计 Agent 增强点（现有 HotBurstService 是算法，Agent 可以加 AI 解读）
   - 在 `tools/news_tools.py` 中对接 `/internal/institution-research` 接口

3. **前端接入**：
   - 等组长完成 App 页面后，对接 Agent API

#### 李俊良
1. **熟悉项目**：
   - 阅读 `aistock-agent-py/AGENTS.md`
   - 阅读 App 异动捕手页面（已有前端）
   - 阅读后端 `service.ts` → `StockMonitorService.getEventsByStockCode()`

2. **开始 alert_agent**：
   - 设计异动检测框架（短线/中线/长线分类）
   - 等组长新增 `/internal/monitor/:symbol` 接口后对接
   - 在 `tools/monitor_tools.py` 中定义 `get_stock_monitor` 工具

3. **数据接入**：
   - 将 Agent 输出接入 App 异动捕手页面
   - App 已有前端，只需对接 Agent API

### Week 2（2026-07-13 ~ 07-19）

#### 组长
- 完善 wind_leader_agent + broadcast_agent
- 完成前端播报效果实现（TTS + UI）
- 开始 stock_analyst（如果时间允许）

#### 王昌泽
- 完成 review_agent 代码实现
- 编写 pytest 测试
- 开始 supervisor（意图路由）如果时间允许

#### 吴涵晶
- 完成 `hot_burst_agent` 代码实现
- 编写 pytest 测试
- 完成 App 页面数据接入
- 下周开始 `tenx_agent`（十倍股评分）

#### 李俊良
- 完成 `alert_agent` 代码实现
- 编写 pytest 测试
- 完成异动捕手页面数据接入
- 下周开始 `forecast_agent`（业绩预测）

---

## 四、参考资源

### 必读文档
| 文档 | 路径 | 内容 |
|------|------|------|
| Agent 入口地图 | `aistock-agent-py/AGENTS.md` | 项目架构、开发规范 |
| Agent 详细设计 | `aistock-agent-py/docs/refactor-plan.md` | 各 Agent 工具集、提示词 |
| 项目工作流 | `d:\aistock\team-guide.md` | 团队开发流程 |
| 网页端风口面板 | `aistock-frontend/src/modules/home/components/WindLeaderPanel.vue` | 长线风口参考 |
| 网页端十倍股 | `aistock-frontend/src/modules/market/views/TenxScoreView.vue` | 评分维度参考 |

### Trae 对话模板

**尹辰开始任务**：
```
在 Trae 中打开 d:\aistock\aistock-agent-py，输入：

我要开发 wind_leader_agent（长线风口 Agent）和 broadcast_agent（播报生成 Agent），
参考 docs/refactor-plan.md 第 7.4 节和第 7.12 节的设计，
broadcast_agent 需要参考涨乐AI的双人播报格式，
输出 AI分析师 + AI主持人的对话式播报内容。
```

**尹辰开始播报前端**：
```
在 Trae 中打开 d:\aistock\aistock-app-frontend，输入：

我要设计双人播报 UI 页面，
参考涨乐软件抓包分析报告中的播报效果，
需要展示 AI分析师和 AI主持人的对话内容，
支持 TTS 语音合成和播放控制（暂停/快进/倍速）。
```

**王昌泽开始任务**：
```
在 Trae 中打开 d:\aistock\aistock-agent-py，输入：

我要完善 morning_agent（晨报 Agent）的 SSE 流式输出，
参考 docs/refactor-plan.md 第 7.2 节的设计，
添加 Redis 缓存逻辑（TTL=2小时）。
```

**吴涵晶开始任务**：
```
在 Trae 中打开 d:\aistock\aistock-agent-py，输入：

我要开发 hot_burst_agent（机构调研热门股 Agent），
参考网页端 HotBurstView.vue 和后端 HotBurstService.ts，
在现有四信号源共振模型基础上增加 AI 解读能力，
在 tools/news_tools.py 中对接 /internal/institution-research 接口。
```

**李俊良开始任务**：
```
在 Trae 中打开 d:\aistock\aistock-agent-py，输入：

我要开发 alert_agent（异动提醒 Agent），
参考 aistock-app-api/src/modules/monitor/service.ts 中的 StockMonitorService.getEventsByStockCode()，
设计异动检测框架（短线/中线/长线分类），
等组长新增 /internal/monitor/:symbol 接口后，在 tools/monitor_tools.py 中对接。
Agent 需要加 AI 解读"发生了什么→为什么→怎么办"。
```

---

## 五、Agent 分工图

```
本周 Agent 开发分工：

┌─────────────────────────────────────────────────────────────┐
│  P0（最高优先级）                                             │
│  ├── morning_agent（晨报）      — 王昌泽                     │
│  └── review_agent（交易复盘）    — 王昌泽                    │
│  ├── wind_leader_agent（长线风口）— 组长                     │
│  ├── broadcast_agent（播报生成） — 组长                   │
│  └── alert_agent（异动提醒）    — 李俊良 + App异动捕手接入   │
│  └── hot_burst_agent（机构调研热门股）— 吴涵晶 + App前端接入 │
├─────────────────────────────────────────────────────────────┤
│  P2（下周）                                                   │
│  ├── tenx_agent（十倍股评分）    — 吴涵晶（下周）            │
│  ├── forecast_agent（业绩预测）  — 李俊良（下周）            │
└─────────────────────────────────────────────────────────────┘

注：event_chain_agent（事件传导链）等徐思云完成逻辑设计后再安排
```

---

## 六、注意事项

1. **禁止修改 shared/ 目录**：如需改动，找项目负责人确认
2. **禁止直接在 main/master 分支开发**：每人创建自己的分支
3. **遵循 aistock-workflow**：开发前必须走 9 步流程（Trae 自动执行）
4. **有问题问 Trae**：直接把错误信息贴给 AI
5. **组长和王昌泽工作量更大**：负责 Agent + 接口/前端协助

---

*本文档由项目负责人创建，如有调整需求请沟通确认。*