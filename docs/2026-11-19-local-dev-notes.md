# 本地开发注意（2026-11-19）

> 写在这批框架升级之后。`changer` 上已合入 langgraph 1.x 升级，本地 venv 也是 1.x ——
> 在这之上继续开发，先花三分钟看完 §3（最容易踩的一条）。

---

## 1. 分支与锁定位置

| 用途 | 分支 / tag | 说明 |
|---|---|---|
| 日常开发 | `changer` | 共享集成分支，多人/多会话都往上提 |
| **本批次锁定** | tag `agent-framework-20261119`<br>branch `release/agent-framework-20261119` | 都指向 `a62c9fc`（5 个提交），**不要在这个 release 分支上继续提交** |
| 周末部署 | `release/agent-framework-20261119` → `main` | **不要用 `changer` 开这个 PR** |

为什么周末部署不能从 `changer` 开 PR：`changer` 会持续收到新提交，从它开的 PR 会把
本批次之外的开发一起带上去（历史教训：frontend #152 把节奏重构和 chat 修复混成一个 PR）。

---

## 2. 环境与命令

本地 venv 已升级，当前版本：

```
langgraph 1.2.22 / langchain 1.5.0 / langchain-core 1.8.2 / langchain-openai 1.8.1
langgraph-checkpoint 4.3.2 / langgraph-checkpoint-sqlite 3.1.3
openai(SDK) 3.55.0
```

**同步依赖**（改过 pyproject 或换了分支后）：

```bash
uv sync --extra dev          # 必须带 --extra dev
```

> 不带 `--extra dev` 会把 `pytest` / `ruff` / `mypy` 卸掉 —— 因为它们在 optional-dependencies 里。

**跑测试必须带 `--basetemp`**：

```bash
.venv/Scripts/python.exe -m pytest tests/unit -q -p no:cacheprovider --basetemp=.pytest_tmp/run
```

不带会被沙箱的批量删除守卫杀掉（pytest 收尾时会 rmtree 系统临时目录下的编号目录，
守卫直接 `SystemExit: 1`），表现为"测试跑了很久没输出然后 exit 1"——不是测试失败。

**其他常用**：

```bash
.venv/Scripts/python.exe -m ruff check src/
.venv/Scripts/python.exe -c "import aistock_agent.main; print('ok')"   # 导入冒烟
```

**版本自检**（怀疑环境不对时）：

```bash
.venv/Scripts/python.exe -c "
import importlib.metadata as md
for p in ['langgraph','langchain','langchain-core','langchain-openai','langgraph-checkpoint-sqlite','openai']:
    print(f'{p:32s}{md.version(p)}')"
```

---

## 3. ⚠️ LangGraph 会静默丢弃 state 未声明的返回键

**最容易踩、且完全没报错的一条。**

```python
class S(TypedDict, total=False):
    a: str

async def node(state):
    return {"a": "x", "cached": True, "audio_path": "/tmp/x.mp3"}

# 实测（langgraph 1.2.22）：终态 = {"a": "x"}，cached 与 audio_path 凭空消失
# stream_mode="updates" 也只剩 a —— 没有任何 warning
```

**影响**：各 worker 的 `run` 返回大量 `AgentState` 未声明的键（`cached` /
`morning_persisted` / `audio_path` / `event_id` / `report_id` / `podcast_brief` …），
scheduler 与下游都在读这些键。

**结论（已写进 `services/task_runner.py` 模块注释，别改回去）**：

- **不要**为了"拿检查点/重试"把 worker 套进图 —— 套图 = 悄悄改它的出参。
- 定时任务走 `task_runner` 的**直调 + 外壳**，出参零失真。
- 若哪天确实需要某个 agent 进图，前提是先把 `AgentState` 补齐成能容纳它的出参，
  并给每个键声明 reducer —— 那是独立一件事，别顺手做。

护栏测试：`tests/unit/test_task_runner.py::test_undeclared_state_keys_are_preserved`。

---

## 4. 新增 agent 必须改 registry

`agents/registry.py` 是 agent 的单一真源（15 个），驱动三处：supervisor 图节点、
intent 路由、chat 深度升级。

新增一个 agent 的步骤：

1. 写 `agents/workers/<name>.py`，暴露 `async def run(state: AgentState) -> dict[str, object]`
2. 在 `agents/registry.py` 的 `_SPECS` 里加一条 `AgentSpec`
3. 若要在**交互式对话**里被路由到 → 填 `intents=(...)`
4. 若要在**定时任务**里跑 → `schedulable=True`，并在 `scheduler.py` 用
   `_run_agent(job_id, "<name>", state)` 调（它会读灰度开关）

**三条硬约束**：

- **`node_name` 不能随便改**：`api/ws.py` 的 `_NODE_LABELS`（前端过程提示）和
  `tests/unit/test_constants.py` 的路由断言都按节点名匹配。
- **`retryable` 只在"真会把异常抛到图上"的 agent 上设 True**：目前只有 general/alert
  （内部是 `create_react_agent`）。其余 worker 的 `run` 顶层自带 try/except，挂重试
  等于挂个永不触发的装饰。
- **意图 `review` 恒由 `general_agent` 兜底**，不是 `review` agent。这是有意的，被
  `tests/unit/test_constants.py:29-43` 锁死（user / scheduler 两种 trigger 都要 general）。

---

## 5. 测试里 patch agent 的坑

`registry` 在 **import 时**就把 `spec.run` 固化成原始函数对象了。之后再
`monkeypatch.setattr(morning_agent, "run", fake)` 只改模块属性，**读 registry 拿不到 fake**。

- 写 patch：优先 patch **源模块属性**（`patch("aistock_agent.agents.workers.morning.run")`
  或 `patch("...workers.morning")` 换整个模块），这是仓库既有约定。
- `scheduler._run_agent` 的 legacy 路径特意用
  `importlib.import_module(...)` + `getattr(包, 名)` 取模块，就是为了和 patch 对齐。
  **不要"优化"成从 registry 取** —— 实测代价：`tests/unit` 从 90s 变成卡死 5 分钟以上
  （打到了真实 agent 的网络调用）。

---

## 6. 流式事件名是跨模块字面量

`synth_answer` 用常量 `_STREAM_DELTA_EVENT` 发事件，`api/ws.py` 用字面量
`"chat_content_delta"` 收 —— 两份互不引用的字符串，改名不会报错，只会静默丢流式内容。

改名要三处同改：`graph/nodes/synth_answer.py` 常量、`api/ws.py` 的 `on_custom_event`
分支、`tests/unit/test_streaming_contract.py`。

---

## 7. 新定时任务怎么接执行记录

两个开关（默认关 = 改造前行为）：

```ini
SCHEDULER_TASK_RUNNER_ENABLED=false
SCHEDULER_TASK_RUNNER_TASKS=            # 逗号分隔 job id；空=全部
```

已接入的 job id：`morning_briefing`、`midday_briefing`、`midday_broadcast`、
`review_full`、`iterate`、`rhythm_master_{after_close|morning|midday|tail}`。

- **agent 任务** → `_run_agent(job_id, agent_name, state)`
- **数据/计算任务** → `task_runner.run_data_task(job_id, body)`
- 查询执行记录：`GET /api/agent/internal/tasks/runs?task_id=&date=`（`X-Internal-Token`）

---

## 8. 已知债（是老问题，别当成自己引入的）

| 位置 | 问题 |
|---|---|
| `graph/nodes/qa_router.py` | 2133 行，12 道闸门靠 if 顺序表达优先级；已规划拆分，**押后到本轮框架层稳定之后** |
| `graph/nodes/synth_answer.py` | 1268 行 |
| 交互式确认 | 用"重跑整张图"模拟暂停，不是 `interrupt()` |
| `services/` | 90+ 个扁平文件，业务逻辑与编排混放 |
| `api/routes.py` | 2290 行 |
| `AGENTS.md` / `CHANGELOG.md` | 170KB / 338KB，已超出可读边界，建议按季度归档 |

---

## 9. 提交前自检

```bash
.venv/Scripts/python.exe -m ruff check src/
.venv/Scripts/python.exe -m pytest tests/unit -q -p no:cacheprovider --basetemp=.pytest_tmp/run
```

- 基线是 **4547 passed, 1 skipped**（2026-11-19）。掉数量就是改坏了。
- **不要为了让测试过而改现有断言** —— 尤其 `test_constants.py` 的意图集合与 review 路由，
  那是有意为之的产品行为。
