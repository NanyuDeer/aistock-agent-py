# AI Stock App 架构设计文档

> 基于6.26会议要求，参照AI涨乐App抓包分析，将现有网站重构为AI智能体驱动的中长线投资App
>
> **v3.0 更新说明**：Agent 推理层从 Node.js/Express 内嵌架构迁移为 **Python/LangGraph 微服务**（`aistock-agent-py`）。Node.js 保留为数据层和 HTTP 接入层，Python 服务专注推理。原因：多 Agent 协作需要 LangGraph 状态机编排，Python 生态（LangGraph/CrewAI）在 Agent 编排领域远超 TS 生态。
>
> **v2.0 更新说明**：前端技术栈由 Expo RN 调整为 **uni-app**（Vue 3 生态，三端统一），后端由新建 Koa 调整为 **复用现有 Express**。保留 Agent + Skills 核心架构不变。

---

## 一、项目背景与战略转变

### 1.1 现有系统

| 模块 | 技术栈 | 说明 |
|------|--------|------|
| aistock-api | Express + PostgreSQL + Redis + TypeScript | Web后端，提供行情/资金流/龙头股/分析等API |
| aistock-frontend | Vue 3 + Element Plus + ECharts | Web前端，PC端网页 |

### 1.2 战略转变（6.26会议核心）

**从"数据展示网站" → "AI智能体驱动的App"**

- 废弃旧网站架构，全面转向App
- 后端对接大模型，构建能与用户对话、执行任务的智能体
- 参考"涨乐"App模式，提供24小时智能体服务
- 采用可扩展的Skills机制，支持功能模块动态接入
- 关注微信智能体机器人推送渠道

### 1.3 功能映射

| 6.26会议模块 | 涨乐对应功能 | 我们的App定位 |
|-------------|------------|-------------|
| 重磅消息 | 重磅事件跟踪 | AI驱动的重大政策/事件推送与解读 |
| 长线风口 | 涨停猎手 + 热点捕手 | 中长线板块机会发现与产业链追踪 |
| 异动提醒 | 特别提醒 | 自选股/持仓异动AI监控与推送 |
| 双人对话播报 | 早点听早/晚报 | 创新双人对话形式，早晚报+盘中实时 |
| (新增) | 动态估值 | 中长线估值分析核心 |
| (新增) | 事件传导 | 事件因果链分析与可视化 |
| (新增) | 持仓陪伴 | 持仓异动AI陪伴与风控 |
| (新增) | 交易复盘 | 历史交易行为分析 |
| (新增) | 智能交易 | 网格交易/分批止盈/AI条件单(模拟) |

### 1.4 技术栈选型决策（v2.0 调整）

**前端：Expo RN → uni-app**

| 决策维度 | Expo RN | uni-app（最终选择） |
|---------|---------|-------------------|
| 团队技术栈 | 需学 React + RN | ✅ Vue 3，零学习成本 |
| 三端覆盖 | App + Web（弱） | ✅ App + H5 + 小程序一等公民 |
| 网页同步更新 | 需维护两套代码 | ✅ 一套代码条件编译 |
| 小程序支持 | 不直接支持 | ✅ 原生支持 |
| 服务器负担 | 无影响 | ✅ 无影响 |
| 动画性能 | 60fps 丝滑 | ⚠️ 略逊，金融工具够用 |

**后端：新建 Koa → 复用 Express**

| 决策维度 | 新建 Koa | 复用 Express（最终选择） |
|---------|---------|----------------------|
| 服务器进程 | 2 个（Express + Koa） | ✅ 1 个 |
| 仓库数量 | 4 个 | ✅ 3 个 |
| Skills 复用 | 需跨仓库引用 | ✅ 同仓库直接 import |
| 维护成本 | 高 | ✅ 低 |
| 架构纯净度 | 高 | ⚠️ Express 中间件模型略逊 Koa |

---

## 二、整体架构

### 2.1 系统架构图

```
┌─────────────────────────────────────────────────────────────────────┐
│                        客户端层 (Client)                              │
│                                                                       │
│  ┌──────────────────┐  ┌──────────────────┐  ┌──────────────────┐   │
│  │  App (uni-app)   │  │  H5 网页          │  │  微信小程序       │   │
│  │  主入口·全功能    │  │  精简功能         │  │  轻量入口         │   │
│  │  (iOS/Android)   │  │  (条件编译裁剪)   │  │  + 智能体机器人   │   │
│  └────────┬─────────┘  └────────┬─────────┘  └────────┬─────────┘   │
│           │                      │                      │             │
│   一套代码 + 条件编译 #ifdef 控制功能差异                  │             │
└───────────┼──────────────────────┼──────────────────────┼─────────────┘
            │                      │                      │
            ▼                      ▼                      ▼
┌─────────────────────────────────────────────────────────────────────┐
│                   Node.js 数据层 + HTTP 接入层                        │
│                   (aistock-app-api, Express)                         │
│                                                                       │
│  · HTTP路由 / WebSocket接入 / 用户认证 / 会话管理                     │
│  · 全部A股数据Service（Tencent/Sina/Tushare/THS）                   │
│  · /api/agent/* → 反代到 Python Agent 服务                           │
│  · /internal/* → Python 专用数据接口（内网鉴权）                      │
└───────────────────────────────┬──────────────────────────────────────┘
                                │ HTTP + SSE
┌───────────────────────────────▼──────────────────────────────────────┐
│              Python Agent 推理层 (aistock-agent-py, FastAPI)          │
│                                                                       │
│  · LangGraph 图编排（多 Agent 协作状态机）                            │
│  · 通过 /internal/* 回调 Node.js 拿 A 股数据                        │
│  · yfinance：境外指数/大宗/汇率（Python 侧直接调用）                 │
│  · Tavily：全网财经新闻搜索（Python 侧直接调用）                     │
│  · Redis checkpointer：会话持久化                                    │
│                                                                       │
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐  ┌──────────────┐ │
│  │ Supervisor  │  │MorningAgent │  │ StockAnalyst │  │WindLeaderAgnt│ │
│  │ (意图分类)   │  │ (晨报·ReAct)│  │ (个股分析)    │  │(长线风口)    │ │
│  └─────────────┘  └─────────────┘  └──────────────┘  └──────────────┘ │
│  ┌──────────────┐ ┌─────────────┐  ┌──────────────┐  ┌──────────────┐│
│  │EventChainAgt │ │ AlertAgent  │  │HotBurstAgent │  │ForecastAgent ││
│  │(事件传导链)   │ │(异动提醒)   │  │(机构调研热门) │  │(业绩预测)    ││
│  └──────────────┘ └─────────────┘  └──────────────┘  └──────────────┘│
│  ┌─────────────┐  ┌─────────────┐  ┌──────────────┐                  │
│  │ ReviewAgent │  │ GeneralAgent│  │BroadcastAgent│  + LangChain @tools│
│  │ (交易复盘)   │  │ (兜底对话)  │  │(双人播报生成)│                  │
│  └─────────────┘  └─────────────┘  └──────────────┘                  │
└─────────────────────────────────────────────────────────────────────┘
                                │
                                ▼
┌─────────────────────────────────────────────────────────────────────┐
│                      数据层 (Data Layer)                              │
│                                                                       │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────────────────┐   │
│  │  PostgreSQL  │  │    Redis     │  │   pgvector (PG 插件)     │   │
│  │  (业务数据)   │  │  (缓存/会话) │  │   (知识检索/RAG·零增量)  │   │
│  └──────────────┘  └──────────────┘  └──────────────────────────┘   │
│                                                                       │
│  注：使用 pgvector 替代独立向量数据库，减少服务器组件                  │
└─────────────────────────────────────────────────────────────────────┘
```

**v3.0 核心变更：** Agent 推理层从 Node.js 内嵌模块拆分为独立 Python 微服务。
**原则：Python 服务不拥有数据，只拥有推理。A 股实时数据留 Node.js。**

### 2.2 仓库规划

| 仓库 | 职责 | 技术栈 |
|------|------|--------|
| `aistock-app-frontend` | App/H5/小程序客户端（一套代码） | **uni-app + Vue 3 + TypeScript + Pinia** |
| `aistock-app-api` | 数据层 + HTTP 接入层（现有 API + /internal/* 数据接口 + /api/agent/* 反代） | **Express + TypeScript**（复用扩展） |
| `aistock-agent-py` | Agent 推理层（LangGraph 多 Agent 编排 + LLM 调用） | **Python + FastAPI + LangGraph + LangChain** |
| `aistock-frontend` | 旧网站前端（保留至 App 上线后废弃） | Vue 3 |

> **v3.0 关键调整**：Agent 推理层从 `aistock-app-api` 内嵌模块拆分为独立 Python 微服务 `aistock-agent-py`。Node.js 通过 `/internal/*` 接口提供 A 股数据，通过 `/api/agent/*` 反代到 Python 服务。Python 服务不拥有数据，只拥有推理。

### 2.3 三端功能分级

通过 uni-app 条件编译 + 分包，实现"一套代码、三端差异":

| 功能 | H5 网页 | App | 小程序 |
|------|---------|-----|--------|
| 股票行情/K线 | ✅ | ✅ | ✅ |
| 自选股管理 | ✅ | ✅ | ✅ |
| 资讯浏览 | ✅ | ✅ | ✅ |
| AI 智能体对话 | ❌ | ✅ | ⚠️ 降级 |
| 双人对话播报 | ❌ | ✅ | ❌ |
| 事件传导图谱 | ✅ 简化 | ✅ 完整 | ✅ 简化 |
| 动态估值 | ✅ | ✅ | ✅ |
| 持仓守护推送 | ❌ | ✅ | ❌ |
| 异动提醒(WebSocket) | ⚠️ 轮询 | ✅ 长连接 | ⚠️ 轮询 |
| 微信智能体机器人 | - | - | ✅ |

---

## 三、前端架构设计

### 3.1 技术选型

| 项目 | 选型 | 理由 |
|------|------|------|
| 跨端框架 | **uni-app (Vue 3)** | 一套代码覆盖 App/H5/小程序，团队 Vue 技术栈零学习成本 |
| 路由 | **pages.json + uni.navigateTo** | uni-app 原生路由，配合分包加载 |
| 状态管理 | **Pinia** | Vue 3 推荐，轻量、TS 友好、支持持久化 |
| UI 组件库 | **wot-design-uni** | TS 友好、设计现代、活跃维护，适合金融场景 |
| 网络请求 | **Axios + @uni-helper/axios-uni** | 复用现有 `api.js` 接口定义，适配器跨端 |
| 本地存储 | **uni.setStorageSync** | 跨端一致；App 端性能敏感场景可用 uts 调原生 MMKV |
| 图表库 | **uCharts + renderjs(ECharts)** | uCharts 跨端一致；H5 端复杂图表用 renderjs+echarts |
| 富文本 | **mp-html** | 支持 HTML/Markdown，替代 v-html |
| 样式方案 | **SCSS + UnoCSS(可选)** | SCSS 变量 + 原子化 CSS |
| 动画 | **uni.createAnimation + CSS** | 满足金融工具克制动效需求 |
| 列表 | **uni-list + 自定义虚拟滚动** | 应对股票列表大数据量 |
| 推送 | **uni.push** | App 端集成个推/小米/华为推送 |
| WebSocket | **uni.connectSocket** | 跨端实时通信 |

### 3.2 目录结构

```
aistock-app-frontend/
├── src/
│   ├── pages/                          # 主包页面（三端共享）
│   │   ├── index/                      # 早点听（首页）
│   │   │   └── index.vue
│   │   ├── stock/                      # 股票模块
│   │   │   ├── detail.vue              # 个股详情
│   │   │   ├── search.vue              # 搜索
│   │   │   └── monitor.vue             # 异动监控
│   │   ├── favorites/                  # 自选股
│   │   │   └── index.vue
│   │   ├── news/                       # 资讯
│   │   │   └── wechat-message.vue
│   │   ├── tag/                        # 板块龙头
│   │   │   └── leaders.vue
│   │   ├── user/                       # 用户
│   │   │   ├── login.vue
│   │   │   └── profile.vue
│   │   └── about/
│   │       └── update-logs.vue
│   │
│   ├── pages-sub-app/                  # App 专属分包（仅 App 编译）
│   │   ├── chat/                       # AI 对话页（全屏）
│   │   │   └── index.vue
│   │   ├── briefing/                   # 双人对话播报
│   │   │   └── index.vue
│   │   ├── event-chain/                # 事件传导详情
│   │   │   └── index.vue
│   │   ├── valuation/                  # 动态估值详情
│   │   │   └── index.vue
│   │   ├── portfolio/                  # 持仓陪伴
│   │   │   └── index.vue
│   │   └── review/                     # 交易复盘
│   │       └── index.vue
│   │
│   ├── pages-sub-mp/                   # 小程序专属分包（仅小程序编译）
│   │   └── wechat-bot/                 # 微信智能体机器人
│   │       └── index.vue
│   │
│   ├── components/                     # 全局公共组件
│   │   ├── ui/                         # 基础 UI
│   │   │   ├── Card.vue
│   │   │   ├── Button.vue
│   │   │   ├── Avatar.vue              # AI 虚拟形象
│   │   │   ├── EmptyState.vue
│   │   │   └── LoadingState.vue
│   │   ├── chat/                       # 对话组件
│   │   │   ├── ChatBubble.vue
│   │   │   ├── SkillButton.vue         # Skills 快捷入口
│   │   │   ├── SkillCard.vue           # Skill 结果卡片
│   │   │   └── StreamingText.vue       # 流式文本渲染
│   │   ├── stock/                      # 股票组件
│   │   │   ├── StockCard.vue
│   │   │   ├── StockCardList.vue
│   │   │   ├── KLineChart.vue          # K线图（uCharts）
│   │   │   └── StockDetailTable.vue
│   │   ├── chart/                      # 图表组件
│   │   │   ├── RadarChart.vue          # 雷达图
│   │   │   └── RelationGraph.vue       # 关系图谱（事件传导）
│   │   ├── event/                      # 事件组件
│   │   │   ├── EventChainGraph.vue     # 因果链图谱
│   │   │   └── EventCard.vue
│   │   ├── briefing/                   # 早报组件
│   │   │   ├── MorningCard.vue
│   │   │   └── DualHostPlayer.vue       # 双人对话播放器
│   │   ├── market/                     # 市场组件
│   │   │   ├── MarketOverview.vue
│   │   │   └── NewsSlider.vue
│   │   └── layout/                     # 布局组件
│   │       ├── TheNavbar.vue
│   │       └── TheFooter.vue
│   │
│   ├── uni_modules/                    # 模块化插件（物理隔离）
│   │   ├── aistock-auth/               # 认证模块
│   │   └── aistock-push/               # 推送模块
│   │
│   ├── api/                            # 接口层
│   │   ├── request.ts                  # Axios 实例 + 适配器 + 拦截器
│   │   ├── modules/
│   │   │   ├── auth.ts
│   │   │   ├── stock.ts
│   │   │   ├── news.ts
│   │   │   ├── agent.ts                # AI 对话 API
│   │   │   ├── briefing.ts             # 早报 API
│   │   │   ├── event.ts                # 事件传导 API
│   │   │   ├── valuation.ts            # 估值 API
│   │   │   ├── portfolio.ts            # 持仓 API
│   │   │   └── push.ts
│   │   └── types.ts
│   │
│   ├── store/                          # Pinia 状态管理
│   │   ├── index.ts
│   │   ├── modules/
│   │   │   ├── user.ts                 # 用户/认证
│   │   │   ├── favorites.ts            # 自选股
│   │   │   ├── market.ts               # 市场概览
│   │   │   ├── chat.ts                 # AI 对话上下文
│   │   │   ├── portfolio.ts            # 持仓
│   │   │   └── app.ts                  # 全局配置
│   │   └── plugins/
│   │       └── persistence.ts          # 持久化（uni.setStorageSync）
│   │
│   ├── hooks/                          # 组合式函数
│   │   ├── useWebSocket.ts             # WebSocket 实时通信
│   │   ├── useStreamingChat.ts         # 流式对话
│   │   ├── usePushNotification.ts      # 推送通知
│   │   ├── useStockCycle.ts            # 股票周期
│   │   ├── useAuth.ts
│   │   ├── useFavorites.ts
│   │   └── useTimer.ts                 # 定时器（自动清理）
│   │
│   ├── utils/                          # 工具函数
│   │   ├── request.ts
│   │   ├── storage.ts                  # 封装 uni.setStorageSync
│   │   ├── cache.ts
│   │   ├── datetime.ts
│   │   ├── stock.ts
│   │   ├── tradingTime.ts
│   │   ├── platform.ts                 # 平台判断
│   │   └── constants.ts
│   │
│   ├── static/                         # 静态资源
│   │   ├── images/
│   │   ├── icons/
│   │   │   └── tabbar/
│   │   └── audio/                      # 双人对话音频缓存
│   │
│   ├── styles/                         # 全局样式
│   │   ├── variables.scss              # 深色主题变量
│   │   ├── mixins.scss
│   │   ├── reset.scss
│   │   └── common.scss
│   │
│   ├── App.vue
│   ├── main.ts
│   ├── pages.json                      # 页面路由 + 分包配置
│   ├── manifest.json                   # 应用配置
│   └── uni.scss
│
├── env/
│   ├── .env.development
│   └── .env.production
├── package.json
├── tsconfig.json
└── vite.config.ts
```

### 3.3 条件编译与分包策略

#### 3.3.1 pages.json 分包配置

```json
{
  "pages": [
    { "path": "pages/index/index", "style": { "navigationBarTitleText": "早点听" } },
    { "path": "pages/stock/detail", "style": {} },
    { "path": "pages/favorites/index", "style": {} }
  ],
  "subPackages": [
    {
      "root": "pages-sub-app",
      "pages": [
        { "path": "chat/index", "style": { "navigationStyle": "custom" } },
        { "path": "briefing/index", "style": {} },
        { "path": "event-chain/index", "style": {} },
        { "path": "valuation/index", "style": {} },
        { "path": "portfolio/index", "style": {} },
        { "path": "review/index", "style": {} }
      ]
    },
    {
      "root": "pages-sub-mp",
      "pages": [
        { "path": "wechat-bot/index", "style": {} }
      ]
    }
  ],
  "tabBar": {
    "custom": true,
    "color": "#9ca3af",
    "selectedColor": "#4d7cfe",
    "list": [
      { "pagePath": "pages/index/index", "text": "早点听" },
      { "pagePath": "pages/favorites/index", "text": "提醒" },
      { "pagePath": "pages/user/profile", "text": "我的" }
    ]
  }
}
```

#### 3.3.2 底部常驻导航设计（自定义 TabBar + AI 对话栏）

**设计理念**：参考涨乐财富通底部设计，采用"两层式"底部常驻栏——上层 Tab 切换 + 下层 AI 对话栏。系统原生 tabBar 完全由自定义组件替代，AI 对话入口在所有 Tab 页常驻，强化"AI 投顾"产品定位。

**整体结构（从下往上）**：
```
┌──────────────────────────────────────────┐
│  页面内容区（可滚动）                        │
│                                            │
├──────────────────────────────────────────┤
│  ✈️    📊    [早点听]    🔔    👤        │  ← Tab 切换行（5个，中间胶囊高亮）
├──────────────────────────────────────────┤
│  交易   搜问或按住说  未读11  🔊    自选  │  ← AI 对话行（常驻）
├──────────────────────────────────────────┤
│  内容由AI生成，不构成投资建议               │  ← 免责声明
└──────────────────────────────────────────┘
```

**组件位置**：`components/layout/AppBottomBar.vue`

**Tab 切换行（上层）**：
- 5 个 Tab，从左到右：发现（✈️）、行情（📊）、早点听（📻）、提醒（🔔）、我的（👤）
- **选中状态**：所有Tab选中时统一显示胶囊背景+文字（蓝色），样式一致
- **未选中状态**：显示对应图标
- **尺寸**：图标28rpx，文字20rpx，高度42rpx（缩小1/3）
- **间隔**：gap 12rpx（紧凑），边距32rpx（离边缘有距离）
- **背景**：透明
- 点击 Tab 切换页面：发现/行情（开发中）、早点听（首页）、提醒（特别提醒页）、我的（个人中心页）

**AI 对话行（下层）**：
- 左侧"交易"快捷入口（圆形白色背景，灰色文字）
- 中间输入框："搜问或按住说" + 未读消息数 + 语音图标
- 右侧"自选"快捷入口（圆形白色背景，灰色文字）
- 左右按钮样式与对话框统一：白色背景、蓝色边框、阴影
- 点击输入框 → 进入全屏 AI 对话页
- 点击"交易" → 交易功能开发中
- 点击"自选" → 跳转到我的自选页（`/pages/stock/favorites`）

**设计优势**：
1. 解决系统 tabBar 遮挡页面底部操作按钮的问题
2. AI 对话入口常驻，降低用户唤起成本
3. 样式完全自定义，支持胶囊高亮、渐变、特殊按钮形态
4. 与涨乐等主流金融 App 交互范式对齐，降低用户学习成本
5. 免责声明常驻底部，合规要求满足

```vue
<template>
  <view class="container">
    <!-- 公共功能：三端都有 -->
    <StockCardList :stocks="stocks" @click="goDetail" />
    
    <!-- #ifdef APP-PLUS -->
    <!-- 仅 App 端：AI 对话入口 -->
    <button class="chat-entry" @click="openChat">🤖 AI 投顾</button>
    <!-- #endif -->
    
    <!-- #ifdef H5 -->
    <!-- 仅 H5 端：引导下载 App -->
    <view class="download-tip">下载 App 体验 AI 投顾 →</view>
    <!-- #endif -->
  </view>
</template>

<script setup lang="ts">
// #ifdef APP-PLUS
import { useChatStore } from '@/store/modules/chat'
import { useWebSocket } from '@/hooks/useWebSocket'
// #endif

const openChat = () => {
  // #ifdef APP-PLUS
  uni.navigateTo({ url: '/pages-sub-app/chat/index' })
  // #endif
  
  // #ifdef H5
  uni.showModal({ title: '提示', content: '请下载 App 体验 AI 投顾' })
  // #endif
}
</script>
```

### 3.4 核心页面设计

#### 3.4.1 早点听（首页）

```
┌─────────────────────────────────┐
│  早点听                   👤   │  ← 顶栏：标题 + 头像
├─────────────────────────────────┤
│                                 │
│  ┌─────────────────────────┐    │
│  │   📻 今日专属晚报         │    │  ← 播报卡片
│  │  重点看 山西焦化 获主力抢筹 │    │
│  │  或存反弹机会  ◉专属播报  │    │
│  └─────────────────────────┘    │
│                                 │
│  ┌─────────┐ ┌─────────┐       │
│  │长线风口  │ │异动捕手  │       │  ← 核心入口（两列卡片）
│  │ 主力新动向│ │ TOP1 创新药 │       │
│  └─────────┘ └─────────┘       │
│                                 │
│  ┌─────────────────────────┐    │
│  │  📰 重磅事件跟踪          │    │  ← 事件流，点击跳资讯详情
│  │  动力煤需求阶段性回落...  │    │
│  └─────────────────────────┘    │
│                                 │
├─────┬─────┬─────┤
│早点听│ 提醒 │ 我的 │  ← 底部Tab
└─────┴─────┴─────┘
```

#### 3.4.2 提醒页（自选股异动）

**定位**：自选股异动 AI 监控的聚合展示页，替代原"自选"Tab。

**设计参考**：涨乐财富通"特别提醒"页。

```
┌─────────────────────────────────┐
│  特别提醒                🀡   │  ← 顶部标题 + 网格视图切换
├─────────────────────────────────┤
│  👆 查看持仓特别提醒          ›  │  ← 持仓提醒入口
├─────────────────────────────────┤
│                                 │
│  山西焦化          +1.55%  📈  │  ← 股票名称 + 涨跌幅 + 迷你图
│                                 │
│  昨 10:33 行业需求变化           │  ← 时间线：时间 + 标签
│  焦炭相关行业需求有升有降 ›      │     事件描述 + 详情箭头
│                                 │
│  • 10:30 成交放量               │  ← 普通异动（无日期标记）
│  量比为2.07，属于明显放量，      │
│  后续价格波动性可能加大...       │
│                                 │
│  26日 15:00 融券比率上榜        │  ← 日期标记异动
│  融券比率全市场前74，做空        │
│  力量压制，市场较为悲观...       │
│                                 │
├─────────────────────────────────┤
│ 1/4  🔵买    ✦ 帮我分析         │  ← 底部：页码 + 快捷操作
└─────────────────────────────────┘
```

**页面结构**：
- 顶部：仅"特别提醒"一个 Tab（已移除"Yomi选"），右侧网格视图切换按钮
- 持仓提醒入口：跳转至持仓陪伴页查看持仓股票异动
- 主体：左右滑动切换不同股票的异动时间线

**时间线类型**：
- **昨日标记**（"昨"）：非今日事件
- **日期标记**（"26日"）：更早日期的事件
- **当日事件**：只有时间，无前缀
- **普通异动**：用 "•" 圆点标识，属于同一类别的子事件

**异动标签类型**：
- 行业需求变化、成交放量、融券比率上榜、基本面突变、主力资金流入、技术破位、龙虎榜异动、公告利好/利空 等

**底部操作区**：
- 页码指示（如 1/4，表示自选股列表第1只，共4只有异动）
- 快捷操作：买入按钮、"帮我分析"（跳转 AI 对话）

#### 3.4.2 长线风口页（板块龙头 + 泡泡图）

**定位**：中长线板块机会发现与产业链追踪页，包含风口概念泡泡图和板块龙头卡片列表。

**泡泡图设计**：
- 使用圆形泡泡展示板块概念，泡泡大小代表AI研判持续性（长期 > 中期 > 短期）
- 泡泡颜色：涨为蓝色（深浅按score），跌为绿色
- 点击泡泡可查看对应板块详情
- 无后端数据时展示mock泡泡数据

```
┌─────────────────────────────────┐
│  长线风口                       │
│  AI 研判的板块龙头与主线个股     │
├─────────────────────────────────┤
│  风口概念     泡泡越大持续性越强 │
│  ┌───────────────────────────┐  │
│  │   ○创新药  ○AI算力        │  │  ← 大泡泡=长期
│  │ ○光伏 ○半导体  ○锂电池    │  │  ← 中泡泡=中期
│  │  ○白酒  ○动力煤 ○稀土     │  │  ← 小泡泡=短期
│  └───────────────────────────┘  │
├─────────────────────────────────┤
│  No.1 创新药          +3.52%   │
│  龙头 药明康德  +8.52%         │
│  驱动 美国标普生物科技...       │
│  持续性 长期(5-10天)            │
│  主线个股: 舒泰神/广生堂/...    │
└─────────────────────────────────┘
```

#### 3.4.3 资讯详情页

**定位**：重磅事件跟踪/资讯列表点击后进入的资讯详情查看页。

**数据来源**：复用网页端 `/api/news/{id}` 接口，支持正文HTML渲染、摘要高亮、原文链接跳转。
- 无后端数据时展示mock资讯内容

```
┌─────────────────────────────────┐
│  ←  资讯详情                    │
├─────────────────────────────────┤
│  动力煤需求阶段性回落，旺季...   │  ← 标题
│  财联社  2026-06-30 14:30      │  ← 来源+时间
├─────────────────────────────────┤
│  ▎动力煤价格阶段性承压，但      │  ← 摘要（蓝色左边框）
│  ▎夏季用电高峰预期强烈...       │
├─────────────────────────────────┤
│  近日，动力煤市场价格出现...     │  ← 正文
│  不过，市场对旺季预期仍然较强... │
│                                 │
│  ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─ ─  │
│  查看原文 ›                     │  ← 原文链接
└─────────────────────────────────┘
```

#### 3.4.4 AI对话页（App 专属，全屏 Modal）

```
┌─────────────────────────────────┐
│  ←  AI投顾                      │
├─────────────────────────────────┤
│                                 │
│  ┌─────────────────────────┐    │
│  │ 🤖 虚拟投顾形象           │    │  ← AI头像 + 名称
│  │ "早上好！今天市场有几个     │    │
│  │  重要信号值得关注..."       │    │  ← AI消息气泡
│  └─────────────────────────┘    │
│                                 │
│  ┌─────────────────────────┐    │
│  │ 👤 "帮我分析茅台的估值"    │    │  ← 用户消息
│  └─────────────────────────┘    │
│                                 │
│  ┌─────────────────────────┐    │
│  │ 🤖 [调用动态估值Skill]     │    │
│  │ "茅台当前估值处于合理区间   │    │
│  │  偏上，四维度分析如下..."    │    │
│  │ ┌───────────────────┐   │    │
│  │ │  📊 估值卡片        │   │    │  ← Skill结果卡片
│  │ │  严重低估·低估·合理·高估│   │    │
│  │ │           ▲          │   │    │
│  │ └───────────────────┘   │    │
│  └─────────────────────────┘    │
│                                 │
├─────────────────────────────────┤
│  [快捷Skills] 📊估值 📡传导 📰研报│  ← Skills入口
├─────────────────────────────────┤
│  💬 输入消息...            🎤   │  ← 输入框 + 语音
└─────────────────────────────────┘
```

#### 3.4.3 流式对话实现（uni-app）

```typescript
// src/hooks/useStreamingChat.ts
import { ref } from 'vue'
import { agentApi } from '@/api/modules/agent'

export function useStreamingChat() {
  const messages = ref<ChatMessage[]>([])
  const streaming = ref(false)
  
  async function sendMessage(content: string) {
    streaming.value = true
    
    // 方式1：WebSocket（推荐，App 端）
    // #ifdef APP-PLUS
    const ws = uni.connectSocket({ url: 'wss://api.example.com/ws/chat' })
    ws.onMessage((res) => {
      const chunk = JSON.parse(res.data)
      appendChunk(chunk)
    })
    ws.send({ data: JSON.stringify({ type: 'chat', message: content }) })
    // #endif
    
    // 方式2：uni.request enableChunked（H5/小程序降级）
    // #ifndef APP-PLUS
    const task = uni.request({
      url: '/api/agent/chat',
      method: 'POST',
      data: { message: content },
      enableChunked: true,
      responseType: 'text'
    })
    task.onChunkReceived((res) => {
      const chunk = parseChunk(res.data)
      appendChunk(chunk)
    })
    // #endif
    
    streaming.value = false
  }
  
  return { messages, streaming, sendMessage }
}
```

### 3.5 UI/UX设计规范（6.26会议要求）

| 项目 | 规范 |
|------|------|
| **虚拟形象** | 专业严肃，西装证券从业者/投顾形象，非卡通/轻松风格 |
| **布局方式** | 卡片式布局，每张卡片一个信息单元 |
| **信息密度** | 遵循"信息减熵"原则，只展示与用户强相关的内容 |
| **配色方案** | 深色主题为主（#1a1a2e底色），专业金融感 |
| **字体** | 数字使用等宽字体（参考涨乐DINPro），中文清晰易读 |
| **动效** | 克制，仅关键交互有反馈，不花哨 |

### 3.6 图表可视化方案

| 图表类型 | App 端 | H5 端 | 小程序端 |
|---------|--------|-------|---------|
| K 线图 | uCharts | renderjs + ECharts | uCharts |
| 雷达图（十倍股） | uCharts | ECharts | uCharts |
| 事件传导图谱 | canvas 自绘 | renderjs + ECharts graph | canvas 自绘 |
| 风口泡泡图 | uCharts 散点 | D3 | uCharts 散点 |
| 资金流向柱状图 | uCharts | ECharts | uCharts |

> **策略**：uCharts 保证三端一致；H5 端用 renderjs + ECharts 提升复杂图表体验。

---

## 四、后端架构设计

### 4.1 技术选型

#### Node.js 数据层（aistock-app-api）

| 项目 | 选型 | 理由 |
|------|------|------|
| 框架 | **Express（复用现有）** | 不新建 Koa 仓库，减少进程和服务器负担 |
| 语言 | **TypeScript** | 与旧项目保持一致 |
| 数据库 | **PostgreSQL** | 复用旧项目数据库 |
| 缓存 | **Redis** | 复用旧项目 Redis |
| 向量检索 | **pgvector（PG 插件）** | 替代独立向量数据库，零增量组件 |
| 日志 | **Pino** | 高性能结构化日志 |
| WebSocket | **ws** | 实时推送与流式对话 |
| 定时任务 | **node-cron** | 与旧项目保持一致 |

#### Python Agent 推理层（aistock-agent-py）

| 项目 | 选型 | 理由 |
|------|------|------|
| 框架 | **FastAPI + uvicorn** | 异步高性能，原生 SSE 支持 |
| Agent 编排 | **LangGraph** | 多 Agent 协作状态机，Python 生态最成熟 |
| LLM 接入 | **langchain-openai** | 统一接口，支持多模型切换（DeepSeek/OpenAI） |
| 境外数据 | **yfinance** | 美股/亚太/大宗/汇率，Node.js 无对应实现 |
| 全网搜索 | **Tavily** | 财经新闻搜索，Node.js 无对应实现 |
| 缓存/会话 | **Redis** | 与 Node.js 共用 Redis 实例 |
| 数据获取 | **httpx** | 异步 HTTP 回调 Node.js /internal/* API |
| 配置 | **pydantic-settings** | 类型安全的环境变量管理 |

#### 双模型策略

| 用途 | 模型 | 原因 |
|------|------|------|
| 意图分类/路由 | quick_think（gpt-4o-mini） | 低延迟，成本低 |
| 深度分析/晨报/事件 | deep_think（gpt-4o） | 推理质量优先 |

### 4.2 目录结构

#### aistock-app-api（Node.js 数据层）

```
aistock-app-api/src/
├── index.ts                      # 入口：Express + WebSocket 挂载
├── db.ts                         # PostgreSQL 连接（现有）
├── redis.ts                      # Redis 连接（现有）
│
├── modules/                      # 按功能域划分的模块
│   ├── quote/                    # 行情模块
│   │   ├── TencentQuoteService.ts
│   │   ├── SinaMoneyFlowService.ts
│   │   └── TushareTagLeaderService.ts
│   ├── monitor/                  # 监控模块
│   │   ├── ClsStockNewsService.ts
│   │   ├── ThsService.ts
│   │   └── ...
│   ├── agent/                    # Agent 模块（保留 TS 骨架，推理已迁 Python）
│   │   ├── orchestrator.ts       # Agent 调度器（已迁 Python，保留入口）
│   │   └── ...
│   ├── auth/                     # 认证模块
│   ├── push/                     # 推送模块
│   └── ...
│
├── core/routes/                  # 路由注册
│   ├── agent.ts                  # Agent 路由（反代到 Python 服务）
│   ├── internal.ts               # Internal API（Python Agent 专用，内网鉴权）★ v3.0 新增
│   └── ...
│
├── shared/                       # 共享工具
│   ├── utils/
│   └── types/
│
└── ws/                           # WebSocket 服务
    ├── handler.ts
    └── channels/
```

#### aistock-agent-py（Python Agent 推理层）★ v3.0 新增

```
aistock-agent-py/
├── pyproject.toml                # 依赖配置
├── .env.example                  # 环境变量模板
├── Dockerfile                    # Docker 构建
├── AGENTS.md                     # AI 开发指南
├── docs/
│   └── refactor-plan.md          # 重构设计文档
│
└── src/aistock_agent/
    ├── main.py                   # FastAPI 入口
    ├── config.py                 # pydantic-settings 配置
    ├── state/
    │   └── schema.py             # AgentState TypedDict
    ├── graph/
    │   ├── edges.py              # 条件边（route_by_intent）
    │   └── builder.py            # StateGraph 构建 + compile()
    ├── agents/
    │   ├── base.py               # 双模型工厂（quick_think / deep_think）
    │   ├── supervisor.py         # 意图分类节点（quick_think）
    │   ├── morning_agent.py      # 晨报 Agent（ReAct + Redis 缓存）
    │   ├── stock_analyst.py      # 个股分析 Agent（deep_think）
    │   ├── wind_leader_agent.py  # 长线风口 Agent（deep_think）
    │   ├── event_chain_agent.py  # 事件传导链 Agent（deep_think）
    │   ├── alert_agent.py        # 异动提醒 Agent（quick_think）
    │   ├── hot_burst_agent.py    # 机构调研热门股 Agent（deep_think）
    │   ├── tenx_agent.py         # 十倍股评分 Agent（deep_think）
    │   ├── forecast_agent.py     # 业绩预测 Agent（quick_think）
    │   ├── review_agent.py       # 交易复盘 Agent（deep_think, P2）
    │   ├── broadcast_agent.py    # 双人播报生成 Agent（deep_think）★核心特色
    │   └── general_agent.py      # 兜底 Agent（quick_think）
    ├── tools/                    # LangChain @tool，按数据域分组
    │   ├── stock_tools.py        # get_quote, get_capital_flow, get_profit_forecast
    │   ├── sector_tools.py       # get_leader_stocks, get_wind_leaders
    │   ├── news_tools.py         # search_cls_news, get_news_fulltext, get_cls_news
    │   ├── market_tools.py       # get_global_markets(yfinance), tavily_finance_search
    │   ├── monitor_tools.py      # get_stock_monitor, get_alert_history
    │   └── tenx_tools.py         # get_tenx_score, get_tenx_top_stocks
    ├── prompts/                  # 所有提示词集中管理
    │   ├── morning.py            # 晨报4步框架
    │   ├── event_chain.py        # 事件传导链分析
    │   ├── tenx.py               # 十倍股评分
    │   ├── routing.py            # 路由分类
    │   └── system.py             # 通用系统提示词
    ├── services/
    │   └── data_client.py        # httpx → Node.js /internal/* API
    └── api/
        ├── routes.py             # REST 接口
        └── ws.py                 # WebSocket 流式接口
```

### 4.3 Internal API（Node.js → Python 数据接口）

Python 服务通过以下内部接口获取 A 股数据（需携带 `X-Internal-Token` header）：

| 接口 | 数据源 | 说明 |
|------|--------|------|
| `GET /internal/quote/:symbol` | 腾讯行情 | 个股实时行情 |
| `GET /internal/flow/:symbol` | 新浪+Tushare | 资金流向（双源降级） |
| `GET /internal/leader/:tagCode` | Tushare | 板块龙头股 |
| `GET /internal/news/search/:symbol` | 财联社 | 个股相关新闻 |
| `GET /internal/news/fulltext/:id` | 财联社 | 新闻全文 |
| `GET /internal/forecast/:symbol` | 同花顺 | 机构盈利预测 |
| `GET /internal/wind-leaders` | 风口算法 | 镎线风口龙头数据 |
| `GET /internal/monitor/:symbol` | 异动引擎 | 个股异动数据 |
| `GET /internal/tenx/score/:symbol` | 十倍股评分 | 评分详情 |
| `GET /internal/tenx/top` | 十倍股评分 | 排行列表 |
| `GET /internal/graph/concepts` | 知识图谱 | 产业链概念列表 |
| `GET /internal/graph/:concept` | 知识图谱 | 产业链图谱数据 |
| `GET /internal/institution-research` | 机构调研热门股 | 四信号源共振检测结果 |
| `GET /internal/institution-research/history` | 机构调研热门股 | 历史记录 |

这些接口仅供 Python 服务内部调用，不对外暴露，通过 `X-Internal-Token` header 鉴权。

### 4.4 智能体层架构（Python Agent 推理层）

#### 4.4.1 LangGraph 图拓扑

```
START → supervisor(quick_think)
  ├── intent="morning"       → morning_agent(deep_think, ReAct + Redis缓存)
  ├── intent="stock"         → stock_analyst(deep_think)
  ├── intent="wind_leader"   → wind_leader_agent(deep_think)
  ├── intent="event_chain"   → event_chain_agent(deep_think)
  ├── intent="alert"         → alert_agent(quick_think)
  ├── intent="hot_burst"     → hot_burst_agent(deep_think)
  ├── intent="tenx"          → tenx_agent(deep_think)
  ├── intent="forecast"      → forecast_agent(quick_think)
  ├── intent="review"        → review_agent(deep_think)
  └── intent="general"       → general_agent(quick_think)
        │
        ▼
  broadcast_agent（双人播报生成）
        │
        ▼
       END
```

**多专家 Agent 协作体系（参考涨乐AI）**：
```
用户请求（如"早点听"、"异动提醒播报"）
       │
       ▼
   supervisor（意图理解与任务调度）
       │
       ├──→ morning_agent：宏观分析（避险需求、利率、政策等）
       ├──→ wind_leader_agent：长线风口与龙头筛选
       ├──→ alert_agent：异动识别与风险监控
       ├──→ hot_burst_agent：机构调研共振检测
       ├──→ event_chain_agent：事件传导链路分析
       ├──→ tenx_agent：十倍股评分与趋势判断
       ├──→ forecast_agent：业绩预测与机构预期
       └──→ stock_analyst：个股深度分析
              │
              ▼
       broadcast_agent（多 Agent 结果汇聚 → 播报生成）
              │
              ├──→ 输出双人对话格式（AI分析师 + AI主持人）
              └──→ 前端 TTS 语音合成 + 播报播放
```

#### 4.4.2 Agent 调度流程

```
用户消息/事件触发
       │
       ▼
  ┌──────────────────────┐
  │  Node.js 反代层       │
  │  /api/agent/* →      │
  │  转发到 Python 服务   │
  └──────────┬───────────┘
             │ HTTP + SSE
             ▼
  ┌──────────────────────┐
  │  Supervisor Agent     │
  │  (quick_think)        │
  │  · 意图识别           │
  │  · 写入 state.intent  │
  └──────────┬───────────┘
             │ 条件路由
    ┌────────┼────────┐
    ▼        ▼        ▼
┌───────┐┌───────┐┌───────┐
│Morning││Stock  ││Event  │  ...
│Agent  ││Analyst││Analyst│
└───┬───┘└───┬───┘└───┬───┘
    │        │        │
    ▼        ▼        ▼
  ┌──────────────────────┐
  │  LangChain @tools     │
  │  · 调用 Node.js       │
  │    /internal/* 获取数据│
  │  · yfinance 境外数据  │
  │  · Tavily 全网搜索    │
  └──────────┬───────────┘
             │
             ▼
  ┌──────────────────────┐
  │  LLM 生成回复         │
  │  · 流式输出(SSE/WS)   │
  │  · 写入 final_response│
  └──────────────────────┘
```

#### 4.4.3 Tool 接口定义（Python LangChain @tool）

```python
from langchain_core.tools import tool

@tool
async def get_quote(symbol: str) -> str:
    """查询 A 股个股实时行情

    Args:
        symbol: 6位股票代码，如 600519（贵州茅台）
    """
    data = await node_api.get(f"/internal/quote/{symbol}")
    if not data:
        return f"未找到股票 {symbol} 的行情数据"
    return _format_quote(data)
```

#### 4.4.4 Agent 定义

```python
# 每个 Agent 实现 async def run(state: AgentState) -> dict
# 使用 create_react_agent 或自定义节点函数

async def run(state: AgentState) -> dict:
    """晨报分析：宏观策略4步框架"""
    llm = get_deep_think()
    tools = [tavily_finance_search, get_global_markets, get_cls_news]
    agent = create_react_agent(llm, tools)
    result = await agent.ainvoke({"messages": [SystemMessage(content=system_prompt)]})
    return {"final_response": final_response}
```

### 4.5 API 设计

#### 4.5.1 对话 API（核心）

```
POST /api/agent/chat/message
  Request:  { message: string, session_id?: string, context?: object }
  Response: 
    - App 端：WebSocket 流式
    - H5/小程序：SSE 流式 或 enableChunked
    
    data: { type: "text", content: "..." }
    data: { type: "skill_call", skill: "valuation", params: {...} }
    data: { type: "skill_result", result: { type: "card", data: {...} } }
    data: { type: "done" }
```

#### 4.5.2 早报 API

```
GET  /api/agent/briefing/morning         # 获取今日晨报
GET  /api/agent/briefing/evening         # 获取今日晚报
POST /api/agent/briefing/generate-audio  # 生成双人对话音频
```

#### 4.5.3 行情与数据 API（复用现有 + 扩展）

```
# 复用现有
GET  /api/cn/stock/quotes/...            # 个股行情（现有）
GET  /api/cn/stock/quotes/kline          # K线数据（现有）
GET  /api/cn/stocks/:symbol/capital-flow # 资金流向（现有）

# 新增
GET  /api/agent/valuation/:symbol        # 动态估值
GET  /api/agent/event/chain/:id          # 事件传导链
GET  /api/agent/event/list               # 事件列表
```

#### 4.5.4 推送与提醒 API

```
GET  /api/agent/alert/list               # 获取提醒列表
POST /api/agent/alert/subscribe          # 订阅异动提醒
POST /api/agent/alert/unsubscribe        # 取消订阅
POST /api/agent/push/token               # 注册推送 Token（uni.push）
```

#### 4.5.5 WebSocket 事件

```
ws://host/ws
  客户端→服务端:
    { type: "subscribe", symbols: ["600519"] }
    { type: "unsubscribe", symbols: ["600519"] }
    { type: "chat", message: "...", session_id: "..." }

  服务端→客户端:
    { type: "quote_update", data: {...} }      # 行情推送
    { type: "alert", data: {...} }             # 异动提醒
    { type: "chat_stream", data: {...} }       # 对话流式
    { type: "event_alert", data: {...} }       # 事件提醒
```

### 4.6 服务器资源对比

| 资源 | v1.0（新建 Koa） | v2.0（复用 Express） | v3.0（Express + Python Agent） |
|------|-----------------|---------------------|-------------------------------|
| 后端进程 | Express + Koa（2 个） | 仅 Express（1 个） | Express + Python（2 个） |
| 向量数据库 | Chroma（独立组件） | pgvector（PG 插件） | pgvector（PG 插件） |
| 仓库数量 | 4 个 | 3 个 | **4 个**（新增 aistock-agent-py） |
| Agent 编排 | 无 | TS 自研（有限） | **LangGraph（成熟框架）** |
| 推荐服务器配置 | 4C8G | 2C4G | **2C4G**（Python 进程轻量） |

---

## 五、智能体与 Skills 详细设计

### 5.1 智能体矩阵

| Agent | 触发场景 | 核心职责 | 可调用 Skills | 优先级 |
|-------|---------|---------|-------------|--------|
| **晨间决策官** | 打开早点听、触发早报时间 | 生成个性化晨报/晚报，整合重磅消息+长线风口+异动 | stock_quote, capital_flow, event_chain, wind_leader, tavily_search | P0 |
| **个股分析官** | 询问个股、股票详情页 | 个股行情+资金流向+机构预测+相关新闻综合分析 | stock_quote, capital_flow, profit_forecast, search_news | P0 |
| **风口龙头官** | 询问板块/风口、长线风口页 | 板块龙头筛选+风口持续性研判+板块资金动向 | wind_leader, leader_stock, capital_flow | P0 |
| **事件传导官** | 询问事件、重磅消息推送 | 事件识别→5级评分→传导链分析→历史回溯 | event_chain, search_news, tavily_search, knowledge_graph | P0 |
| **异动提醒官** | 自选股异动、特别提醒页 | 异动检测→类型分类→AI解读→应对建议 | stock_monitor, stock_quote, capital_flow, search_news | P1 |
| **机构调研热门官** | 机构调研热门股页 | 四信号源共振检测→AI解读共振原因→持续性判断 | hot_burst, hot_burst_history, stock_quote, tavily_search | P1 |
| **十倍股评分官** | 询问趋势股/评分、十倍股页 | 多维度评分(6维度18指标)→趋势起始点判断→潜力排序 | tenx_score, stock_quote, capital_flow, profit_forecast | P2 |
| **业绩预测官** | 询问业绩预测、业绩预测页 | 业绩预告/预测数据聚合→AI提炼核心要点→机构预期整理 | profit_forecast, stock_quote, search_news | 后续 |
| **交易复盘官** | 请求复盘、盘后推送 | 交易行为分析→标记高光/待改进→优化建议 | trade_sim, stock_quote | P2 |
| **双人播报官** | 各 Agent 分析完成后 | 汇聚各 Agent 结果→生成双人对话格式播报内容 | 无（汇聚上游 Agent 结果） | **P0（核心特色）** |
| **通用对话** | 其他所有对话 | 意图路由→调用对应 Agent 或直接回答 | 所有 Skills | P0 |

### 5.2 Skills 详细设计

| Skill | 功能 | 数据源 | 复用现有服务 | 优先级 |
|-------|------|--------|------------|--------|
| `stock_quote` | 个股实时行情 | 腾讯行情 API | ✅ TencentQuoteService | P0 |
| `capital_flow` | 主力资金流向 | 新浪/Tushare | ✅ SinaMoneyFlowService | P0 |
| `wind_leader` | 风口龙头数据 | 自研风口算法 | ✅ WindLeaderService | P0 |
| `event_chain` | 事件传导链分析 | LLM 推理 + AI产业链图谱 | ✅ IndustryKGService | P0 |
| `search_news` | 个股相关新闻搜索 | 财联社 | ✅ ClsStockNewsService | P0 |
| `tavily_search` | 全网财经搜索 | Tavily API | ❌ Python侧新增 | P0 |
| `hot_burst` | 机构调研热门股（四信号源共振） | 格隆汇/财联社/同花顺/研报 | ✅ HotBurstService | P1 |
| `hot_burst_history` | 机构调研热门股历史 | HotBurstService | ✅ HotBurstService | P1 |
| `stock_monitor` | 个股异动数据 | 腾讯行情 + 规则引擎 | ✅ StockMonitorService | P1 |
| `tenx_score` | 十倍股评分 | 6维度18指标评分系统 | ✅ TenxScoreService | P2 |
| `profit_forecast` | 业绩预测/预告 | 同花顺 + 财联社 | ✅ ThsService | 后续 |
| `leader_stock` | 板块龙头股筛选 | Tushare概念龙头 | ✅ TushareTagLeaderService | P1 |
| `knowledge_graph` | AI产业链图谱 | PostgreSQL + 向量检索 | ✅ IndustryKGService | P1 |
| `trade_sim` | 交易模拟(网格/止盈) | 模拟计算 | ❌ 新增 | P2 |

### 5.3 双人对话播报设计（Broadcast Agent）

6.26 会议要求的创新内容形式——双人对话播报，由 **broadcast_agent** 实现：

```
双人对话播报生成流程:

  触发时间（盘前/盘后）或用户请求播报
       │
       ▼
  各功能 Agent 完成分析任务
       │
       ├── morning_agent → 晨报分析结果
       ├── alert_agent → 异动提醒结果
       ├── wind_leader_agent → 镎线风口结果
       └── event_chain_agent → 事件传导结果
       │
       ▼
  broadcast_agent（双人播报生成）
       │
       ├── 汇聚各 Agent 分析结果
       ├── 生成双人对话脚本（AI分析师 + AI主持人）
       └── 输出结构化 JSON
       │
       ▼
  前端 TTS 服务合成音频
       │
       ├── 语音A（AI分析师）：专业沉稳
       └── 语音B（AI主持人）：清晰明快
       │
       ▼
  输出：双人对话播报音频 + 文字稿
```

**播报格式设计（broadcast_agent 输出）**：
```json
{
  "title": "晨报播报 - 2026-07-06",
  "segments": [
    {
      "role": "host",
      "content": "各位投资者早上好，今天有哪些重要信息需要关注？"
    },
    {
      "role": "analyst",
      "content": "昨晚美股三大指数集体收涨，S&P500涨1.2%，纳指涨1.5%..."
    },
    {
      "role": "host",
      "content": "这对A股开盘有什么影响？"
    },
    {
      "role": "analyst",
      "content": "预计A股开盘偏暖，但需关注..."
    }
  ],
  "audio_urls": [],
  "duration": "3min"
}
```

**播报场景覆盖**：
- 晨报播报（morning_agent 结果）
- 异动提醒播报（alert_agent 结果）
- 风口龙头播报（wind_leader_agent 结果）
- 事件传导播报（event_chain_agent 结果）

**对话脚本示例：**
```
A: 早上好，今天的早点听来了。先看重磅消息——央行宣布降准0.5个百分点。
B: 这是个4级事件，影响面很广。降准意味着银行可贷资金增加，对银行股直接利好。
A: 那传导路径呢？
B: 银行→信贷扩张→基建/地产受益。你的持仓里宁德时代不在主要传导路径上，影响有限。
A: 再看长线风口，今天新能源板块有异动...
```

**前端音频播放实现（uni-app）：**

```typescript
// src/components/briefing/DualHostPlayer.vue
import { ref, onUnmounted } from 'vue'

const audioA = uni.createInnerAudioContext()  // 主持人A
const audioB = uni.createInnerAudioContext()  // 主持人B
const bgAudio = uni.getBackgroundAudioManager()  // 后台播放（App 端）

// 交替播放双人对话
async function playDualHost(segments: AudioSegment[]) {
  for (const seg of segments) {
    const ctx = seg.host === 'A' ? audioA : audioB
    ctx.src = seg.url
    await new Promise(resolve => {
      ctx.onEnded(resolve)
      ctx.play()
    })
  }
}

// #ifdef APP-PLUS
// App 端支持后台播放
bgAudio.title = '今日早点听'
bgAudio.src = mergedAudioUrl
// #endif
</script>
```

---

## 六、推送渠道设计

### 6.1 推送矩阵

| 渠道 | 场景 | 实现方式 |
|------|------|---------|
| **App 内推送** | 异动提醒、事件预警 | uni.push（个推/小米/华为）+ WebSocket 实时 |
| **微信智能体机器人** | 重磅消息、早报摘要 | 微信公众号模板消息 / 微信客服接口 |
| **双人对话播报** | 早晚报 | App 内音频播放（uni.createInnerAudioContext）+ 微信语音消息 |

### 6.2 微信智能体机器人

```
用户微信 ←→ 微信公众号/客服接口
                    │
                    ▼
              微信消息路由
                    │
          ┌────────┼────────┐
          ▼        ▼        ▼
      订阅推送   对话查询   早报摘要
      (主动)    (被动)    (定时)
```

### 6.3 App 推送实现（uni-app）

```typescript
// src/hooks/usePushNotification.ts
export function usePushNotification() {
  // #ifdef APP-PLUS
  // 注册推送服务
  uni.getPushClientId({
    success: (res) => {
      const clientId = res.cid
      // 上传 clientId 到后端
      pushApi.registerToken({ token: clientId, provider: 'unipush' })
    }
  })
  
  // 监听推送点击
  uni.onPushMessage((res) => {
    if (res.type === 'click') {
      const payload = res.data.payload
      // 根据 payload 跳转对应页面
      uni.navigateTo({ url: payload.page })
    }
  })
  // #endif
}
```

---

## 七、数据迁移与复用策略

### 7.1 可直接复用的服务（Skills 直接 import）

从 `aistock-api/src/services/` 直接被 Skills 调用，无需迁移：

| 服务 | 被哪个 Skill 复用 | 迁移方式 |
|------|-----------------|---------|
| `TencentQuoteService` | stock_quote | 同仓库直接 import |
| `TencentKlineService` | stock_quote | 同仓库直接 import |
| `TushareService` | capital_flow / valuation | 同仓库直接 import |
| `SinaMoneyFlowService` | capital_flow | 同仓库直接 import |
| `ThsService` | leader_stock | 同仓库直接 import |
| `FeishuResearchReportService` | research_report | 同仓库直接 import |
| `IndustryKGService` | knowledge_graph | 同仓库直接 import |
| `StockMonitorService` | alert_monitor | 同仓库直接 import |
| `CacheService` | 所有 Skill | 同仓库直接 import |
| `TradingCalendarService` | 所有 Skill | 同仓库直接 import |

### 7.2 需重构的模块

| 旧模块 | 重构方向 |
|--------|---------|
| `AiGraphService` → | 拆分为 Agent + Skills 架构 |
| `StockAnalysisAgentService` → | 纳入持仓守护官 Agent |
| `StockMonitorService` → | 纳入 alert_monitor Skill |
| `MessagePushService` → | 重构为统一推送服务 |
| `WechatPushService` → | 扩展为微信智能体机器人 |

### 7.3 缓存策略（保持旧项目约束）

| 缓存键格式 | TTL | 说明 |
|-----------|-----|------|
| `stock_quote:{level}:{symbol}` | 交易 60s/非交易到开盘 | 保持不变 |
| `briefing:morning:{date}` | 当日有效 | 新增 |
| `event:chain:{id}` | 24h | 新增 |
| `valuation:{symbol}` | 1h | 新增 |
| `agent:session:{id}` | 30min | 新增，对话上下文 |

### 7.4 前端代码复用（从 aistock-frontend 迁移）

| 类别 | 复用方式 | 复用率 |
|------|---------|--------|
| API 接口定义（api.js） | 拆分到 `api/modules/*.ts`，替换 axios 适配器 | 90% |
| Store 业务逻辑（Vuex actions） | 迁移到 Pinia actions | 80% |
| Utils（cacheManager、stockCycle 等） | 替换 localStorage → uni.setStorageSync | 70% |
| 组件业务逻辑 | 保留逻辑，替换 UI 组件 | 50% |
| 图表组件 | 需重写（echarts → uCharts） | 20% |

---

## 八、开发路线图

### Phase 1：骨架搭建 ✅

- [x] 创建前端仓库 `aistock-app-frontend`
- [x] 初始化 uni-app 项目结构和基础配置
- [x] 搭建后端 WebSocket 服务 + Agent 路由骨架
- [x] 前端 Tab 导航 + 三端页面骨架
- [x] 实现登录认证（小程序 wx.login + App token）

### Phase 2：Agent 推理层迁移 + 数据接口 ✅

- [x] 创建 Python Agent 服务仓库 `aistock-agent-py`
- [x] 实现 LangGraph 图拓扑（supervisor → 各 Agent 条件路由）
- [x] 实现 Node.js `/internal/*` 数据接口（6个，内网鉴权）
- [x] 实现 Python LangChain @tools（stock/sector/news/market）
- [x] Python tools mock 测试覆盖

### Phase 3：Morning Agent + 晨报（当前）

- [ ] 实现 Morning Agent（ReAct 模式 + deep_think）
- [ ] 实现 Redis 晨报缓存（TTL=2h）
- [ ] 实现 SSE 流式晨报接口
- [ ] 前端早点听页面对接晨报 API
- [ ] 晨报 4 步宏观分析框架提示词调优

### Phase 4：核心对话 Agent 层

- [ ] 实现 Supervisor 意图分类（quick_think）
- [ ] 实现 Stock/WindLeader/EventChain/General Agent
- [ ] 完整消息流程：输入→路由→工具调用→回复
- [ ] 前端 AI 对话页 + 流式消息渲染
- [ ] 前端 Skills 卡片组件

### Phase 5：Node.js 接入 + 新增 Internal API

- [ ] Express 反代 `/api/agent/*` 到 Python 服务
- [ ] 新增6个 `/internal/*` 接口（wind-leaders/monitor/tenx/graph）
- [ ] 新增 Python Tools（monitor_tools/tenx_tools）
- [ ] 端到端测试

### Phase 6：产品功能 Agent

- [ ] 实现 Alert Agent（异动提醒/持仓监控）
- [ ] 实现 Tenx Agent（十倍股/趋势股评分）
- [ ] 实现 Forecast Agent（业绩预测）
- [ ] 前端对应页面对接 Agent API
- [ ] docker-compose 编排（Express + Python）

### Phase 7：交易复盘 + 标准文档

- [ ] 实现 Review Agent（交易复盘）
- [ ] 编写 `AGENT_STANDARDS.md`（Agent 开发标准文档）
- [ ] 微信智能体机器人对接
- [ ] 性能优化 + 安全加固

### Phase 8：打磨与上线

- [ ] 研报精读 Tool 优化
- [ ] 性能优化 + 安全加固
- [ ] App 打包与发布（HBuilderX 云打包）

---

## 九、Harness 工程体系

> 详见 [Harness架构设计文档.md](./Harness架构设计文档.md)，此处为要点摘要

### 9.1 两套 Harness 体系

| 体系 | 目标 | 约束对象 | Token 策略 | 约束手段 |
|------|------|---------|----------|---------|
| **Dev Harness** | 规范团队 AI 编码工作流 | 开发者 + AI Agent | 不考虑 | 文档 + Linter + CI |
| **App Agent Harness** | 约束 App 内智能体运行 | App 内各 Agent | 精打细算 | 缓存 + 限流 + 精简 Prompt |

### 9.2 Dev Harness 核心规范

- 每个仓库必须有 `AGENTS.md`（~100 行入口地图）+ `docs/` 结构化文档目录
- 成员与 Agent 对话前必须填写结构化任务描述模板（业务意图 + 技术约束 + 验收标准）
- 5 阶段工作流：读地图 → 结构化输入 → 小步对话 → 人类复核 → 提交 + 回写文档
- 机械约束：自定义 ESLint 规则强制依赖方向和数据源约束；CI 门禁

### 9.3 App Agent Harness 核心规范

- 4 阶段管道：数据层(0 Token) → 筛选层(0 Token) → Harness 循环(少量 Token) → 输出层(0 Token)
- BaseHarness 基类：缓存查重 + Prompt 精简 + max_tokens 限制 + temperature=0 + 限速
- 单用户日均 Token 预算约 30000，月均成本约 2.4 元（DeepSeek）
- 异常策略：LLM 调用失败时跳过，返回纯数据不含分析，不重试（防烧钱）

---

## 十、技术约束与注意事项

| 约束 | 说明 |
|------|------|
| 行情数据 | 必须使用腾讯行情 API，保持 sessionFetch |
| K 线数据 | 必须使用腾讯 K 线 API |
| 北交所资金流向 | 使用新浪财经接口，需单位转换(元→万元) |
| 东方财富 | 不允许使用 |
| 龙头股数据 | 必须来自同花顺概念页面，失败返回空 |
| Tushare | 优先使用，积分充足(10000) |
| 缓存键格式 | 保持 `stock_quote:{level}:{symbol}` 格式不变 |
| AI 分析失败 | 不使用规则引擎回退，避免数据缺失 |
| 300/301 开头 | 均识别为创业板 |
| 前端条件编译 | App 专属功能必须放在 `pages-sub-app` 分包，H5/小程序不编译 |
| Skills 复用 | 优先复用现有 `services/`，禁止重复实现数据获取逻辑 |
| 向量检索 | 使用 pgvector，不引入独立向量数据库 |

---

## 附录：关键变更记录

### v2.0 → v3.0（2026-07-04）

| 变更项 | v2.0 | v3.0 | 变更原因 |
|--------|------|------|---------|
| Agent 推理层 | Node.js 内嵌（TS） | **Python 微服务（aistock-agent-py）** | 多 Agent 协作需要 LangGraph 状态机编排，Python 生态成熟 |
| Agent 编排 | 自研 Orchestrator | **LangGraph** | LangGraph 提供条件边、状态机、checkpointer |
| LLM 调用 | OpenAI SDK (TS) | **langchain-openai (Python)** | 与 LangGraph 原生集成 |
| 境外市场数据 | 无 | **yfinance (Python)** | 晨报需要美股/亚太/大宗/汇率数据 |
| 全网搜索 | 无 | **Tavily (Python)** | 晨报需要全网财经新闻搜索 |
| 数据接口 | Skills 直接 import Services | **/internal/* API（内网鉴权）** | Python 服务不拥有数据，通过 HTTP 回调获取 |
| 仓库数量 | 3 个 | **4 个**（新增 aistock-agent-py） | Agent 推理层独立部署 |
| 后端进程 | 1 个（Express） | **2 个**（Express + Python） | 推理与数据分离 |

### v1.0 → v2.0

| 变更项 | v1.0 | v2.0 | 变更原因 |
|--------|------|------|---------|
| 前端框架 | Expo RN | uni-app (Vue 3) | 团队 Vue 技术栈；三端同步更新诉求；小程序支持 |
| 状态管理 | Zustand | Pinia | 与 Vue 3 生态一致 |
| UI 方案 | NativeWind | wot-design-uni + SCSS | uni-app 生态适配 |
| 本地存储 | MMKV | uni.setStorageSync | 跨端一致；App 端可 uts 调原生 |
| 动画 | Reanimated 3 | uni.createAnimation + CSS | 金融工具对动画要求低 |
| 虚拟列表 | FlashList | uni-list + 自定义 | uni-app 生态适配 |
| 后端框架 | 新建 Koa | 复用 Express | 减少服务器进程；Skills 直接复用 services |
| 向量数据库 | Chroma | pgvector | 减少服务器组件；零增量 |
| 仓库数量 | 4 个 | 3 个 | 维护成本降低 |
| 推荐服务器 | 4C8G | 2C4G | 资源消耗降低 |

---

**文档结束**

> v3.0 保留 Agent + Skills 核心架构，将 Agent 推理层从 Node.js 内嵌迁移为 Python/LangGraph 微服务。
> 核心创新（智能体矩阵、双人对话播报、事件传导链、动态估值）全部保留，通过 LangGraph 实现多 Agent 协作编排。
