# AI Stock Vibecoding 工作流手册

> 本手册指导团队成员如何使用 Trae IDE + AI 进行协作开发

## 什么是 Vibecoding？

Vibecoding 是一种用自然语言描述需求、让 AI 生成代码的开发方式。你不需要精通编程，只需要：
1. 清楚地描述你想要什么
2. 检查 AI 生成的代码是否正确
3. 在 Trae IDE 中验证运行效果

## 工具准备

### 必需工具
- **Trae IDE**：AI 编程工具，下载地址 https://trae.com.cn
- **Git**：代码版本管理，Trae 内置

### 项目配置
1. 用 Trae 打开项目目录（如 `d:\aistock\aistock-app-frontend` 或 `d:\aistock\aistock-agent-py`）
2. Trae 会自动加载 `.trae/` 配置（Rules、Skills、Hooks）
3. 在对话窗口中直接用中文描述需求

---

## 工作流系统说明

项目使用两层 Rules + Skills 配合的架构：

### Rules 分工（不是优先级，而是职责分工）

| 规则文件 | 职责 | 说明 |
|---------|------|------|
| `aistock-workflow.md` | **流程控制**（做什么步骤） | 定义 9 步开发流程，告诉 AI "先做什么，后做什么" |
| `superpowers.md` | **Skills 触发**（用什么工具） | 定义触发条件，告诉 AI "遇到什么场景用什么 Skill" |

**关键设计**：两个规则配合使用，不是覆盖关系。

### TodoWrite 可视化追踪

AI 在执行任务时，会自动用 TodoWrite 在右侧边栏显示任务列表：

```
Phase 1: 上下文加载 ✅
Phase 2: 需求确认 ✅
Phase 3: 编码 ⏳
Phase 4: 跨端同步检查
Phase 5: 验证
Phase 6: 文档维护
Phase 7: 用户验收
```

**你可以实时看到 AI 正在做什么步骤**。

### 自动工作流（9 步）

当你提出开发需求时，AI 自动按以下流程执行：

| 步骤 | 内容 | 你需要做什么 |
|------|------|-------------|
| 1. 上下文加载 | 读取 README.md、模块 AGENTS.md、project_memory.md | 无需操作 |
| 2. 需求确认 | 小任务直接做，大任务等你确认 | 大任务时回复"确认" |
| 3. 编码 | AI 生成代码（遇到特定场景会调用对应 Skill） | 无需操作 |
| 4. 跨端同步检查 | 询问是否需要同步另一端前端 | 回答"需要"或"不需要" |
| 5. 验证 | 类型检查、运行验证（必须有证据） | 无需操作 |
| 6. 文档维护 | 更新 README、AGENTS.md 等所有功能文档 | 无需操作 |
| 7. 用户验收 | **先总结修改内容**，再询问满意度 | 回答"满意"或"需要小改" |
| 8. 技能缺口记录 | 记录到 skill-requests.md | 无需操作 |
| 9. 修改记录 | 自动追踪修改，commit 时整理写入 CHANGELOG.md | 无需操作 |

### 技能缺口反馈机制

AI 在开发中遇到没有合适 skill 的复杂问题时：
1. AI 自行完成
2. 记录建议到 `.trae/skill-requests.md`
3. 告知开发者："这个问题建议引入 XXX 类型的 skill"
4. 组长定期查看 skill-requests.md，引入新 skill
5. 技能库随团队需求持续丰富

### 修改记录机制

- 从你 `git pull` / `git clone` 开始，每次修改文件自动记录到临时文件
- 当你说"帮我 commit"时，AI 整理所有修改写入仓库的 `CHANGELOG.md`（标注分支、时间区间、开发者）
- 所有团队成员可以查看 `CHANGELOG.md` 了解其他人做了哪些修改

---

## 组长工作流

### 1. 项目规划

```
请帮我规划下周的开发任务，当前优先级：
1. 早点听页面完善
2. AI 对话功能对接
3. 自选股列表优化
```

### 2. 任务分配

根据模块负责人分配表，将任务指派给组员：

```
请帮我创建一个任务清单：
- 首页模块（小王）：完善早点听页面
- AI 对话模块（小李）：对接 Agent API
- 自选股模块（小张）：优化列表样式
```

### 3. 共享层维护

组长负责 `shared/` 目录的维护：

```
请在 shared/types/ 下新增一个 StockAlert 类型定义，
包含 symbol、name、price、changePercent、alertType 字段。
```

### 4. 代码审查

```
请帮我审查 modules/favorites/ 下的代码，
检查是否有跨模块 import 或违反约束的问题。
```

### 5. 查看技能缺口

```
请查看 .trae/skill-requests.md，汇总团队需要哪些新 skill
```

### 6. 查看团队修改记录

```
请查看各仓库的 CHANGELOG.md，汇总最近所有人的修改
```

---

## 组员工作流

### 1. 认领任务

你负责的模块目录是固定的，只在你的模块目录下写代码：

| 你的模块 | 前端目录 | 后端目录 | Agent 目录（Python） |
|---------|---------|---------|---------------------|
| 首页/行情 | modules/home | modules/quote | - |
| 自选股/监控 | modules/favorites | modules/monitor | - |
| AI对话/Agent | modules/chat | modules/agent | aistock-agent-py/src/aistock_agent/agents/ |
| 资讯/推送 | modules/news | modules/push | - |
| 用户/认证 | modules/user | modules/auth | - |
| 龙头/爬虫 | modules/market | modules/crawler | - |
| 晨报/分析 | - | - | aistock-agent-py/src/aistock_agent/agents/morning_agent.py |
| 事件传导 | - | - | aistock-agent-py/src/aistock_agent/agents/event_analyst.py |

### 2. 用 Trae 开发

**好的描述（AI 能准确理解）：**
```
请在 modules/favorites/pages/ 下新增一个特别提醒详情页，
显示单只股票的所有异动事件列表，每个事件有时间、标签、描述。
使用 PageCard 组件包裹，数据通过 API 获取。
```

**不好的描述（AI 猜不到你的意图）：**
```
帮我加个页面
```

### 3. 模块边界规则

**绝对不能做的事：**
- ❌ 不要 import 其他模块的文件
- ❌ 不要修改 shared/ 目录下的文件（找组长改）
- ❌ 不要修改其他人的模块目录

**应该做的事：**
- ✅ 通过 shared/types/ 中定义的接口跨模块通信
- ✅ 复用 shared/components/ 中的全局组件
- ✅ 复用 shared/api/ 中的 API 方法
- ✅ 复用 shared/utils/ 中的工具函数

### 4. 提交代码

```
请帮我 commit 代码
```

AI 会自动：
1. 整理修改记录到 CHANGELOG.md
2. 生成 commit 消息
3. 执行 git add + commit

如果需要推送：
```
请帮我 push
```

### 5. 常见问题

**Q: 我的模块需要用到另一个模块的数据怎么办？**
A: 两个选择：
1. 在 shared/types/ 中定义一个接口，两个模块都通过这个接口读写数据
2. 通过页面路由参数传递数据

**Q: 我需要新增一个全局组件怎么办？**
A: 请找组长，在 shared/components/ 中添加。

**Q: AI 生成的代码报错了怎么办？**
A: 直接把错误信息贴给 AI。

**Q: 我不确定改了会不会影响别人？**
A: 只要你只改自己模块目录下的文件，就不会影响别人。如果需要改 shared/ 中的文件，先找组长确认。

**Q: 怎么启动开发服务？**
A: 在 Trae 中说"启动前端服务"或"启动后端服务"。Python Agent 服务说"启动 Python Agent 服务"。

**Q: 我需要在 Python Agent 服务中新增一个 Tool 怎么办？**
A: 在 `aistock-agent-py/src/aistock_agent/tools/` 下对应的文件中添加 `@tool` 函数，然后在 `api/routes.py` 的 `list_skills` 中注册。必须编写 mock 测试。详细规范见 `aistock-agent-py/AGENTS.md`。

**Q: AI 说"建议引入 XXX skill"是什么意思？**
A: AI 遇到了一个没有现成 skill 辅助的复杂问题，自己解决了但记录了建议。这不影响你的开发，组长会定期查看这些建议并引入新 skill。

**Q: 怎么看其他人改了什么？**
A: 查看各仓库的 `CHANGELOG.md`，按时间倒序记录了所有人的修改。

---

## 附录

### 常用指令速查

| 想做什么 | 在 Trae 中怎么说 |
|---------|-----------------|
| 开发新功能 | "请在 modules/xxx/pages/ 下新增 XXX 页面" |
| 修 Bug | "XXX 页面的 YYY 功能有问题，报错信息是 ZZZ" |
| 改样式 | "请调整 XXX 页面的 YYY 样式" |
| 对接 API | "请把 XXX 页面对接到 GET /api/yyy 接口" |
| 启动服务 | "启动前端服务" / "启动后端服务" |
| 检查类型 | "运行 tsc 检查" |
| 提交代码 | "帮我 commit" |
| 推送代码 | "帮我 push" |
| 查看修改记录 | "查看 CHANGELOG.md" |
| 查看技能缺口 | "查看 skill-requests.md" |

### 项目目录速查

| 目录 | 内容 | 谁能改 |
|------|------|--------|
| shared/ | 全局组件、类型、API、工具 | 组长 |
| modules/home/ | 首页模块 | 首页负责人 |
| modules/favorites/ | 自选股模块 | 自选股负责人 |
| modules/chat/ | AI 对话模块 | AI 对话负责人 |
| modules/market/ | 行情模块 | 行情负责人 |
| modules/user/ | 用户模块 | 用户负责人 |
| modules/news/ | 资讯模块 | 资讯负责人 |
| pages.json / router/ | 路由配置 | 组长 |
| aistock-agent-py/agents/ | Python Agent 推理层 | Agent 负责人/组长 |
| aistock-agent-py/tools/ | Python LangChain @tools | Agent 负责人/组长 |
| aistock-agent-py/prompts/ | 提示词模板 | Agent 负责人/组长 |

### 相关文档
- 架构设计文档：`AI投资App架构设计文档.md`（v3.0 — Python/LangGraph Agent 微服务）
- Agent 重构设计文档：`aistock-agent-py/docs/refactor-plan.md`
- Agent 开发规范：`aistock-agent-py/AGENTS.md`
- 模块化设计文档：`docs/superpowers/specs/2026-07-02-pluggable-module-architecture-design.md`
- 工作流设计文档：`docs/superpowers/specs/2026-07-02-aistock-workflow-v2-design.md`
- 项目主流程 Rules：`.trae/rules/aistock-workflow.md`
- Superpowers Rules：`.trae/rules/superpowers.md`
- 技能缺口日志：`.trae/skill-requests.md`
- 修改记录（临时）：`.trae/changelog-pending.md`
