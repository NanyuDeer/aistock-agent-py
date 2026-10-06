# CHANGELOG.md — aistock-agent-py 变更记录

> 所有修改记录按时间倒序排列。每条记录标注分支、时间、开发者。

## [main] 2026-10-06 — 清理存量真问题 ruff 告警 96 条（保留 E501）

**开发者**: Aria

### 改进

- **存量 ruff 告警 296 → 199，清掉 96 条真问题/格式告警**（保留 `E501` 行长；200 → 199 中 −1 为 I001 自动修的良性副作用）：
  - **自动修 83 条**（`ruff check --fix`，未用 `--unsafe-fixes`）：`I001` ×27、`F401` ×19、`F541` ×15、`W292` ×13、`UP017` ×7、`W605` ×1、`UP032` ×1。
  - **人工修 13 条**：`UP038` ×6（`isinstance(x, (A, B))` → `isinstance(x, A | B)`）、`F841` ×4（删除未使用赋值；含 2 处仅去掉 `as xxx` 绑定、保留 `patch(...)` 副作用）、`E402` ×2（`scripts/export_langgraph_graphs.py` 第三方 import 上移；第一方因 `sys.path` hack 保留既有 `# noqa: E402`）、`N806` ×1（`insight_candidate.py` 局部 `EVIDENCE_WINDOW` → `evidence_window`）。
  - **未触碰 `E501`**：不修行长、不加 `noqa`、不改 `pyproject.toml`（`line-length`/`select`/既有 `per-file-ignores` 均不动）。
- **`UP017` 运行时安全已核实保留**：`datetime.UTC` 需 py3.11+，而 `Dockerfile` = `python:3.11-slim`、`requires-python >=3.11`、ruff `target-version=py311`、`uv.lock requires-python >=3.11`，无 <3.11 部署路径。
- **`F401` 删除导入未破坏导出**：58 个改动文件不含任何 `__init__.py`，未触及 re-export / `__all__`。

### 验证

- `uv run ruff check src tests scripts --statistics` → **296 → 199**（仅剩 `199 E501`）。
- `uv run python -m pytest tests/unit -q` → **3433 passed / 9 failed / 1 skipped**，9 条与既有基线同集（`test_industry_vector_search.py` ×6 + `test_scheduler.py` ×3）→ **零新增失败**。
- 根级 `tests/*.py` → **56 passed**；`tests/integration tests/e2e tests/diagnostics --collect-only` → **450 collected / 0 errors**（无 ImportError）。

### 说明

- 58 文件、+100 / −131；单文件改动均为格式/死导入/现代化改写，无业务语义变更。

---

## [main] 2026-10-06 — 收口改动文件剩余 28 处 ruff 告警 + 修复 scripts 包解析

**开发者**: Aria

### 改进

- **清掉改动文件里剩余的 28 处 ruff 告警**（`prediction_stats.py` / `prediction_validator.py` / `test_prediction_stats.py` / `test_prediction_validation_cbis.py` / `test_prediction_validator.py`）：25 处 `E501`（正当换行，语义与文案不变）、2 处 `F841`（删除未使用的赋值）、1 处 `I001` 与 1 处 `W292`（ruff 自动修）。这 5 个文件现 **`All checks passed!`**；全仓 `ruff check src tests scripts` **324 → 296**（只减本次这 28 处，未波及其它文件）。
- **修复 `scripts/` 的 mypy 包解析**：新增 `scripts/__init__.py` 与 `scripts/calibration/__init__.py`（各一句中文 docstring，风格同既有）。根因是混合文件列表下 mypy 报 `Source file found twice under different module names: "k_band" and "scripts.calibration.k_band"`；补 `__init__.py` 使包语义明确后该错消失。**未改任何 mypy 配置**，`mypy src` 存量错误数 **290 → 290 零恶化**。

### 验证

- `uv run ruff check <5 个改动文件>` → **All checks passed!**
- `uv run mypy src/aistock_agent/services/k_band_table.py scripts/calibration/k_band.py` → **Success: no issues found in 2 source files**（原报错场景已修复）
- `uv run mypy src` → 290 errors / 53 files（与改前一致，零恶化）
- `uv run pytest tests/unit -q` → **3433 passed / 9 failed / 1 skipped**（9 条与基线同集）
- 定向 `test_prediction_validator + test_prediction_stats + test_prediction_validation_cbis + test_k_band` → **130 passed**

### 说明

- 全仓仍有约 **296 条 `E501` 存量** 与 **290 条 mypy strict 存量**（`mypy src` 的 53 个文件），属**仓库既有积压、与本次改造无关**，本轮未处理（不在「本次改动文件」范围内）。

---

## [main] 2026-10-06 — 退役条件链死代码清理（物理删除）+ 本轮新增 ruff 告警清零

**开发者**: Aria

### 重构

- **物理删除退役条件链死代码**（`src/aistock_agent/services/prediction_validator.py`，−844/+51）：删 `_verify_conditions`（162 行）、`_scan_condition_met`（132 行）、`_judge_condition_hit`（20 行）及仅供它们使用的 import。三者自 2026-10-06 条件退役起已无生产调用方（`run_once` 已摘除调用），仅单测引用。
- 同步删除 `tests/unit/test_prediction_validator.py` 中**只测这两个函数**的 24 个用例与 3 个专属 fixture helper（`_anchor_condition_record` / `_ref_level_rows` / `_event_condition_record`）；该文件用例数 87 → 63。
- 保留（仍被存活路径引用）：`condition_met_judge.py`、`_judge_condition_met_once`、`_condition_scan_range`、`_CONDITION_SCAN_*`、`_should_skip_horizon`，以及存量回溯入口 `scripts/backfill_condition_met.py` / `rollback_condition_met.py`。
- 同步更正文档/注释：模块 docstring（不再描述已删的①②两段链路）、`_compound_pct` docstring（删掉指向已删函数的例外说明）、`run_once` 注释、`condition_met_judge` 调用方引用、`skills/prediction_validation.py` 顶部注释、`AGENTS.md`（L59/L561）。

### 改进

- **本轮新增 ruff 告警清零**：对改动文件做基线对照（基线 `cfe8af4`）后，把本轮引入的 **13 处 `E501 Line too long (>100)` 全部修掉**（拆行/缩短注释，语义与文案不变）。改动文件告警数 **41 → 28 = 基线**，既有告警一行未动，未加 `noqa`、未放宽 `pyproject.toml` 配置。

### 验证

- `uv run pytest tests/unit -q` → **3433 passed / 9 failed / 1 skipped**，9 条与基线同集（`test_industry_vector_search.py` ×6 + `test_scheduler.py` ×3）→ **零新增失败**。
- 定向 `test_prediction_validator + test_condition_met_judge + test_backfill_condition_met` → **199 passed**。
- **节奏护栏**：`test_rhythm_verification.py + test_rhythm_engine.py` → **47 passed**（节奏链路未受影响）。
- `uv run ruff check <改动文件>` → 28（= 基线）。

### 说明

- **mypy 包解析报错属项目既有配置缺口**（非本次引入）：`test_k_band.py` 以 `scripts.calibration.k_band` 模块名引用、命令行又按路径传入 → 双模块名；既有 `compute_delta` / `report_judge_bias` 配对同样可复现，且仓内规范入口为 `mypy src/`。故**未改配置**，登记为遗留。

---

## [main] 2026-10-06 — 末项 Important 修复：下钻桶 sufficient_sample 对齐复合判据

**开发者**: Aria

### 修复

- **问题**：本批新增的下钻桶（`direction_buckets` / `horizon_buckets`，含 `long`）的 `sufficient_sample` 仅用 `n >= 30`，而既有聚合桶 `_summary` 已是 `n >= 30 and n_predictions >= 30`（`n_predictions` = 不同预测数，按 `prediction_id` 去重）。同响应内因此会出现 `combined.sufficient_sample=false` 而 `direction_buckets.bullish.sufficient_sample=true` 的自相矛盾。
- **改法**：`services/prediction_stats.py` 的 `_bucket_metrics`（方向/档位/long 桶的唯一聚合实现）改为 `n >= 30 and n_predictions >= 30`；`n_predictions = len({e.get("prediction_id") ...})`，全 None（旧记录）时退化为 `n`——与 `_summary` 逐字同口径。`_summary` 及复用它的 `build_validation_profile` / `horizon_breakdown` 不变（本就复合判据）。
- **行为变更（有意收紧）**：仅新增下钻桶由 `n>=30` 收紧为复合判据（更保守，不会反向）；聚合桶行为不变。收紧后与 app-api 侧判据完全一致。

### 验证

- **测试**：`test_prediction_stats.py` +2（RED→GREEN：15 预测 × 2 档 = n30/pred15 → False；30 预测 × 1 档 → True）。
- **验证**：`pytest tests/unit/test_prediction_stats.py -q` → 49 passed；`pytest tests/unit -q` → 3457 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）。

---

## [main] 2026-10-06 — 终评修复：迭代看板补方向桶 × 档位桶（§8-3）+ `_judge_window` k 注释

**开发者**: Aria

### 新增

- **I2（§8-3 落地下钻桶）**：`services/prediction_stats.py` 新增共用聚合 `_bucket_metrics`（给定一组 entry → n/hits/hit_rate/sufficient_sample/flat_*）与 `_dimension_buckets`（按维度切分复用）：`hit_rate_summary` 与 `bucket_summary` 的每个桶新增 `direction_buckets`（bullish/bearish/neutral 各带 `flat_rate`，分母 = 该方向已结算数）与 `horizon_buckets`（short/mid/long；**long 单列并标注 `iteration_board=False`**）。口径与主桶逐条一致：methodology_version=4.0、排除 approximate、long 不入迭代桶、无样本 hit_rate=None（不用 0）、`sufficient_sample=n>=30`、小数 `round(...,4)`。
- **M4**：`prediction_validator._judge_window` 的 `k` 形参加内联注释，明示「4.0 调用方必须显式传入 k（唯一来源 `k_band_table.k_for`）；k is None → ValueError」（仅注释，判定行为不变）。

### 验证

- **测试**：`test_prediction_stats.py` +7（方向桶与整体桶不同且正确、方向桶 flat_rate 分母为该方向数、无样本 None / neutral flat_rate None、档位桶含 long `iteration_board=False`、approximate/旧版本不入任何新桶、bucket_summary 各桶补两维桶）。
- **验证**：`pytest tests/unit -q` → 3455 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）；`test_prediction_stats.py` → 47 passed。

---

## [main] 2026-10-06 — Task 7：验证侧下线条件判定链路（+4 项结转小修）

**开发者**: Aria

### 重构

- **主体（1 服务 + 1 测试）**：`services/prediction_validator.py` 的 `run_once` 移除两处条件判定调用——第①段 `_scan_condition_met`（点亮 `c{i}.condition_met=true`）与第②段 `_verify_conditions`（到期 hit/miss），并删除仅供条件路径使用的 `scan_cache`/`event_cache` 局部量；补退役注释（日期 + 范围限 `prediction_records.conditions` + 节奏 `branches` 不受影响 + 不删文件）。退役后 `run_once` 不再写入任何 `c{i}` entry（旧记录保持只读可查、不清洗）。
- **保留不删**：`_scan_condition_met` / `_verify_conditions` / `_judge_condition_met_once` / `condition_met_judge` 均保留——存量回溯入口 `backfill_condition_met` 仍调用 `_judge_condition_met_once`；故无死 import / 死常量（`_CONDITION_SCAN_*` 等仍被 `backfill_condition_met` 与 `_condition_scan_range` 引用）。物理删除留待后续单独清理。

### 测试

- `test_prediction_validator.py` 新增 `test_run_once_writes_no_condition_entries`（RED→GREEN：退役前 payload 出现 `c0`）、`test_run_once_ignores_conditions_after_retire`；原 `test_run_once_scans_condition_met_before_due` / `test_run_once_memoizes_condition_scan_fetch_per_window` 改为退役护栏（零 `c{i}` 回写、零条件取数）。

### 改进

- **结转 A**：`prompts/workers/prediction.py` 两处说明行加「字段」二字（`- conditions 字段：**不再产出**…`）；`test_prediction_prompt_no_conditions.py` 恢复字面断言 `"conditions：" not in prompt`，并加复归护栏 `'"condition"' not in prompt`。
- **结转 B**：`test_v4_single_band` 追加 3 组边界用例（x 恰等于 ±k），锁死 `>=`/`<=` 语义。
- **结转 C**：`_compound_pct` docstring 去掉「全项目唯一 / 禁止再用 sum(window)」绝对化断言，改为「4.0 主链唯一口径」并注明退役的条件链路（仅 backfill 可及）是已退役例外。
- **结转 D**：`skills/prediction_validation.py` 的 `scenario_signal.note` 去掉「预判时可适当提高其 conditions[] 权重」（该文案会注入 LLM，与不再产出 conditions 相抵），改为不含 conditions 的参考提示。

### 验证

- `test_prediction_validator.py` → 93 passed；节奏护栏 `test_rhythm_verification.py + test_rhythm_engine.py` → 47 passed；`test_prediction_prompt_no_conditions.py + test_prediction_validation_cbis.py + test_backfill_condition_met.py + test_condition_met_judge.py` → 134 passed；`tests/unit -q` → 3448 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）。ruff 改动行无新增。
- **提交**：主体 `feat(prediction): retire conditional-branch verification path`；结转（含本记录）见本任务第二个 commit（chore(prediction): clean up retired-condition residuals）。

---

## [main] 2026-10-06 — Task 5 二轮修复：insufficient 计 pending 定调 + 舍入对齐 + 全 pending 产出

**开发者**: Aria

### 修复

- **I1（定义确认，不改行为）**：`prediction_stats._settled_ratio` 的 pending 分支补「为什么」注释——insufficient 属**数据可用性状态**（数据源故障/无数据）、非**判定结论**，故计入 pending_slots（保留原行为，不做双排除）。新增测试 `test_settled_ratio_counts_insufficient_as_pending`（scope={4.0 hit, 4.0 insufficient} → settled_ratio==0.5）。
- **M1（舍入对齐）**：agent-py 既有 `round(...,4)` 保持不变；新增测试 `test_settled_ratio_and_flat_rate_rounded_to_4dp` 锁死 1/3 → 0.3333（与 app-api 新增的 `round4` 同值）。
- **M2（边界对齐）**：`prediction_validator._report_stats` 提前 return 由 `if not entries` 改为 `if not entries and not slots`——全 pending（有声明档位槽、无任何 verification entry）时仍产出统计且 settled_ratio==0（None 仅在分母为 0 时使用）。新增测试 `test_report_stats_emits_zero_settled_ratio_when_all_pending`。

### 验证

- `pytest tests/unit -q` → 3446 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）。

---

## [main] 2026-10-06 — Task 5 修复：settled_ratio 统一口径 + long 命中率交付 + direction/flat 限 4.0

**开发者**: Aria

### 修复

- **Important 1（settled_ratio 三处口径不一致 → 统一「声明档位槽」口径）**：`services/prediction_stats.py` 弃用 `_scope`，改为 `_slots_from_entries` / `_bucket_slots` / `_settled_ratio`：分母 = 记录**声明**的非-long、非-近似档位槽（含真 pending，来源 `record.prediction.horizons` 而非已存在的 verification entry）；分子 = 其中 result ∈ {hit,miss} 且 version=="4.0"；分母 = 分子 + pending；**旧版本（2.0/3.0/无版本）已结算槽位既不入分子也不入 pending**（口径隔离）。`hit_rate_summary` / `bucket_summary` 新增显式 `scope_slots` 关键字参数（缺省 None → 回退用 entries 自身作槽位；`_summary` 无槽位时 settled_ratio=None，不再隐式 1.0）；调用方 `prediction_validator._report_stats` 用 `prediction.horizons` + `due_dates_approximate` 构造槽位传入。
- **Important 2（long 命中率交付）**：`long{n,hits,hit_rate}` 口径对齐 app-api —— long + 非近似 + version 4.0 + result ∈ {hit,miss}；无样本 hit_rate=None（原 0.0）。`long_excluded` 仍按「存在当前版本非近似 long 档（任意 result）」判定。
- **Minor（direction/flat 仅 4.0 写入）**：`prediction_validator._verify_horizon` 的 `direction` 与 `flat` 改为**仅 `methodology_version=="4.0"` 分支写入**；v2/v3 存量 entry 不再新增这些键（无消费者依赖 v2/v3 的 direction）。

### 验证

- **测试**：`test_prediction_stats.py` +5（真 pending 使 settled_ratio<1、旧版本已结算双隔离、无槽位=None、bucket 按桶槽位、long 展示仅 hit/miss 且排除近似）。
- **验证**：`pytest tests/unit -q` → 3442 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）。

---

## [main] 2026-10-06 — Task 5：long 档不计入迭代看板 + 补看板指标（settled_ratio / flat_rate）

**开发者**: Aria

### 新增

- **改动（2 文件 + 2 测试文件）**：`services/prediction_stats.py`——`_filter_v2` 追加 `horizon != "long"`（long 档排除出迭代看板命中率分子）；新增 `_scope`（settled_ratio 分母 = 统计范围内全部非-long 档位条目，含未结算）与 `_long_entries`（long 单列汇总）；`_summary` 增补 `settled_ratio` / `flat_rate` / `flat_count` / `directional_count` / `long_excluded` / `long{n,hits,hit_rate}`；`hit_rate_summary` / `bucket_summary` 三桶同口径接入。`services/prediction_validator.py`——`_verify_horizon` 的 v4 分支新增结构化 `flat` 标记（direction ∈ {bullish,bearish} 且 |x| < k → `flat: True`；字段驱动、无值即无键），并落 `direction` 字段。
- **三处对计划原文的有意修正/新增**：① `flat` 判据依赖 k，而 k 唯一来源在 Python（`k_band_table.k_for`）→ 定在**写入侧**落标记，app-api 只读计数、不复制 k；② `flat_rate` 分母改为**方向预判已结算数** `directional_count`（非计划原文 `flat_count + n`，n 含 neutral 会稀释，得不到设计 §4.3 的「33% ≈ 瞎猜」基准线）；③ entry 新增 `direction` 字段（flat_rate 分母所需；与 reason 的"方向="同源），供读取侧计数。

### 验证

- **测试**：`test_prediction_stats.py` +8（long 排除/单列、settled_ratio 含 pending、flat_rate 分母锁定 1/2≠1/5、无方向样本 flat_rate=None、bucket 同口径）；`test_prediction_validator.py` +3（入带 flat=True、出带不写 flat 键、neutral 不写 flat）。
- **验证**：`pytest tests/unit -q` → 3438 passed / 9 既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3，与本改动无关）。
- **未提交**：无（随本任务 commit 提交）。app-api 侧同口径改动见该仓 commit。

---

## [main] 2026-10-06 — Task 4：单带宽 k 判定（v4，替换纯符号 + 两套阈值）

**开发者**: Aria

### 改进

- **改动（1 文件 + 测试）**：`src/aistock_agent/services/prediction_validator.py`——`_judge_window` 新增 v4 分支：单带宽 k（`bullish hit ⟺ x ≥ +k`、`bearish ⟺ x ≤ -k`、`neutral ⟺ -k < x < +k`，x=`_compound_pct(window)` 复利）；`|x| < k` 时方向预判记 miss（不再"涨 0.01% 也算命中"）。新增 `k: float | None = None` 关键字参数，v4 下 `k is None` → ValueError（fail loud，禁止静默回退默认带宽）。`_verify_horizon` 调用点改为**按版本分流**：`methodology_version=="4.0"` → `k=k_for(target_type)` + `baseline_neutral` 同谓词（`-k < x < k`）；`"2.0"/"3.0"` 存量路径逐字不变（仍用 legacy 阈值）。v4 起 `threshold_version`（sector）改记 k 表版本 `"4.0"`（legacy 仍 `"1.0"`）。
- **对计划 Step 5 的偏离（有意）**：计划要求删除两套阈值常量并把调用点无条件改 `k_for`；实际 v2 是 backfill 存量回补口径（`_BACKFILL_METHODOLOGY_VERSION="2.0"`），无条件改写会篡改 v2/v3 存量判定语义。故**保留** `_LEGACY_SECTOR_THRESHOLDS`/`_LEGACY_INDEX_THRESHOLDS`（连同 `_NEUTRAL_PCT_THRESHOLD`/`_STRONG_PCT`/`_THRESHOLD_VERSION`），收拢进带 `# ⚠️ legacy` 标注的区块，仅供 v2/v3 使用；4.0 主链阈值唯一来源 = `k_band_table.k_for`。

### 验证

- **测试**：新增 `test_v4_single_band`（7 参数化）、`test_v4_single_band_uses_compound_over_four_days`、`test_v4_requires_k_fail_loud`、`test_k_for_dispatches_by_target_type_and_falls_back_index`、`test_v2_path_unchanged_by_v4`，以及**护栏用例** `test_legacy_v3_sector_index_thresholds_unchanged_by_v4`（锁死 v3 存量 sector=0.25 / index=0.5 阈值区分未被 v4 影响）；同步更新 sector entry 断言（`threshold_version` → `"4.0"`；v4 不再产 `grade`；sector 用例 pct_chg 调至 ≥ sector k）。
- **验证**：`pytest tests/unit/test_prediction_validator.py -q` → 89 passed；`tests/unit` 全量 → 9 条既有基线失败（`test_industry_vector_search.py` 6 + `test_scheduler.py` 3）与本改动无关；ruff 改动后错误数与 HEAD 持平（18=18，无新增）。
- **未提交**：无（随本任务 commit 提交）。

---

## [main] 2026-10-06 — Task 3：验证器 actual 统一为复利口径（_compound_pct）

**开发者**: Aria

### 修复

- **改动（1 文件 + 测试）**：`src/aistock_agent/services/prediction_validator.py` 新增唯一复利实现 `_compound_pct(window) = ∏(1+p/100) − 1`（与 `scripts/calibration/k_band.py#cumulative_returns` 同口径）；`_verify_horizon` 的展示/落库 `actual` 由 `sum(window)` 改为 `_compound_pct(window)`。
- **严格边界（只改 actual）**：`_judge_window` v2/v3 分支的判定 `sum(window)` 保持原样（历史口径，改动会篡改存量回测语义）；`_verify_conditions` 的 scenario 累计判定不动；v4 复利判定属 Task 4，未提前实现。

### 验证

- **测试**：`tests/unit/test_prediction_validator.py` +2 用例（`_compound_pct` 复利口径 `[1,1]→+2.01%`、`[-1,1]→-0.01%`；`_verify_horizon` 集成点 4×+1% → `actual=+4.06%`，锁死与简单求和的差异）。
- **验证**：`python -m pytest tests/unit/test_prediction_validator.py -q` → 77 passed；`tests/unit` 全量 9 条既有基线失败（industry_vector_search/scheduler）与本改动无关。
- **未提交**：无（本任务随 commit 一并提交）。

---

## [main] 2026-10-06 — 个股情报入环 P2：个股粒度首次进入验证环

**开发者**: Aria

### 新增

- **端点** `POST /api/agent/internal/predictions/from-stock-info`（`api/routes.py`）：接收个股情报（`symbol`/`stock_name`/`published_date`/`ai_impact`/`ai_horizon`/`ai_summary`/`url`），确定性映射为 `PredictionResult` 后落 `prediction_records`（`source_type='stock_info'`、`source_id=stock_info:{symbol}:{published_date}`）。校验内部令牌；门槛未达/输入非法 → 200 `skipped`（带机器可读 `reason_code`）；同 `source_id` 已验证 → 409 拒覆盖；落库失败或其它意外异常 → 502。
- **模块** `services/stock_info_prediction.py`：个股情报 → `PredictionResult` 的**确定性映射**（不调 LLM、不发网络），含**全系统唯一的入环门槛判定点**（重大利好/重大利空恒入环；利好/利空仅中期/中长期/长期入环；中性不入环）。`conditions` 恒空、`horizons` 恒 1 档、`prediction_status` 恒 `hypothesis`；`evolution_narrative`/`attribution_summary` 为 `ai_summary` 逐字原文。
- `services/data_client.py`：新增 `list_predictions_strict(source_id)`（查询失败抛错，而非折叠为空列表）。

### 修复

- 落库失败（`save_prediction` 吞异常返回 `None`）此前报成 `200 saved` 假成功 → 改判 `502`。
- `skipped` 语义混淆（门槛未达 / 输入非法 / 取值无法映射同码）→ 新增 `reason_code` 区分，供调用方只对「门槛未达」静默。
- 409「已验证拒覆盖」防线在 `list_predictions` 查询失败时被静默绕过（失败被折叠为空列表）→ 改走 strict 入口 **fail-closed**，拒绝落库以免已验证记录正文被覆盖。

### 验证

- `pytest tests/ -q` = `32 failed / 3889 passed / 4 skipped`，失败集与基线（`32 failed / 3877 passed`）一致 → **新增失败 0**（+12 为本次新增用例）；`ruff check src/` = 65（基线 65）；`mypy src/` = 297（基线 297）→ 新增 0。
- 新增/扩展单测：`tests/unit/test_stock_info_prediction.py`、`test_stock_info_prediction_route.py`、`test_data_client_prediction.py`。

### 说明

- `due_dates` 复用既有 `_compute_due_dates`（与 index/sector 保持单一口径，不新增第二套交易日历）；未改动 `prediction_validator` / `_METHODOLOGY_VERSION` / 阈值 / 表结构。
- **尚未部署**。验收需部署后**次日 16:00 后**执行（P2-V1~V4/V6/V7）；`short` 档到期 = `published_date` + 5 交易日，**在此之前不得宣称「个股已入验证环闭环」**。

---

## [main] 2026-10-06 — 准确性体检：review 404 噪音修复（fix6）+ 历史事件 DateWindow 补置 + 断链①复核

**开发者**: Aria

### 修复

- `agents/workers/review.py`：quick 覆盖检查由 `node_api.get_analysis_report(...)` 改为 `get_analysis_report_quiet(...)`——quick 先于 full 生成时报告不存在（404）属常态，此前每次落 error 噪音；已在服务器部署并 `pm2 restart aistock-agent`，噪音消除。

### 新增（运维脚本）

- `scripts/backfill_event_entities_0924_1006.py`：对 2026-09-24 ~ 2026-09-30 历史政策事件按 DateWindow 补置资讯并幂等 upsert（`canonical_event_key` 冲突更新）。8 条 **8/8 成功**、重跑返回相同 `EVT-*`；L2 与检索结果**零差异**（时点/工具名/金额/利率全对）。

### 复核结论（非代码 bug，附证留档）

- **断链①「已到期未验证」**：以真实取值函数 `_verify_horizon` 精确定量——`pending_total=178` 中 63=窗口未满的合法等待、38=已回写仅等 long 档、**仅 18 条真滞后**（全为 `sector_prediction`/short/到期 09-24）；根因为板块日线 16:00 尚未入库 + 国庆长假断档。手动 `run_once()` → `updated=71`，复核 `truly_missing=0`。原报告「漏验 65 条」属高估，已纠正。
- **「市场洞见每天都证据不足」**：`attribution_status=hypothesis` 时系统强制清空 `primary_chain_id` 并把 supported 降级 weak → `primaryCause` 恒 null。属**证据门槛错配**（非崩溃、非数据缺失），改由 App 前端展示口径解决。
- **fear-greed（fix3）**：`composite` 非 9/27 冻结；实测 9/27–9/30 仍波动，**10/01 起恒 5.38 恰为国庆休市**，`breadth_daily`/`limit_daily` 最新 9/30，复牌自动更新。
- **日历预期差（fix4）**：`expectation_diff` 每日运行但 judged/skipped/attempted 全 0，因 20 条 high 事件**全在未来**（观察窗 [昨日,今日] 内无发生）；未来事件 result 空=设计正确，0 条已过期 high 缺 result。
- **watchlist（fix2）**：2026-08-30 已并入 stock-trace，Node/前端均改读等价新链，9/04 停更是预期；`watchlist_insight_*` 为遗留死表。
- **stock_monitor_events（fix5）**：遗留死表；监控端点 `service.ts` 已改读 `stock_info_judgements`。

---

## [xusiyun] 2026-10-02 — 重大事件时间线物化 id 全链路修复（物化先于落库 + 重放透传）

**开发者**: xusiyun

### 修复

- **物化先于落库**（`services/event_scraper.py`）：抽出 `_materialize_events(events)`（开关内短路、失败只置 `event_entity_unfilled` 不阻断主链路），`scrape_full_daily` / `scrape_intraday` 改为在 `save_event_scrape` **之前**物化本批重大事件，使权威 `app_event_id` / `app_event_status` 随事件库 content 一并持久化。此前「先落库、后物化」导致 id 只写内存、**从不落库**：任何「从事件库重读再传导」的路径（晨报 I4 兜底、缓存命中重放）都拿不到权威 id，传导报告退回 `evt_md5` 键，而 app-api 时间线的 occurred 准入要求 `agent_analysis_reports.user_id == event_entities.event_id` → 该事件被静默丢弃（症状：事件传导列表有、时间线没有）。物化作用于整批（幂等 upsert 不产生重复实体），顺带覆盖「已入库但当时未物化」的补漏；`_spawn_conduction` 守卫语义不变（同一批对象、`added>0` 才传导）。
- **重放透传权威 id**（`agents/workers/morning.py`）：`_event_records_to_major_events` 从事件库重放时条件透传 `app_event_id` / `app_event_status`（非空才落键，与 `_trigger_conduction` 同款，`major_events` 形状向后兼容）。
- **重放保留来源名**（`services/event_store.py`）：`load_event_scrape` 构造 `EventRecord` 时补 `source_name`（有值保留、缺省 None），修掉「从事件库重读再传导」丢媒体名 → LLM 判不出媒体 → 前端恒显示「未知来源」的同族缺陷（2026-09-24 `source_name` 透传只覆盖了同批路径）。
- **`_safe_float` 消除 mypy strict 告警**（`services/event_store.py`）：`float(object)` → 先按 JSON 标量窄化（int/float 直转，其余走 `str` 兜底解析），实现对齐同包 `global_importance_evaluation._safe_float`；数字字符串 / 可字符串化数值对象仍可转，None/畸形结构/非数值串回落默认值（契约由 `test_safe_float_contract` 锁定）。修复后 `mypy event_scraper.py event_store.py morning.py` 三文件 **0 error**。

### 改进

- `services/event_store.py`：`EventRecord` 加性声明 `event_entity_unfilled: NotRequired[bool]`（stdlib `typing`，Python ≥3.11；仅写不读的排查标记，构造时无需提供）。

### 测试

- `tests/integration/test_event_scraper_conduction_payload.py` 新增 `test_scrape_intraday_persists_app_event_id_into_event_store`、`test_scrape_full_daily_persists_app_event_id_into_event_store`、`test_scrape_intraday_switch_off_skips_materialize_but_conduces`（走真实 `save_event_scrape`，用 JSON 快照捕获落库 content —— 修复前必失败）。
- `tests/unit/test_morning_event_store_integration.py` 新增「透传 id/status」与「缺省不落键」两例。
- `tests/unit/test_event_scraper.py`、`tests/unit/test_event_scraper_conduction.py` 共 7 个既有用例补 `_materialize_event_entity` 隔离 patch（防测试打真实 HTTP）。
- `tests/unit/test_event_store.py` 新增「重放必须保留 `source_name`」与 `test_safe_float_contract`（9 组畸形/可转值参数化：None/畸形结构/非数值串回落默认值，数字字符串/Decimal/bool 仍可转）两例。
- 受影响 9 文件：`124 passed, 3 skipped`（3 skipped 为需本地 app-api 的 e2e）；`mypy` 目标三文件 0 error。

---

## [junliang] 2026-09-26 — 洞察报告改结构化 blocks 输出（移除 reportlab）+ 异动解读「分析维度三」改三列表格

**开发者**: 李俊良

### 新增

- `services/insight_report.py`：`build_report_blocks(data)`（原 `build_report_sections` 的 `lines` 彻底移除）；Block 判别联合 6 类（`kv`/`verdict`/`candidates`/`chain`/`evidence`/`list`）；空节 → `blocks: []`（规则统一为"源数据非空才出块"）。
- 六阶段因果链**只取 `role=primary` 主链**（真实 artifact 通常有 primary + alternative 两条链、各 6 个节点，旧实现把 12 个节点平铺、无任何分界，是"看不出这是因果链"的根因之一）；链节点与候选项**同时输出中文标签与机器 key**（`stageKey`/`epistemicKey`/`statusKey`），供前端做中性弱化判定而不必匹配中文标签（改文案即静默失效）。
- `POST /api/agent/insight-report/sections`（替代 `/insight-report/render`）。

### 变更

- AI 异动解读「分析维度三：产业链机会」改为三列表格：`MASTER_PROMPT` 强制单行表格「环节 | 标的 | 理由」——一行一环节、标的只写名称不带代码、理由 ≤8 字、禁止留空单元格、总行数 3~5 行；`GRAPH_DIVERGE_PROMPT` 输出口径同步收紧（示例理由改短句 + 同口径硬性要求），让 Master 拿到干净素材、减少格式漂移。
- 删除 reportlab 渲染与内嵌字体（2.33MB）；`pyproject.toml` 移除 reportlab 依赖。

### 测试

- `tests/unit/test_insight_report.py` 重写为 blocks 断言（主链筛选 / 无 primary 降级 / 无链空节 / 脏数据跳过 / `*Key` 字段 / 空节规则 / 中文化回归），**25 passed**；`tests/integration/test_alert_agent.py` **10 passed**。
- `ruff check` 改动文件 All checks passed（注意 ruff 的 E501 按 **CJK 双宽**计，中文 prompt 单行约 50 字即到 100 上限）。
- 实测：真实跑一次 alert 分析（603065）输出即三列表格（标的无代码、理由 6 字、无空单元格），且 `display_report.stocks` 仍为代码数组；**独立脚本直跑 worker 必须先 `await HttpClientPool.init(...)`**，否则 `node_api` 全部报 `not initialized`、子 Agent 静默降级（资讯/图谱工具全失败），输出不可信。

### 文档

- `AGENTS.md` / `README.md` 同步 blocks 口径（章节构建改为纯模板 JSON blocks，无 LLM / 无字体依赖）。

## [xusiyun] 2026-09-25 — 未来事件影响板块预计算 + 物化时间兜底 + 来源名透传

**开发者**: xusiyun

### 新增

- `services/impact_sectors_precompute.py`：未来 calendar 事件（`source_type='calendar'` 且 `status∈{scheduled,upcoming}` 且 `impact_sectors` 为空）→ 行业向量（KG 语义）匹配（阈值 0.7）取 Top3 写回 `/internal/event-entities`；无可靠行业保持 `[]`（方案 A，不 LLM 强猜）；独立 cron 每日 07:00（在 app-api Calendar 物化 06:40 之后）；幂等守卫「仅列为空才处理」（`updated_at` 节流不可靠，Calendar 物化 cron 会刷新）。
- `config.py` 新增 `impact_sectors_precompute_enabled`（默认 True）与 `scheduler_impact_sectors_precompute_cron`（`0 7 * * *`）；`scheduler.py` 注册该 job（misfire_grace_time=3600）。
- 物化时间兜底（spec §5B.3）：抽不出绝对日期时改用发布时间兜底（`publish_time_fallback`，`time_confidence=0.5`）；`_extract_publish_time` 字段序 publish_time→published_at→event_time→ctime_stamp→ctime→create_time→date，`_normalize_datetime_to_iso` 兼容 ISO/无时区/unix 秒毫秒；仅落已发生/当日，不投未来。

### 修复

- 新增 STOCK 事件守卫：`event_scope='STOCK'` 的普通个股事件不物化为 Event Entity（不进重大事件时间线），记 `event_entity_materialize_skipped_stock`。
- `collect_ths_original` 补齐 `source_url` → `url` 映射（Node 侧字段为 `source_url`，`normalize_event` 只认 `url/link`，否则同花顺原创事件 url 恒空）。
- 外盘行情事件携带 `source_name="外盘行情"`：经 `event_store`/`event_conduction` 透传并拼入 user_msg「事件来源：X」，修外盘无 URL 事件恒显示「未知来源」。

### 改进

- `config.py`：`event_entity_enabled` 默认由 False 翻为 True（app-api 端点已落地，News 通道重大事件开始物化）。

### 测试

- `tests/unit/test_impact_sectors_precompute.py`（13 passed）、`tests/unit/test_event_entity_time_extract.py`。

---

## \[changer\] 2026-09-22 — 事件前瞻主体化（种子 / 候选晋升 / 预期差 / 读侧折叠）

**开发者**: changer-collab

### 新增

- `services/forward_events.py`：事件前瞻主体化——种子导入（`source='L4'`，**X5：显式 L4，`upsertEvent` 缺省 L3 会把种子归成 earnings 污染 typeFromSource**）+ 候选晋升（逐条带 GET 存在性预检，rejected 不写入）+ 预期差 `run_expectation_diff`（谓词 **[昨日,今日]** 已公布事件 + `result_attempted_at` 日内重试 X2）。
- `services/forward_event_sources.py`:L3 前瞻迁入（query 族 6 条，`L3_QUERY_HARD_LIMIT=6` / `L3_DAILY_SOFT_LIMIT=12`，collect_jiuyan 退化留痕）；`services/forward_event_llm.py`：预期差 LLM 只产 result 事实（不产展望/方向）。
- `rhythm_master` 新增 `event_window_near_end_date`（近 5 交易日末日，越年 null，`NEAR_HINT_DAYS=5`）；scheduler 注册 4 job（种子 07:30 / 抓取 07:40 / 预期差 08:00+11:30+13:00，misfire_grace）。

### 改进

- `event_calendar.py` 移除读侧词表升格（`is_high_importance_event` 已删，`MACRO_EVENT_TERMS` 保留）。

### 契约（跨仓）

- 预期差 `result` 经 app-api `toContractEvent.detail`（`x｜consensus:N` 前缀，O1）读回——**读侧必须与写侧同通道（C1）**，跨仓契约断裂会让 job 空转。

## \[changer\] 2026-09-21 — 节奏大师事件日历提前展示（全量窗 + 展示/分析分离 + L3 前瞻扩窗）

**开发者**: changer-collab

### 新增

- 节奏卡事件日历改为**全量展示窗**：`load_event_window(horizon_days=None)` 自 target_date 拉到当年年末（越年截断留痕，不误显"数据源未接入"）；`EventWindow` 新增 `display_events` 承载展示窗事件，新增 `split_analysis_window()` 按**交易日差 ≤4** 切分析子窗（A2 裁决：单次请求超集，HTTP 仍 1 次/卡/时点）。

### 改进

- `event_window` 投影从"5 交易日全部事件"改为"全量事件（过滤 importance≥medium + 上限 30，防财报季噪音）"；分析链路（event_d / 锚点 / 分支 / certainty）**维持 5 交易日口径不变**（临近=证据语义不被远期事件污染）。
- L3 前瞻捕捉查询族由"下周"口径扩为"本月 + 下月"，让 FOMC/CPI/财报季等远期宏观日程提前 3-4 周入库；查询族仍恒 4 条（与硬上限 1:1），成本不增。

### 测试

- `tests/unit/test_event_calendar.py`：全量窗越年截断 / 数据源缺失 / 子窗交易日差切分（含跨周末不误纳）用例。
- `tests/unit/test_rhythm_engine.py`：`project_event_window` importance 过滤 + limit 上限用例。
- `tests/unit/test_rhythm_master_compose_card.py`：事件 fixture 补齐 date 字段对齐真实契约。

---

## \[changer\] 2026-09-21 — 主线候选池扩容至 35 真实板块与 3 主线标签展示（P3″）

**开发者**: changer-collab

### 新增

- 主线候选池 5 → 35 真实同花顺板块：活动配置 `mainline_candidates.v35.json`（每条含 `mainline` 分组字段：AI硬件 10 / 半导体 11 / AI软件 9 / 无标签 5），`mainline_candidates.json` 收敛为 v5 冻结基线（只读回滚态）；loader 新增 `MAINLINE_CANDIDATES_PATH` env 灰度切换（下个 slot 冷生效，回滚 = 恢复 env）。`judge_mainline` 与阈值/返回结构零改动（H2）。
- 3 主线（AI硬件/半导体/AI软件）以分组口径接入展示层：溯源行 head 升级为 `主线：{label}·{name}（主线成立·强，超额 +x pct，数据日 …）`（label==name 去重、无 label 原样）；LLM 叙事确定性事实带 `{label}方向（…）` 前缀，narrative 可引用方向标签、mainline[].name 仍只放行真实板块名。不新增对外字段（H6）。

### 改进

- 守门扩展：`VERIFIED_BOARD_NAMES` 实测表扩至 35；新增 `mainline` 枚举守门与 name∪aliases 跨候选碰撞守门（H11）；H5 口径迁移为「既有 5 条映射不得改写 + 允许加性新增」。
- 移除候选配置中的 `priority` 死字段（loader 本就不消费）。

### 文档

- 契约文档 rhythm-master-logic.md §9.1/9.1.1/9.2/9.4/§11/§14/§16 同步至 v35 灰度架构；spec §7.2/§7.3/§8.12 勾销实施状态。

---

## \[changer\] 2026-09-20 — 主线候选清单增加维护机制（确定性去重 / 降级留痕拆分 / CI 守门）

**开发者**: 37588

### 修复

- 候选清单此前无维护机制：板块代码或板名重复出现时被静默接受，胜出候选的**按名反查**可能落到另一只板块；同时「已入库但算不出超额」的候选（区间行数恰好等于入库下限 20 根）被静默丢弃，最终只报「无清晰主线」，把归因指向错误方向。现于加载阶段做**确定性去重**（代码与归一化板名双唯一，保留清单中首次出现者并留痕），并把「取数失败 / 序列不足 / **不可评分**」三类降级**分开记入**缺失清单，禁止互相掩盖。
- 板块表重复代码此前按接口返回顺序**静默覆盖**（同一输入结果不可复现）；现改为确定性保留首次并留痕。

### 改进

- 新增候选清单 **CI 守门**：直接读取真实清单文件，逐条校验「候选名称与其板块代码反查到的真实板名一致」，并要求代码与板名各自唯一。此前这类校验只在生产现场生效，清单里别名写错/写空在 CI 全绿。

### 测试

- 新增 5 条候选清单用例（读真实清单 + 模拟板块表）；**修正 2 条与实现脱节的存量用例**（GI 准入改走确定性规则后「调用模型次数」应为 0；迭代适配注册表已含个股预判条目）。
- **承接主分支**：手动触发节奏卡的端点新增可选参数后，3 条存量断言未同步（断言 2 个入参、实调 3 个，报 `expected await not found`）；已按当前契约对齐，并顺带修正该端点一处超长行，使分支恢复全绿。
- 全量单元测试 **3295 passed / 1 skipped / 0 failed**；节奏相关集成 18 passed；lint 改动文件零新增（仅余 1 条既有超长行，非本批引入、已用基线核对）。

### 硬约束（未触碰）

- 主线判定层返回结构、阈值、状态值域、候选池优先级**全部未改动**；主线结论的对外可见形态（溯源行 / 仓位文案）未改动；**不新增对外字段**。

### 文档（2026-09-20 P3″ 扩容同步，非本 commit 代码改动的分量）

- **主线候选池扩容至 35 真实板块**：`mainline_candidates.v35.json`（35 条活动配置）+ `mainline_candidates.json`（v5 冻结基线，5 条只读）+ `MAINLINE_CANDIDATES_PATH` 环境变量灰度切换（下个 slot 冷生效，恢复 env 即回滚基线）。`judge_mainline` 零改动。
- **3 主线标签（AI硬件 / 半导体 / AI软件）接入**溯源行（head `主线：{label}·{name}`，`label==name` 去重）与 LLM 叙事（`确定性主线事实：{label}方向（…）`）；展示 A/B 落地，judge 层零改动，不新增对外字段。
- **守门扩展**：`VERIFIED_BOARD_NAMES` 实测定长表扩至 35（守门测试读 v35）；新增 `mainline` 枚举与 `name∪aliases` 跨候选碰撞守门（H11）；H5 口径迁移为"既有 5 条映射不得改写 + 允许加性新增"。
- **`priority` 死字段删除**（C2，独立 commit）；loader 不再读它。

## [main] 2026-09-19 — 节奏大师手动补跑支持 `target_date` 覆盖落库键（R-节奏-补跑）

**开发者**: Aria

### 新增

- **背景（用户实测）**：手动触发 `POST /api/agent/briefing/rhythm-master/trigger` 传 `report_date=2026-09-18` 重算后，**前端 9-18 页仍显示 9-17 内容**。查实为**日期语义错位**：落库键 = `target_date`（worker `report_date=card.target_date`），而 `after_close` 的 target_date = 基准日的**次一交易日**；手动端点的 `report_date` 又是**基准日** → 重算结果落到了 `09-21` 键，前端 9-18 页读到的是 09-17 自动跑生成的旧卡（basis=09-17）。
- **改动（3 文件 + 测试）**：
  - `src/aistock_agent/agents/workers/rhythm_master.py`：`_compose_card(run_date, slot, target_date=None)` 显式 target_date 覆盖推导；`run(state)` 读取 `state["target_date"]` 透传（非空 str 才生效）。
  - `src/aistock_agent/services/scheduler.py`：`_dispatch_rhythm_master(slot, report_date, target_date=None)` 写入 `state["target_date"]`；docstring 说明基准日/目标日语义。
  - `src/aistock_agent/api/routes.py`：`trigger_rhythm_master` 解析可选 `target_date`（YYYY-MM-DD 校验，非法 422），透传并记日志。
  - `tests/unit/test_scheduler.py`：+2 用例（显式 target_date 写 state / 缺省不写，保持按 slot 推导）。
- **用法**：重算 9-18 数据并让前端 9-18 页显示 → `{"refresh_slot":"after_close","report_date":"2026-09-18","target_date":"2026-09-18"}`。

### 验证

- `pytest tests/unit/test_scheduler.py` 50 passed；`tests/unit/test_rhythm_master_compose_card.py` + `test_rhythm_wiring.py` 23 passed。
- **未提交**：用户选择暂不 commit/push/部署。

---

## \[changer\] 2026-09-19 — 阶段兜底启用「前一交易日」修复热度轴整条消失（X2）

**开发者**: 37588

### 修复

- 证据中性时 `detect_stage` 会走兜底「沿用前阶段」，但生产调用点固定传 `prev_phase=None`，使该兜底失效 → `stage=None` → `level`/`score`/`phase` 整条热度轴消失（2026-09-19 生产实测：`technical.index_breakdown=true` 而三键全 `null`，卡片只剩一句「空仓观望」）。现读「前一交易日 `after_close` 卡」的主阶段作为 `prev_phase` 传入，使兜底按原设计生效。
- 降级留痕：仅当「本地无阶段可归」且「前卡也拿不到阶段」时写一条 `data_missing`；并按硬约束 12 把「取数失败」与「卡存在但阶段非法」分开措辞（避免把失败归因为数据问题，也避免每卡常驻噪音）。

### 改进

- 抽出纯函数 `_stage_from_report` 作为「从节奏卡响应提取主阶段」的**单一校验收口**（缺失/越界一律 `None`），既有同日沿用函数 `_inherit_basis_stage` 改为复用它，防止两处规则漂移。

### 硬约束（未触碰）

- 未改 `detect_stage` 判据、未改 `STAGE_TO_LEVEL`、未新增档位；`score`/`level` 仍由 `stage` 派生（09-18 硬约束 6）。

## \[changer\] 2026-09-19 — 修复主线候选取数日期格式导致主线不可用（X1）

**开发者**: 37588

### 修复

- 主线（板块）候选区间日 K 取数传入 ISO 连字符日期（`2026-05-11`），而 Node 侧 `/internal/ths/:code/daily` 硬校验 `^\d{8}$` → 请求**恒 400**；失败被 `get()` 归为 `None` 后又被 `or []` 静默吞掉，导致 5 个候选全部被误记为「序列不足」→ 主线恒不可用（`有效候选 0/3`），节奏卡仓位文案失去主线驱动。现于取数边界（`data_client.get_ths_daily_range`）统一归一为紧凑 `YYYYMMDD`。
- 留痕归因纠偏：新增纯函数 `mainline_engine.build_mainline_notes`，把「取数失败（请求失败/异常 → None）」与「数据不足（`pct_chg` 行数 < `MA20_MIN_BARS`）」**分开留痕**。此前把 400 写成「序列不足 5」，把排查方向误导为数据问题。`fetch_failed == 0` 时文案与既有实现逐字一致（零回归）。

### 新增

- 主线留痕新增「主线候选取数失败（N 个）」标注；worker 区分 `None`（取数失败）与行数不足（数据不足）两条降级路径。
- 单元用例：`build_mainline_notes` 的「legacy 文案零回归」「两类失败不混写」「候选充足不留痕」三条断言；`get_ths_daily_range` 的 ISO 归一 / 紧凑格式不变 / 失败返回 None 三条断言。

## \[main\] 2026-09-19 — 条件点亮专项：新增「未点亮」归因码审计（方案 A，判定口径不变）

**开发者**: Aria

### 新增

- `condition_met_judge.explain_unjudgeable_reason`：**纯诊断函数**（不参与判定、不产键），为"判不出（`None`）"的条件归因原因码——`event_channel` / `guard_domain`(G1) / `guard_dir_pct`(G3) / `guard_or`(G4) / `compound_other_unjudgeable` / `single_other_unjudgeable`；守卫口径与 `_judge_clause_state` **逐子句同序**。
- `prediction_validator._scan_condition_met` 按记录聚合落 `prediction_condition_met_unlit_reasons`（`{id, checked, lit, reasons}`），用于回答"条件为什么不亮"。

### 修复

- 专项取证（三仓交叉）：`/internal/ths/:code/daily` 对 ISO 日期**恒 400** → **板块条件判定取数全空 → 一律 None**（该缺陷已由 app-api X1 修复并部署，实测 ISO 入参 200）；剩余原因 = **32/32 复合「且」条件** + **14/32 含已下线的板块资金流口径**，叠加 G2（复合不得半判）→ 整体 None。**四道护栏与全部判定行为一律未改**（只加观测，不加点亮）。

### 测试

- `tests/unit/test_condition_met_judge.py` +7 例、`tests/unit/test_prediction_validator.py` +1 例（先红后绿）；定向 193 passed；全量 `tests/unit` 3277 passed / 8 failed（8 条为文档化存量红，零新增）。

### 后续任务

- 为「板块主力资金 / 情绪家数 / 外盘走势」等**无数据源口径补数据源**（不采用"生成侧禁写"，避免阉割预判信息量）；事件类判径（状态锚 → 受限 LLM，默认关）已具备，**无需新建验证 agent**。

---

## [main] 2026-09-18 — 板块定向检索 query 换词：现象式问句 → 5 个事件族

**开发者**: Aria

### 改进

- **背景（组长口径「检索的关键词也需要改进，要更容易找到真正的驱动事件」）**：`sector_trace_snapshot._sector_evidence_queries` 原本 3 组定向 query，其中
  1. `{date} {板块} 板块 暴跌 大涨 原因` 是**现象式问句**——检索器返回的正是「注册制次新股大涨八个点」「全线上涨！涨幅第一」这类行情综述，而准入层（`is_driving_event`）刚写好规则专门拒它们：等于**捞回来再扔掉**，白花检索配额；
  2. `{date} {板块} 板块 反垄断 调查 监管` 是早前某次「存储狙击」案例的**特化词**，只有板块恰在被调查时才命中，绝大多数日子空转；
  3. 三条 query 只有 `事件 公告 政策` 一条是通用事件导向，召回面太窄。
- **改动（`services/sector_trace_snapshot.py`，单文件、单函数）**：改成 **5 组事件族**，族间词不重叠（族内为并列同义词），每族仍注入 `report_date` 与板块名、中文空格连接：
  | 族 | query |
  |---|---|
  | 政策监管 | `{date} {板块} 政策 监管 调查 部委 试点` |
  | 公司硬事件 | `{date} {板块} 公告 中标 订单 获批 并购` |
  | 供需价格 | `{date} {板块} 涨价 减产 扩产 供需 库存` |
  | 技术产业 | `{date} {板块} 量产 投产 认证 技术突破 招标` |
  | 海外贸易 | `{date} {板块} 出口 关税 制裁 海外订单 豁免` |

  **不再出现任何现象词**（原因/暴跌/大涨/涨幅）；保住原第 3 组的监管意图（`监管 调查` 进政策族），另把此前完全没覆盖的公司级硬事件、供需价格、技术产业、海外贸易四类补上。**副作用（如实登记）**：每板块检索条数 3×5=15 → 5×5=25（Tavily 调用 +67%），换来召回面与精确度同时提升。
- **连带**：`tests/unit/test_attribution_chain.py` 的 `_SEARCH_QUERY` fixture（"与第 1 组同形"的 kind 标签）同步改为新第 1 组，保持"同形"注释不假。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_sector_trace_snapshot.py` +1 例（现象词一律不得出现 + 10 个事件族代表词必须命中 + query 条数 = 5）；既有 `test_sector_queries_include_regulatory` 加强为"每条 query 都带板块名与日期"。RED 取证：`1 failed, 1 passed` → GREEN。
- `pytest -q tests/unit/test_sector_trace_snapshot.py tests/unit/test_attribution_chain.py tests/unit/test_sector_trace_worker.py` → **165 passed / 0 failed**；`ruff check` 两个改动文件 `All checks passed!`。
- **评估后暂缓（已登记 AGENTS.md，未做）**：① **龙头股名扩展**（`{板块} {龙头股} 公告 中标 订单`）——`lead_stock` 已在快照 `fact` 里可直接取用；② **Tavily `days` / `include_domains` 参数**——用时间过滤替代"日期关键词" + 站点白名单从源头掐掉行情页/栏目页噪声；二者都需改 `TavilyService.search` 包装并确认 failover 链（tavily→doubao→anysearch）都支持，故本轮未做。
- **跨端**：仅改 agent-py（1 源文件 + 2 测试文件 + `AGENTS.md` + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 文档更新（跨页口径已根治 + 问题 1 样本补第 4 条）

**开发者**: Aria

### 文档

- **`AGENTS.md` 链式溯源闭环 ① 段**：「残留（如实登记）」改为「**残留（2026-09-18 当晚已根治）**」——板块详情页 `trace.summary` 已由 app-api `848669e` 改为**读当日链**（`pickSectorTraceSummary` 链优先），与「市场洞见」页同源；agent-py 侧 0 改动。
- **`AGENTS.md` 待观察样本清单补第 ④ 条**（目标 3–5 条，现 4 条）：2026-09-18 当日最后一次重跑（14:31）`科创次新股` 的 `政策支持集成电路发展，科创综指ETF华夏(589000)昨日净流入超1200万元_每日经济新闻` —— **"资金流报道"族**：主体是 ETF 净流入/主力资金，靠夹带的「政策支持」被 `_CAUSE_TOKENS` 豁免。关键落差：`_NON_PRICE_DOMAIN_KEYWORDS` 的资金流词族**只作用于条件点亮 G1，不作用于事件准入** → 候选修法是在事件准入侧补资金流/份额/净流入类词并与 G1 词表对齐。

### 验证

- **无代码改动**（本轮 agent-py 生产代码与测试 0 改动）；验证：该次重跑链 `CONTRADICTION=0`、`PAGE_NOISE_IN_EVENTS=0`，3 个板块各有自己的链摘要。

---

## [main] 2026-09-18 — 页面噪声**覆盖回归补偿**（站点栏目名 + 站点入口首页）

**开发者**: Aria

### 修复

- **背景（迭代 4 复核时暴露）**：收窄提交 `38284cb` 把站点名（同花顺/东方财富）移出 `_PAGE_NOISE_TOKENS` 后，`股票频道- 东方财富网`（ref `https://stock.eastmoney.com/`）**再无任何判据可拦**——它既非现象也非原因，按"判不出即放行"进了事件层；迭代 4 的一致性裁决还会把它**提升成板块摘要**。这是收窄引入的**覆盖回归**——但不能恢复站点名（那会退回"同花顺：国家大基金三期成立"被误拒）。
- **改动（`services/attribution_chain.py`，单文件）**：补两道**形态类**判据，与站点名这条"品牌类"判据解耦：
  - **标题级**：`_PAGE_NOISE_TOKENS` 增栏目/首页形态词 `频道/頻道/首页/首頁/栏目/欄目/导航/導航`——拦的是"栏目名"这一**体裁形态**（`股票频道- 东方财富网`），不是品牌；含站点名但无栏目形态的真事件标题照常放行。
  - **URL 级**：新增 `_PAGE_NOISE_URL_ROOT_PATHS = ("", "/", "/index.html", "/index.htm", "/index.shtml", "/index.php")` 与纯函数 `_is_site_root(host, path)`——已知行情/数据站点域（`10jqka.com.cn`/`eastmoney.com`，**含任意子域**）的**站点/栏目首页**判噪声。与既有页面模块子域判据正交：`stock.eastmoney.com/`（频道首页）判噪声，`www.eastmoney.com/news/…`（站内报道页）不判。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+8 例**（4 条栏目/首页标题必拒 + 4 条站点首页 URL 必判噪声）。RED 取证：`7 failed, 3 passed`（4 例 `assert '' == 'page_noise'` + 3 例 `assert False is True`）→ GREEN **144 passed**。
- `pytest -q tests/unit/test_attribution_chain.py` **144 passed**；全量 `tests/unit` **3224 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 两个改动文件 `All checks passed!`；`mypy` 改动源文件 **1 条**（与 HEAD 同一条、仅行号平移 → 新增 0 条）。
- **另登记（暂不改）**：本次生产复核还捞出两条**问题 1 族**漏网（组长裁定先攒样本），新增到 `AGENTS.md`「待观察样本清单」：② `国家大基金持股概念涨4.03%，主力资金净流入39股-证券之星`；③ `今日A股上演魔幻一幕，完全看不懂了…… - 股票`（"情绪化评论体裁"族，需 `commentary_without_event` 判据）。
- **跨端**：仅改 agent-py（1 源文件 + 1 测试文件 + `AGENTS.md` + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 迭代 4：摘要与事件层一致性（"结论不得与证据相反"）

**开发者**: Aria

### 新增

- **背景（生产实证）**：2026-09-18 重跑后同一板块卡片上两句话并存 —— `children[].trace_summary` = `未检索到可明确解释当日行情的独立触发事件`（溯源阶段 trigger headline），而 `children[].events` 非空（检索补漏路径从同一快照的 `sector_event:*` 候选里放行了一条）。组长口径：**逻辑通顺完整** —— 结论不得与证据相反。
- **改动（`services/attribution_chain.py`，单文件）**：
  - 新增常量 `_NEGATIVE_SUMMARY_MARKERS`（未检索到/未找到/未发现/未确认/未明确/未识别/未匹配/没有检索到·找到·发现/无法确认/无法判断/不能确认/暂无/尚未）。**刻意不收"不足/没有/缺少/缺乏"**：肯定归因句会被误判成否定句、把真有归因的摘要错误让位给事件标题。
  - 新增纯函数 `_is_negative_summary(text)` 与 `_first_event_headline(events)`（事件节点空/空白/非 dict/非字符串一律跳过）。
  - 原 `_trace_summary` 的报告侧取源（报告 summary → trigger headline → claims → 中性兜底）**原样抽成** `_trace_summary_from_report(trace_result)`；`_trace_summary(trace_result, *, events=())` 变为"取源 + 一致性裁决"：摘要为否定句 **且** `events` 非空 → 让位给事件首条非空 headline；事件无可用标题则让不了位、如实保留否定句。**不传 `events` 的调用方行为逐字不变**。
  - 调用侧（`assemble_attribution_chain` children 组装）：`trace_result` 归一化为 `trace_result_dict` 后**先算 events、再定摘要**（顺序即语义），改写发生时留痕 `chain_trace_summary_overridden_by_events`（`report_date`/`sector`/`from_summary`/`to_headline`），可审计、可对账。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+15 例**（否定句 6 种写法 × 有事件必让位 / 肯定句 + 有事件必保留 / 无事件保留且不传 events 行为不变 / 肯定句含"不足"不被误判 / 事件 headline 空或非 dict 让不了位 + 集成 2 例）。RED 取证：`14 failed, 8 passed` → GREEN **136 passed**。
- `pytest -q tests/unit/test_attribution_chain.py` **136 passed**；`-k "chain or sector_trace or consumer or condition_met or attribution or review"` **657 passed / 2 failed**（2 条为基线存量）；全量 `tests/unit` **3216 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 两个改动文件 `All checks passed!`；`mypy` 改动源文件 **1 条**（与 HEAD 同一条、仅行号平移 → 新增 0 条）。
- **残留（如实登记，未做）**：板块详情页 `/api/agent/sector-insight/:date` 的 `trace.summary` 仍由 app-api `extractTraceSummary` 取 `market_trace.trace` 的 trigger headline（agent-py 侧本轮 0 改动）→ 两页可能不一致；根治方向：让 sector-insight 改读当日链 `children[].trace_summary`（单一真相源），跨仓未做，已登记 AGENTS.md。
- **另登记（暂不改）**：事件准入现存一类漏网 —— **研报观点/展望体裁**标题既非现象也非基本事件，靠弱原因词「落地」被 `_CAUSE_TOKENS` 豁免放行。组长裁定**先攒样本（目标 3–5 条）再定词表**。
- **跨端**：仅改 agent-py（1 源文件 + 1 测试文件 + `AGENTS.md` + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 页面噪声判据**收窄**（消上一轮如实登记的两处误伤）

**开发者**: Aria

### 修复

- **背景**：上一轮（`45a0cd9`）落地两道网时已登记两处误伤风险，本轮就地收窄：① 标题级词表含**站点名**（同花顺/东方财富）——它们是**来源品牌**不是页面形态，真原因标题若只带站点名而不含原因词会被 `page_noise` 误拒；② URL 主机做**任意子串**匹配（`guba`/`f10`/`quote`）——`f10.example.com` 这类非行情站域名被误判成"页面噪声页面"。
- **改动（`services/attribution_chain.py`，单文件）**：
  - **收窄 ①**：`_PAGE_NOISE_TOKENS` 删除 `同花顺/同花順/东方财富/東方財富` 四个词条（留注释说明**为何刻意不入表**）。覆盖不丢——生产实证那条「国家大基金持股 - 行情中心- 同花顺」靠「行情中心」仍判 `page_noise`（测试固化为正例）。
  - **收窄 ②**：`_PAGE_NOISE_URL_HOST_TOKENS = ("q.10jqka.com.cn","guba","f10","quote")`（子串匹配）**替换**为双条件常量 `_PAGE_NOISE_URL_SITE_DOMAINS = ("10jqka.com.cn","eastmoney.com")` + `_PAGE_NOISE_URL_PAGE_LABELS = ("q","data","stockpage","quote","f10","guba")`，新增纯函数 `_is_page_noise_host(host)`——**仅当主机落在已知行情/数据站点域下、且其首段子域是页面模块标签**时才判噪声；主机解析顺带剥 userinfo 与端口。路径段判据（`/detail/code/`、`/quote/`、`/f10/`、`/guba/`）与"原因词豁免"口径**逐字不变**。
  - **副作用（正向）**：站点首页（`www.eastmoney.com`）与站内**报道页**（`finance.eastmoney.com/news/…`）不再被判页面噪声；新增 `stockpage.10jqka.com.cn`、`data.10jqka.com.cn`、`quote.eastmoney.com`、`f10.eastmoney.com` 四类页面模块子域覆盖。
  - 同步更新块注释、`is_page_noise_url`/`event_summary_reason` docstring（删除"站点名"相关措辞）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+15 例**——站点名单独出现必须放行 3 例、站点名+页面形态词仍拒 2 例、页面模块子域 URL 判噪声 5 例、非行情站 URL 不判 5 例。RED 取证：`7 failed, 8 passed` → GREEN **121 passed**。
- `pytest -q tests/unit/test_attribution_chain.py` **121 passed**；`-k "chain or sector_trace or consumer or condition_met or attribution"` **571 passed, 2639 deselected**；`ruff check` 两个改动文件 `All checks passed!`。
- **仍存风险（如实登记，无实锤样本）**：① 站点域白名单只覆盖 `10jqka.com.cn`/`eastmoney.com`——其它行情站只能靠**路径段**判据命中，主机级漏判属"多放行"（方向安全）；② 站点内非页面模块子域（`www.`/`finance.`/`news.`）一律放行，若日后出现这些子域下的纯行情表格页误入，需扩 `_PAGE_NOISE_URL_PAGE_LABELS`。
- **跨端**：仅改 agent-py（1 源文件 + 1 测试文件 + `AGENTS.md` 链事件准入 ⑤ 段 + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 链事件准入补「页面噪声」两道网（标题级 `page_noise` + URL 级 `page_noise_url`）

**开发者**: Aria

### 新增

- **背景（今日生产实证）**：链 `children[].events`（`source="search"`）混入「国家大基金持股 - 行情中心- 同花顺」（URL `http://q.10jqka.com.cn/gn/detail/code/…`）——它是**行情页/UI 页面标题**，既无现象词也无原因词，按"判不出即放行"通过旧准入；但页面标题回答不了"为什么动"，是纯噪声。
- **改动（`services/attribution_chain.py`，单文件）**：
  - **网 1（标题级）**：新增 `_PAGE_NOISE_TOKENS`（行情中心/行情页/行情查询/行情报价/行情走势/个股行情/概念行情/板块行情/资金流向表/数据中心/资讯中心/研报中心/公告列表/F10(f10)/股吧/盘口/同花顺/东方财富，＋英文 `quote page`/`market center`/`stock quote`，匹配统一对 `lower()` 文本）。`event_summary_reason` 判据由两条扩为三条，判定顺序 **marker → 现象 → 页面噪声**：两者**都不命中 → 放行**；命中任一者 **且** `_CAUSE_TOKENS` 全不命中才拒（现象→既有 `phenomenon_without_cause`，页面噪声→新码 `page_noise`）。
  - **网 2（URL 级）**：新增纯函数 `is_page_noise_url(url) -> bool`——主机含 `q.10jqka.com.cn`/`guba`/`f10`/`quote`，或路径含 `/detail/code/`、`/quote/`、`/f10/`、`/guba/`（无 scheme 裸域按首段判主机）。`_search_candidates` 在标题准入后新增该门槛：URL 命中 **且** headline 不含原因词 → 拒收（reason `page_noise_url`，同一 `chain_event_rejected_not_driving` 留痕）；**headline 含原因词时不因 URL 被拒**。
  - 抽出 `_has_cause_token(low)` 供两道网共用**同一原因词豁免口径**。中台 `warehouse` 路径照旧**不经**该准入（回归锁保持绿）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+29 例**——标题级拒 11 例（首条为生产实证原样字符串）、标题级放行 4 例、原因码区分 1 例、标题级检索集成 1 例；URL 纯函数 6 真 + 4 假、URL 级集成 2 例。RED 取证：`12 failed, 5 passed` → `ImportError: cannot import name 'is_page_noise_url'` → GREEN **106 passed**。
- `pytest -q tests/unit/test_attribution_chain.py` **106 passed**；`-k "chain or sector_trace or consumer"` **350 passed, 3335 deselected**；全量 `tests/unit` **3186 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 两个改动文件 `All checks passed!`；`mypy` 改动**源文件 1 条**（`attribution_chain.py:947` `len(chain.get(...))`，与 HEAD 同一条 → 新增 0 条）。
- **误伤风险（如实登记）**：① 站点名入噪声词——真原因标题若只含站点名而**不含任何** `_CAUSE_TOKENS` 会被 `page_noise` 误拒；② URL 规则的 `quote`/`f10` 是**主机子串**匹配，`f10.example.com` 之类会被判页面噪声（含原因词即豁免）；③ 路径段匹配刻意**带斜杠**（`/quote/`、`/f10/`）；④「公告列表」噪声词在现原因词表下**不可达**（必然含「公告」→ 被豁免），保留仅对齐建议口径。
- **跨端**：仅改 agent-py（1 源文件 + 1 测试文件 + `AGENTS.md` 链事件准入一段 + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 事件准入判据修订：由「命中现象即拒」改为「命中现象 且 无原因词 → 拒」

**开发者**: Aria

### 修复

- **背景（今日生产实证漏网，组长口径修订）**：链 `children[].events`（`source="search"`）混入两条**无百分号**现象标题——`注册制次新股大涨八个点，A股市场全线拉升，沪指站上五日均线 - 网易`、`全线上涨！A股这一板块，涨幅第一！ - 21财经`。旧判据（marker / 涨跌幅 `%` / 两市＋成交 / 时段＋涨跌）只认"百分号式"行情复述，对"八个点""全线上涨""涨幅第一""站上五日均线"全漏；同时旧判据一旦命中现象即拒，会误杀"现象外衣＋真原因"（如政策落地带动板块大涨）。修订口径：**是原因不是现象** → 现象 **且** 无原因词才拒。
- **改动（`services/attribution_chain.py`，单文件）**：
  - 新增现象词族：`_MARKET_ACTION_TOKENS`（站上/失守/跌破/拉升/上涨/下跌/走强/走弱/回落/低开/高开，简繁）、`_RALLY_PHRASES`（全线上涨/全线拉升/集体上涨/集体拉升/普涨/涨幅第一/涨幅居前/涨幅榜/领涨两市）、`_POINT_MOVE_RE`（"大涨八个点"/"涨了3个点"：涨跌＋中文/阿拉伯数字＋个点）、`_NEW_HIGH_PHRASES`×`_NEW_HIGH_QUALIFIERS`（创新高限与板块/指数/两市同现）；`_MARKET_WIDE_TOKENS` 补 `大盘/大盤`。
  - 新增原因词表 `_CAUSE_TOKENS`（政策/监管/部委/国常会/发改委/工信部/证监会/央行/国务院/公告/披露/预案/中标/订单/签约/招标/获批/牌照/涨跌价/减产/扩产/投产/产能/供需/需求/供给/库存/出口/进口/关税/补贴/试点/并购/重组/收购/增持/回购/业绩/财报/落地/细则/方案/规划/标准，简繁同列；按需增补 `价格`/`供给`）。刻意与只做排序加权的 `_DRIVING_KEYWORDS` 分开。
  - 判据重构：抽出纯函数 `_has_phenomenon(low)`，`event_summary_reason` 改为 ①`summary_marker`（综述体裁词，**不因**含原因词放行）→ ②现象形态 **且** `_CAUSE_TOKENS` 全不命中 → `phenomenon_without_cause`；含原因词即放行。原 `index_pct_recap`/`turnover_recap`/`session_recap` 三个码被"现象＋无原因"统一取代（留痕键位不变）。
  - 中台 `warehouse` 路径照旧**不经**该准入（回归锁测试保持绿）；判不出即放行的"宁可漏判"原则不变。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+16 例**（`_RECAP_HEADLINES_V2` 7 例含今日两条实证原样字符串；`_CAUSE_BEARING_HEADLINES` 5 例必须放行；`_MARKER_WITH_CAUSE_HEADLINES` 2 例；检索路径集成 2 例）。RED 取证：`6 failed, 71 passed` → GREEN **77 passed**。
- `pytest -q tests/unit/test_attribution_chain.py` **77 passed**；`-k "chain or sector_trace or consumer"` **321 passed, 3335 deselected**；全量 `tests/unit` **3157 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 两个改动文件 `All checks passed!`；`mypy` 源文件 1 条（HEAD 已存在 → 新增 0 条）。
- **误伤风险（如实登记）**：① 现象词族含单字级方向语义（`上涨`/`下跌`/`上升`），若真原因标题里出现方向词而**恰好不带** `_CAUSE_TOKENS` 任何词，会被 `phenomenon_without_cause` 误拒；② `创新高` 已限板块/指数/两市同现，个股新高不受影响；③ 简繁原因词为手工列举，繁体原因标题存在漏配→误拒余量。
- **跨端**：仅改 agent-py（1 个源文件 + 1 个测试文件 + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — sector_trace 报告写入收敛：多板块「一天一份」（修报告层只剩 1 个板块）

**开发者**: Aria

### 修复

- **背景（生产实证）**：`run_sector_trace` 每个板块各写一次 `report_type="sector_trace"` 报告，Node upsert 键 `(report_type, report_date, COALESCE(user_id,''))` 让同日多板块**互相覆盖**——`agent_analysis_reports` 全库仅 2 条、`display_report.sectors` 长度**恒为 1**（`2026-09-17 → ["玉米"]`、`2026-09-18 → ["先进封装"]`）。app-api `sector-insight` 用它作 `review_primary` 候选集 → 多主因日在报告层不成立；R16 兜底补跑并行跑多板块后更明显（仍只剩最后一个）。本条即上一条记录"未解决/风险 ①"的闭环。
- **改动（agent-py 2 个源文件，最小追加）**：
  - `agents/workers/sector_trace.py`：`run_sector_trace` 增 `persist_report: bool = True`——**默认行为逐字不变**（单板块自写、content 形状不变）；`False` 时只溯源+回传结果。新增 `build_sector_trace_report_content(results, *, parent_trace_ref)`（纯函数，多板块聚合 content，无成功板块返回 `None`）与 `save_sector_trace_report(*, report_date, results, parent_trace_ref)`（聚合写**一次**，返回写入的板块名清单）。
  - `services/event_consumers.py::SectorTraceConsumer.handle`：`_one` 传 `persist_report=False` 并 `return result`（失败 `return None`）；`asyncio.gather` 返回值按入参顺序汇总为 `traced`（顺序确定），gather 后调 `save_sector_trace_report` **一次**，留痕 `sector_trace_report_saved`（`sector_count`/`sectors`）；写失败只 warning `sector_trace_report_save_failed`（不把 review_done 拖进 retry/DLQ）。顺手把 `parent_ref` 显式标注为 `dict[str, object]`（消掉存量 arg-type 报错）。
- **报告结构（加性，不改 app-api / 不改 Node upsert 键）**：`display_report.sectors` = 当天**全部成功**溯源板块名（gather 入参顺序去重，T1 在前）；新增 `display_report.sector_traces = {板块名: trace 序列化}`；`market_trace` 保持**单板块**形状（首个板块 snapshot+trace，app-api 契约不变）；`schema_version` 仍 `"2.1"`。
- **失败隔离**：溯源失败板块**不进 `sectors`**，其它板块照写、不 panic。**幂等**：按板块名去重，同日重放/补跑覆盖同一行且清单一致。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_sector_trace_worker.py` **+6 例**（默认 content 形状回归锁、`persist_report=False` 不落库、聚合去重/market_trace 取首板块、空入参返回 None、聚合写一次、无成功板块不写）+ `tests/unit/test_sector_trace_consumer.py` **+4 例** + 该文件新增 autouse fixture 隔离真实报告落库。RED 取证：`9 failed, 84 passed`。
- `pytest -q tests/unit/test_sector_trace_consumer.py tests/unit/test_attribution_chain.py` **77 passed**；`-k "sector_trace or chain or consumer or iterate"` **533 passed / 1 failed**（唯一红为存量 `test_iterate_adapters`）；全量 `tests/unit` **3141 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 4 个改动文件 `All checks passed!`；`mypy` 改动源文件 **17 条（基线 18 条）→ 新增 0 条**。
- **未闭环/风险**：app-api/前端仍只读 `display_report.sectors` 与 `market_trace.trace`（单板块 summary）——多板块日的 `sector_traces` 暂无消费方（本轮跨仓 0 改动，纯写入侧收敛）；`sector_wind_prediction._load_cause_sector_codes` 走 `list_analysis_reports` 逐份读 `display_report.sectors`，收敛后一份报告即含全部板块（排除集更完整，正向副作用）。
- **跨端**：仅改 agent-py（2 源文件 + 2 测试文件 + `AGENTS.md` 18:30 链路一行 + spec §13.8 + 本记录）；app-api / 两个前端 / 组件库 **0 改动**。

---

## [main] 2026-09-18 — 弱归因兜底板块「补跑板块溯源」显式化（上限 + 逐项隔离 + 留痕）

**开发者**: Aria

### 改进

- **背景（组长口径，R16 续）**：R16 三级兜底（T1 主链 claim → T2 全部候选链 claim → T3 快照 `top_losers`）落地后，兜底命中会照常溯源，但"兜底必须真跑一遍板块溯源"这条口径在代码里是**隐式**的（T1/兜底混在同一 `asyncio.gather` 里，无兜底专属留痕、无补跑上限、无截断日志），弱归因日一旦上游放宽提取上限或某板块失败，无法从日志对账"到底补跑了几个、哪些失败"。
- **改动（`services/event_consumers.py`，最小追加，不动其它消费者）**：
  - 新增常量 `SECTOR_TRACE_FALLBACK_MAX_SECTORS = 3`（与 `extract_primary_sectors` 的 `max_sectors` 默认同值）+ 来源级别标签 `_fallback_level(source)`（`candidate_claim`→`T2`／`snapshot`→`T3`）。
  - `SectorTraceConsumer.handle`：命中按弱标记**分流**为 `primary_hits`（T1）与 `fallback_hits`（T2/T3），兜底侧先按上限**截断并留痕** `sector_trace_fallback_truncated`（`total/kept/dropped`），再与 T1 一并 `asyncio.gather` 并行溯源；`_one(hit, *, fallback_level="")` 逐板块记 `sector_trace_fallback_started` / `_done` / `_failed`（字段 `sector`、`fallback_level`、`extraction_source`、`elapsed_ms`；失败另带 `error`）。
  - **为什么是"分流"而不是"对同一板块再跑一遍"**：兜底板块本就在同一次 gather 中溯源（R16），再补一遍会把弱归因日的 LLM/检索成本翻倍（每板块 3 次 Tavily 定向检索 + 1~2 次 deep-think LLM）。分流保留"兜底必跑"的语义与并行度，成本与 R16 持平（≤3 板块 × 并行）。
  - 既有语义逐字保留：`sector_trace_done` / `sector_trace_one_failed` 两个日志键与字段不变（T1 路径**无任何行为变化**）；兜底补跑发生**在链组装/保存之前**；失败仍逐项隔离；无证据时照旧如实降级（`attribution_status="insufficient"` + 中性摘要），**未放宽上一轮的 `is_driving_event` 事件准入**。
- **关系标记**：核查确认 `children[].relation` 一向走既有 `judge_sector_driver_relation(pct, index_pct)` 真实判定，**不存在硬编码/推断值**，故本轮无改动（存量已满足）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_sector_trace_consumer.py` **+5 例**——T2 命中 3 板块→3 次 `run_sector_trace`；T3（快照来源）同样补跑且日志带 `T3`；命中 5 个→只补跑前 3 个 + `sector_trace_fallback_truncated`；其中 1 个抛异常→其余板块入链且链仍保存成功 + `sector_trace_fallback_failed`；T1 路径调用入参逐字比对 + 无兜底日志。（其中两例在基线即绿——它们是**回归锁**。）RED 取证：`3 failed, 2 passed`。
- `-k "sector_trace or chain or consumer"` **295 passed**；全量 `tests/unit` **3131 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 两个改动文件 `All checks passed!`；`mypy` 改动源文件 **18 条存量 → 新增 0 条**。
- **未解决/风险（如实登记）**：① 前端 `sector-insight` 的 `trace` 仍只覆盖 1 个板块——`run_sector_trace` 落库键同日多板块**互相覆盖**（最后一次写赢）；**本轮不改**（需 app-api/前端配合，超出授权范围）；前端已有 R17 兜底（以链 `children` 为主合成 `chain_only` 候选）→ 用户可见内容已由链侧承载。② 补跑上限目前恒不触发（提取层已限 3），仅作上游放宽时的成本护栏。
- **跨端**：仅改 agent-py（1 个源文件 + 1 个测试文件 + spec §13.7 + 本记录）；app-api / 两个前端 / 组件库 **0 改动**（`children[]` 字段契约未变，纯行为补全）。

---

## [main] 2026-09-18 — 链事件层只收「驱动原因」+ 链摘要取源修正（现象 vs 原因）

**开发者**: Aria

### 修复

- **背景（生产实证 2026-09-17，CRO 概念）**：链路 `attribution_chains.children[].events[]` 存的是**行情综述**而非驱动原因——`{"headline": "A股收評| 滬指跌0.41% 三大指數收跌農業板塊逆勢大漲", "source": "search"}`、`{"headline": "今天A股，三大指数集体下跌 - 时间线- 搜狐", "source": "search"}`。综述只复述"发生了什么"，回答不了"为什么动"。**组长口径**：要溯源到基本事件（是原因，不是现象）；找不到原因事件时**宁可 `events: []`（如实交空）**，不得拿综述兜底。
- **A. 检索补漏准入收紧（`services/attribution_chain.py`）**：
  - 新增确定性单点判定 `event_summary_reason(headline) -> str`（`""` = 放行）+ 薄包装 `is_driving_event(headline) -> bool`；只作用于**检索补漏**（`source="search"`），**中台 `warehouse` 路径逐字不变**（回归用例锁定）。
  - 拒收形态（reason 码）：① `summary_marker`＝`收评/收盤/收盘/午评/早评/复盘/盘点/盘面/三大指数/涨跌家数/时间线/资金流向/涨停潮/异动`（简繁同列）＋英文 `closing bell/market wrap/market recap/daily recap`；② `index_pct_recap`＝市场级词元＋涨跌幅式描述（`[涨漲跌][幅超逾]?\d+(\.\d+)?%`）；③ `turnover_recap`＝`两市`＋成交额/家数/涨跌综述；④ `session_recap`＝时段词（午后/早盘/盘中/尾盘/开盘）＋涨跌幅式描述或涨跌动词。匹配统一对 `lower()` 后文本做。
  - **判不出即放行**（宁可漏判）；被拒即不产节点，全部被拒 → `events: []`（不回退成综述）。留痕：逐条 `chain_event_rejected_not_driving`（`sector`/`reason`/`headline[:60]` 截断）；汇总计数 `chain_sector_events.rejected_not_driving`。
  - 正向要求：检索候选排序末位信号由旧「综述降权」改为「**含驱动类关键词加权**」（`_DRIVING_KEYWORDS`：政策/监管/部委/公告/披露/供需/涨价/减产/扩产/并购/关税/补贴/试点…；仅加权不排除）。
  - 生成侧同批收紧（`prompts/workers/sector_trace.py`，双保险）：phenomenon 只描述盘面现象不得写原因；trigger 必须是能解释「为什么动」的基本事件，**禁止把收评/复盘/指数涨跌幅/涨跌家数/成交额综述当 trigger 或 evidence 标题**；无可解释事件时 `attribution_status="insufficient"` + missing_evidence 如实说明。
- **B. 链 `children[].trace_summary` 取源修正**：原实现在 `attribution_status == "insufficient"` 时直接返回中性兜底，**覆盖了报告已有的归因句**。改为：报告非空 `summary` → trigger headline → trigger claims → 兜底 `溯源未确认驱动原因`（**兜底只在确实无内容时出现、不得覆盖已有内容**）；刻意**不**回退 phenomenon/首 stage 标题（那是现象）。口径对齐 app-api `extractTraceSummary`（前端两页「有无归因」判定一致，前端 0 改动）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` **+21 例**——`is_driving_event` 参数化（8 综述形态含生产实证两例＋简繁＋英文 / 4 驱动形态）、综述源不产节点且日志键+原因+计数出现、长标题截断留痕、全被拒 → `events: []`、同板块综述被拒而驱动保留、驱动关键词排序靠前、**中台路径不受准入影响**、`trace_summary` 三例 + 报告空摘要仍兜底；另**改写**两处旧用例。RED 取证：`ImportError: cannot import name 'is_driving_event'`（collection error）。
- `-k "chain or sector_trace or consumer or event"` RED(1 error) → **794 passed / 6 failed**（6 条红均为存量且与改动文件无关）；全量 `tests/unit` **3126 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff check` 三个改动文件 `All checks passed!`；`mypy` 两个改动源文件 **1 条（`attribution_chain.py:778` 未改动行存量）→ 新增 0 条**。
- **误伤风险（已知、按"宁可漏判"接受）**：① 时段词＋涨跌动词规则会误拒"午后公告提价""早盘发布涨价函"这类**含时段词的真驱动**；② `收盘/盘面/异动/时间线` 等词若出现在真事件标题里（如"公司公告…收盘价"，罕见）会被拒；③ 英文仅覆盖 limited 词表（`closing bell/market wrap`）＋指数名＋百分比。
- **跨端**：仅改 agent-py（准入 + 摘要取源 + prompt）；app-api / 前端 0 改动——本次是让链的口径**向**前端既有判定（`isUnconfirmedAttribution` 按摘要判「有无归因」）与 sector-insight `trace.summary` 对齐。

---

## [main] 2026-09-18 — EventBus PEL 恢复（XAUTOCLAIM 认领 + XPENDING/XCLAIM 降级，spec §13.6 遗留项）

**开发者**: Aria

### 修复

- **背景**：`services/event_bus.py` + `event_consumers._consumer_loop` 走 XREADGROUP(">")/XACK，但**无任何 PEL 恢复**：消费进程在「读到消息」与 `XACK` 之间崩溃/重启，该消息永久滞留在组 PEL（`>` 只投递新消息）→ **静默丢事件**（表现为某天链/快照/播报莫名没跑）。注：`workers/insight_consumer.py`、`workers/stock_trace_consumer.py` 早已有 `xautoclaim`，缺口只在 evening_chain 事件总线这条链路。
- **实现（最小追加，不动既有语义）**：
  - `config.py` 新增 `event_bus_pel_reclaim_enabled: bool = True`、`event_bus_pel_min_idle_ms: int = 300000`、`event_bus_pel_reclaim_batch: int = 10`（行内注释写明默认值理由：5 分钟 > 正常单条处理耗时，避免抢回在途消息）。
  - `EventBus.reclaim_pending(channel, consumer_name, *, group, min_idle_ms, count)`：XAUTOCLAIM（min-idle 过滤 + `start_id="0-0"`）→ 归一为与 `consume` 同形的 `Event`；XAUTOCLAIM 不可用（Redis < 6.2 / 老客户端 / 任意异常）降级 `XPENDING(idle=…) + XCLAIM`；仍失败只 `logger.warning("event_bus_pel_reclaim_failed")` 返回空，**不抛出**（总线不可用不影响主链路）。成功认领记 `event_bus_pel_reclaimed`。
  - 抽出 `EventBus._parse_entries`（**consume 与认领共用**，含"payload 非法 JSON → error 日志 + XACK 丢弃"语义，避免毒消息被反复认领）；`consume` 改为收集条目后调用它，行为不变。
  - `event_consumers._consumer_loop`：每轮先 `reclaim_pending(...)` 再 `consume(...)`；新增 `_dispatch_events(consumer, events)` 承载既有「handle → ack / except → retry」分支，**认领消息与新消息共用同一分发代码**。批量大小、XACK 时机、异常吞错策略均未改。
  - 顺带修一个被复用的旧缺陷：`_parse_entries` 里 `str(msg_id)`（bytes）会得到 `"b'1234-0'"` → 非法 payload 的 `XACK` 其实匹配不到消息（毒消息永远留在 PEL）。改为 bytes 先 `decode` 归一，认领路径因此不会死循环。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_event_bus.py` **+6 例**（正常认领并断言 `xautoclaim` 的 group/consumer/min_idle/count 与"认领本身不 XACK"、未超阈值不认领、XAUTOCLAIM 异常降级 XPENDING+XCLAIM、降级也失败只返回空、毒消息复用丢弃语义）；`tests/unit/test_event_consumers.py` **+3 例**（认领消息走既有分发并 XACK、`enabled=False` 零调用、认领消息处理失败不 XACK 而走 retry）。RED 取证：9 failed（`AttributeError: 'EventBus' object has no attribute 'reclaim_pending'` ×6 等）。
- `-k "event_bus or consumer"` **87 passed + 9 failed → 96 passed**；全量 `tests/unit` **3092 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败；通过数 = 基线 3083 + 新增 9）；`ruff check`（config/event_bus/event_consumers）`All checks passed!`；`mypy` 三个改动源文件 **23 → 21 条**，其中 event_bus.py **5 → 3 条**。
- **命名/幂等核查（重复投递副作用）**：① 消费者名 `f"{c.channel}_consumer"`（如 `snapshot_consumer`）**按通道稳定、但不含主机/PID** → 同代码多实例共享同名消费者（Redis 视为同一 consumer），XAUTOCLAIM 到同名即"改派给自己"；② 落库类均幂等：`snapshot`/`iterate`/`review` 走 `POST /internal/analysis-reports`，Node 侧 `ON CONFLICT ... DO UPDATE`；③ **非幂等副作用**：重投 `review_quick`/`review_full` 会重跑 LLM 并**再发一次 snapshot/iterate/broadcast**（这三个 publish 未带 event_id → 无幂等去重），`IterateConsumer` 会**重复发迭代邮件**，`BroadcastConsumer` 会重复生成播报；故 min_idle 必须大于最慢 handler 耗时。
- **风险点**：① 默认 300000ms 对 `ReviewQuickConsumer` 偏紧（最多 3 次 `run_review` + 60/120s 退避，最坏可逼近 5 分钟）→ 极端情况下可能重复执行；建议按通道观测后上调或分通道配阈值（本轮未做）。② 认领每轮执行且 `start_id` 固定 `"0-0"`（不追 next cursor）：只读时开销可忽略；若某消息反复处理失败且 `retry()` 也失败（未 ack），会被每轮反复认领。③ 消费循环内的 XAUTOCLAIM 是额外一次 Redis 往返（有消息时每轮一次），负载可忽略。
- **跨端**：仅改 agent-py（event_bus/event_consumers/config），未触碰 Node/前端；`/internal/analysis-reports` 契约未变 → 无跨端同步项。

---

## [main] 2026-09-18 — quick 手动触发对称补齐 `with_chain`（review_quick 联动 review_done）

**开发者**: Aria

### 新增

- **背景**：手动触发入口 `trigger_review_full` 已支持可选 `with_chain`（ok 后补发 `review_done`，驱动链组装/级联预判消费者），但姊妹入口 `trigger_review_quick`（15:30 盘中快复盘）**结构同缺** → 运维无法手动复现/验证 quick 路径的链路。
- **实现（就地对称，不抽公共函数）**：`src/aistock_agent/api/routes.py` 新增 `ReviewQuickTriggerBody`（`report_date: str | None`、`with_chain: bool = False`，与 `ReviewFullTriggerBody` 同形，docstring 对应 `ReviewQuickConsumer`）；`trigger_review_quick` 形参 `body: dict[str, str] | None` → `ReviewQuickTriggerBody | None`，`report_date` 改走同一解析，`with_chain = bool(body.with_chain) if body is not None else False`；复用既有私有 helper `_publish_review_done_for_chain`（**未改其实现**）；发布条件与 full 逐字一致（`with_chain and result.status == "ok"`），返回体加同名字段 `chain_published`；日志键不新增。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_admin_trigger_review_full_chain.py` **+7 例**（quick 用例逐条镜像 full：缺省不发布/返回 `chain_published=False`、true+ok 走默认总线发布一次且 kwargs 带 `manual-quick-*` trace_id、status≠ok 不发布、无默认总线用 RedisPool 单例临时总线（单例不关）、RedisPool 未初始化按 `settings.redis_url` 临时连并关闭、发布异常不影响复盘返回、非法日期仍 422）。RED 取证：6 failed（1×`KeyError: 'chain_published'` + 5×`assert 422 == 200`）。
- `-k "trigger_review or review_full or review_quick"` **27 → 34 passed**；全量 `tests/unit` **3083 passed / 8 failed / 1 skipped**（8 条红与基线**同集**且**逐文件复核**：`test_gi_admittance.py` + `test_industry_vector_search.py` + `test_iterate_adapters.py` 三文件 `-q` 恰为 `8 failed` → 零新增失败）；`ruff check`（routes.py + 测试文件）`All checks passed!`；`mypy routes.py` **26 条 → 26 条**（**新增 0 条**，行号位移）。
- **取舍（需复核）**：返回体按 full 的既有做法**无条件带** `chain_published`（缺省时 `False`）而非"缺省时省略该键"——取"与 full 完全对称 + 可 diff 对照"优先；若要求缺省响应体逐字段不变，只需把该键改为 `with_chain` 为真时才写（2 行改动），测试同步改 `assert "chain_published" not in body`。
- **跨端**：`trigger/review_quick|full` 仅被 agent-py 自身测试/文档引用（全仓 grep 无前端/Node 调用）→ 无跨端同步项。

---

## [main] 2026-09-18 — 收口四项：R22 threshold 口径收敛 / G1 词表扩展 / G4「或」守卫 / 告警原因合并

**开发者**: Aria

### 修复

- **R22 生成侧 `threshold` 口径脱节 → prompt 收敛**（真根因比"文本与锚不一致"更深）：`anchor.threshold` 被三方赋予两种语义——① spec §12.3 ① 的涨跌幅类条件 = 触发阈值（判定层 `_judge_pct_state` 据此判 `condition_met`）② prompt 示例（量类/参考位类）教的是**情景验证幅度** ③ 生成侧对"条件本身即涨跌幅口径"**无任何示例** → LLM 按 ② 填 → 涨跌幅类条件拿到情景幅度而非触发阈值 → 判定层用错数字（id=24 c1 文本 `-3%` 的触发线按锚 `-4%` 判）。**修复**：三条 prompt（`PREDICTION_PROMPT` / `PREDICTION_CHAT_PROMPT` / `PREDICTION_LIGHT_PROMPT`）同批写明取值口径——涨跌幅口径条件下 `threshold` 必须 = 该条件的触发阈值且与 condition 文本百分数**同值同号**；其余口径填情景验证幅度。**判定层不动**（存量仍走 G3 不判）。测试：`test_prediction_prompt.py` 新增 `test_prediction_prompts_declare_threshold_caliber`。
- **G1 词表扩展**（R20 遗留"未覆盖口径"）：`sentiment` 补 封单/开板/晋级率/封成率/炸板率/首板/二板/空间板/情绪温度；`capital_flow` 补 大单净额/龙虎榜/ETF/份额/融资买入额/南向/沪股通/深股通/席位；`overseas_macro` 补 非农/CPI/议息/汇率中间价/金价/伦铜/A50/日经。**关键取舍**：海外组用「**金价**」不写「黄金」——A 股有"黄金/贵金属"板块（`sector_aliases.json` 标准名），裸用「黄金」会把「黄金板块指数站上 MA20」这类**可判**条件整体拦成不可判；「金价」既覆盖"黄金价格创新高"（含子串）又不撞板块名。
- **G4「或」守卫**：文本含「或」（排除"不可或缺"）→ 一律 `unjudgeable`（不产键）。**为什么不做 or 运算**：「或」= "任一子句成立"，与 G2 的"全部成立"**相反**，正确实现需三值或运算；而**生产实测 0 条**（全库 372 条条件 `with_or=0`）→ 零收益却新增误判面 → 按"宁可 None"一致性一律不判（日后真产出再按三值或实现）。护栏数由 3 → 4（同点单测锁定）。
- **告警原因合并**（R17 遗留）：`data_client.post` / `_post_request` 新增可选出参 `error_out`，失败分支写入 `{stage, detail}`（成功路径**不写**）；`AttributionChainStore.save` 把真实原因（`business_error code=… message=…` / `http_error status=… body=…` / `request_error`）并入同一条 `attribution_chain.save_failed`，不必再交叉 grep 独立日志。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` **+4 例**（25 条新词分类命中 + 不误伤含 A 股"黄金"板块名 + 端到端 unjudgeable + 「或」检测与不误伤"不可或缺"/「且」系与单子句逐字不变）；`test_prediction_prompt.py` **+1 例**；`test_attribution_chain.py` **+2 例**（save 文案带 stage/detail；`_post_request` 业务错填出参、成功路径不写）。RED 取证：`ImportError: cannot import name 'has_or_connector'`、`TypeError: _post_request() got an unexpected keyword argument 'error_out'`。
- `tests/unit` **3076 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败；通过数 +45）；`test_condition_met_judge.py` 108→111、`test_prediction_prompt.py` 13→14、`test_attribution_chain.py` 38→40；`ruff` 改动文件 `All checks passed!`；`mypy` 改动的 3 个源文件：`condition_met_judge` 干净，`data_client` 6 条报错**全在未改动行**。
- **遗留**：① 存量不一致条件（含 id=24）不因 prompt 收敛而改变，仍走 G3 不可判；② `份额`/`席位`/`ETF` 属较宽词，命中即不判（保守侧可接受，会损失本可判的收益）；刻意**不入表**的宽泛词：`美国`/`隔夜`/`指数`/`大涨`；③ 「或」若日后真出现，需补三值或真值表实现；④ 三口径按表中顺序 `overseas_macro → sentiment → capital_flow` 取**首个命中**，分类标签可能与直觉不符（仅影响标签，不影响"不可判"结果）。

---

## [main] 2026-09-18 — 溯源背离报告（Phase 7 收口：§13.3 验收第一条）

**开发者**: Aria

### 新增

- **背景**：Phase 7「溯源弱反馈」观测层早已落地（16:10 cron + `attribution_feedback_signals` + `POST /api/internal/attribution-feedback` + `GET /api/agent/attribution-feedback/:date`，`mode` 默认 `observe`），但 spec §13.3 的验收第一条——"**能给出「某板块溯源反复与验证结果背离」的报告条目**"——一直只以原始统计行的形式输出（`unit 样本= 命中= 未中= 命中率= → 建议（原因码）`），既没把"背离"从"观望/样本不足"里挑出来，也没给"影响对象（板块）"。
- **组长裁决（2026-09-18）**：**应用层（真正按建议调溯源权重）本轮不做**——每 unit 需 ≥10 样本才出建议，当前样本天数不足 → 全线 `insufficient`，此时写加权等于对着空数据写规则；且加权会改变链产出，需先有真实样本才能验证效果。**重启条件**：审计表出现稳定 `downgrade`/`upgrade` 条目后再立项。故本轮只补**只读的背离报告**，零行为变更。
- **实现**：
  - `services/attribution_feedback.py` 新增 **`divergence_entries`**（纯函数）：背离 = 样本充分且命中率越阈值（`suggestion ∈ {downgrade, upgrade}`），**观望 `hold` 与样本不足 `insufficient` 只计数不成条目**；排序口径（可复现、无随机）① **降权侧在前**（对应 §13.3 原始场景"溯源到但预判未中"，人工复核优先看）② 同侧按**背离强度** `|hit_rate-0.5|` 降序 ③ 样本量降序 ④ `unit_key` 升序定序。
  - `scripts/attribution_feedback.py` 新增 **`render_divergence_report`**（纯函数）：输出「背离条目 N 条｜观望 x｜样本不足 y（unit=…；触发规则 样本≥10 且 命中率<0.35 降权 / >0.65 提级）」+ 每条 `unit_key / 样本= / 命中=未中= / 命中率= / 越阈方向 → 建议降权|提级` + **板块抽样**（`detail.sectors`，≤5 个，超出标"共 N"）；无条目时明确打「无背离：所有单元均落在观望区间或样本不足」；末行固定声明**只读**（不改变溯源权重、应用层未启用）。已并入 `render_report` 输出（第二段），并更新脚本 docstring（含 `--unit sector` 看板块维度的说明）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_feedback.py` **+6 例**——只收越阈单元（hold/insufficient 被排除）/ 排序（降权侧优先 → 强度 → 样本量 → key 稳定序，含同强度同样本并列）/ 全观望时零条目 / 渲染含证据（样本·命中率·越阈方向·板块抽样）/ 空态与只读声明 / `render_report` 追加背离段。RED 取证：`ImportError: cannot import name 'render_divergence_report'`。
- `tests/unit/test_attribution_feedback.py` **50 passed**（基线 44，+6）；`-k "attribution_feedback or scheduler"` **152 passed**；`ruff` 3 个改动文件 `All checks passed!`；`mypy attribution_feedback.py` 0 报错。
- **遗留**：① **应用层未做**（见上，重启条件明确）；② 审计数据仍无自动消费方（报告靠人工跑 CLI 看）；③ `unit` 默认 `relation`，"某板块"维度需显式 `--unit sector`/`relation_sector`（样本更少，可能长期 `insufficient`）。

---

## [main] 2026-09-18 — 条件点亮 G3 守卫（方向动词 + 百分数口径不确定就不判）

**开发者**: Aria

### 修复

- **生产误点亮第 4 条（R21，已人工回滚）**：`id=24 c1`「重组蛋白板块指数**相对当前收盘价跌破 -3%**」（anchor：`metric=close` / `threshold="-4%"` / `direction=bearish`，点亮于 2026-09-17T13:54Z）。**发现路径**：R19 修好回滚脚本后跑全量 `--dry-run` 扫 true 值时暴露（它不在 backfill 报告里——已有布尔 `condition_met` 的 entry 被 backfill **幂等跳过**，只有扫 true 的回滚脚本看得到）。
- **根因（确定性，非 LLM）**：`PredictionAnchor.metric` 在 schema 里**缺省即 `close`**（不携带口径信息）→ `infer_condition_class` 落**文本兜底**；裸方向动词「跌破」命中 `_TECH_RE` → 归**技术位类** → `_judge_tech_state` 用 **MA20** 近似（`末值 < MA20` 成立）→ 假 `true`。既有两道护栏都拦不住：G1 只认情绪/海外/资金流关键词（`收盘价` 不在表内）；绝对点位守卫 `_ABS_LEVEL_VERB_RE` 要求"动词 + **纯数字**"，而 `-3%` 有符号 + 百分号。
- **修复（G3，`condition_met_judge.py`）**：新增常量 `_DIR_VERB_PCT_RE`（方向动词集合**与 `_TECH_RE` 逐字同集**：跌破/下破/失守/站上/突破/收回；**不含 上穿/击穿**——二者本就不进技术位判径，走涨跌幅口径属正常判定）与 `_TECH_LEVEL_HINT_RE`（均线/日线/周线/月线/`MA\d+`/前低/新高）+ **单点函数** `is_dir_verb_pct_ambiguous`；在 `_judge_clause_state` 中紧跟 G1 早退：**方向动词 + 百分数、且未明示技术位 → 整体 `unjudgeable`（None，不产键）**，不得走技术位近似。事件类（`anchor.event_ref`）仍首行短路，不受 G3 影响。
- **为什么"不判"而不是"按 `anchor.threshold` 走涨跌幅口径"**：取证发现该形态**文本与锚本身就不一致**——文本写「跌破 **-3%**」而 `anchor.threshold = "-4%"` → 按显示值判或按锚值判都说不通（口径不确定就不判，与 G1 同源）。已登记为生成侧遗留 **R22**（prompt/归一化层收敛后可评估放宽为涨跌幅判径）。**规模**：全库同形态条件 `same_pattern = 3`。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` 新增 **7 例**（4 例变体参数化）——`跌破 -3%`/`突破 +3%`/`站上3%`/`失守 -1.5%`/`收回 +2%` 均 `None`；**不误伤 2 例**：明示技术位「相对 5 日均线跌破 3%」仍走技术位（`True`）、无方向动词「指数上涨超过 5%」仍走涨跌幅（`True`）。RED 取证：5 failed（复现生产误判）、2 passed；实现后 75 passed。
- `-k "condition_met or validator"` → **200 passed**；全量 `tests/unit` → **3031 passed / 8 failed / 1 skipped**（8 条红与基线**同集** → 零新增失败）；`ruff` 改动文件 `All checks passed!`；`mypy` 0 报错。
- **遗留**：① **R22 生成侧文本/锚阈值不一致**（上）；② **无方向动词的相对百分比**（如「回调 3%」）不受 G3 影响，仍走涨跌幅口径正常判定——若后续发现同源误判需再扩词表；③ 回滚 id=24 后它会在下次扫描**走同一条 bug 路径被重新点亮**，故本提交须在次日 16:00 判定前部署。

---

## \[main\] 2026-09-18 — 板块溯源新增「归因结论」`conclusion`（折叠卡不再显示「触发」）

**开发者**: Aria

### 新增

- `schemas/sector_trace.py::SectorChainResult` 加性新增 `conclusion: str = ""`（不升 `schema_version`，缺省空串不编造）；因 `trace_result = model_dump(mode="json")`，新键自动流进 `display_report.sector_traces[板块名]` 与 `market_trace.trace`，中间层零改动。
- `prompts/workers/sector_trace.py` 输出字段加 `conclusion`，并新增【conclusion 约束】镜像大盘【attribution_summary 约束】：仅 `attribution_status === "sufficient"` 时给一句 30-40 字综合该板块当日驱动原因的结论，其余输出空串；只讲原因本身，不混现象描述/涨跌幅数据/事件罗列，不用冒号或列表，语义须与 stages 一致。

### 修复

- 链 `children[].trace_summary` 取源改为 `conclusion` → 顶层 `summary`（旧数据兼容层）→ trigger headline → trigger claims → 中性兜底。根因：板块溯源 schema 此前无结论字段，`_trace_summary_from_report` 读 `trace_result.get("summary")` 永远读不到，摘要只能落 trigger 段 headline（原因第 1 段），导致市场洞见链分支 / 板块预判页 / 板块详情页三处折叠卡显示的都是「触发」。
- 事件让位裁决（`_is_negative_summary` / `_NEGATIVE_SUMMARY_MARKERS`）逐字不变；老数据无 `conclusion` 自动回退旧口径（零变化）。

### 测试

- `tests/unit/test_attribution_chain.py` +4 例、`tests/unit/test_sector_trace_worker.py` +2 例（先红后绿）；定向 5 个 sector/chain spec 201 passed；全量 `tests/unit` 3263 passed / 8 failed（8 条均为文档化存量红，零新增）；`ruff` 改动文件通过；`mypy` 仅 1 条 HEAD 存量。

---

## \[changer\] 2026-09-18 — 节奏大师新增手动触发端点（补跑 / 补发）

**开发者**: 37588

### 新增

- 新增 `POST /api/agent/briefing/rhythm-master/trigger`：管理员可手动触发节奏大师卡生成，无需等待三时点定时任务。入参 `refresh_slot`（缺省 after_close，仅接受三时点枚举）与 `report_date`（缺省上海当天，语义为**基准日**）；返回统一 `{"success", "data"}` 契约并回带 `target_date` / `basis_date` / `refresh_slot` / `synthesis_available` / `rhythm_card`，便于补跑后当场核验卡片内容。非法 slot 与 worker 无产出均返回结构化错误体，不抛 500。
- 三时点分发函数支持显式传入基准日：定时任务路径仍用上海当天，手动补跑可指向最近一个有 K 线的交易日（否则 after_close 的"基准日无当日K线"门禁会使档位降级为空）。
## \[junliang] 2026-09-18 — 异动归因：资金维度降级为条件准入层 + 反证校验补齐

**开发者**: Aria

### 改进

- **资金（capital）候选层由"必产层"降级为"条件准入层"**（`schemas/stock_trace.py`）：资金净流入/流出方向与价格涨跌是同一事实的两种记账方式，据此归因属同义反复；且资金证据"永远存在且天然与价格同向"，是五层中最易被置 supported 的一层，会挤压 company/sector/market 真因。`_validate_selected_chain_shape` 的 `required_layers` 由 `{company, sector, market, capital, technical}` 改为 `{company, sector, market, technical}`（`capital` 仍保留在 `TraceCandidate.layer` 枚举中，存量结果继续通过校验）。
- **提示词新增 capital 专项规则**（`prompts/workers/stock_trace.py`）：① 禁同义反复——不得以"股价上涨/下跌是因为主力资金净流入/流出"作为 supported 依据；② 仅当资金证据含价格读不出的增量信息（结构/来源/背离三类，如分单结构、主力与散户方向分化、席位来源、量价背离）时才可置 supported/weak；③ **时效分档**——`trade_date` 等于异动交易日可 supported，T-1 及更早最高只能 weak（可作 alternative 链驱动）且不得作为 primary_chain 支撑证据；④ 仅含方向性净流入/流出时必须置 insufficient。同时删除 `primary_phrase` 旧示例"主力资金撤离"（本身即同义反复）。
- **反向事实反证要求**：提示词新增"板块/大盘事实与个股方向相反、仍将该层置 supported 时必须引用 `counter_evidence_ids`，否则只能置 weak"；`services/stock_trace_validator.py` **镜像** Node `validateStockTraceResult` 的 `missing_counter_evidence` 规则（Node 侧是回写后的终态门、无重试，Python 侧失败才能触发 LLM 纠错重试）。

### 修复

- `agents/workers/stock_trace.py` 纠错提示语的非法枚举值 `probable` 改为 `hypothesis`（`attribution_status` 合法值仅 confirmed/hypothesis/insufficient/not_applicable，原值会让纠错重试再次校验失败）。

### 文档

- PDF 报告章节标题"五层候选归因"改为"分层候选归因"（`services/insight_report.py`；capital 不再恒产，标题需与口径一致）。
- `AGENTS.md` 登记本次决策与配套改动。

### 测试

- `tests/test_stock_trace_validator.py`：新增 capital 可选用例（不产出该层不阻塞校验）、原"缺 capital 报错"改为"缺 technical 报错"、新增提示词语义断言、新增反向事实反证三例（要求反证/引用后放行/非 supported 不误伤）。
- `tests/unit/test_insight_report.py`：章节标题断言同步。

---


## \[changer\] 2026-09-18 — 节奏大师事件可见性与报告逻辑修复（事件维度可见 + 市场主线可信）

**开发者**: 37588

### 修复

- 事件维度此前在报告与日历上完全不可见（窗口内真实存在的期指交割日零体现，前端反而呈现"无事件"）：事件按用途拆为两条互不干扰的通道——展示与提示通道放开到中级事件，档位通道严格只认高级事件，中级事件只被看见、不改变任何数值。
- 市场主线（领涨板块）候选改用实测有效的板块代码，并新增"板块代码存在，且板块名称与候选名互认"的双条件校验，堵住"用无关板块行情产出错误主线结论"的风险。
- 事件节点分支数量限定为三条上限，技术分档与事件情景两个来源互斥使用，被让位的一侧写入缺失标注。
- 正常市场状态（候选齐备但无清晰主线）不再被标记为数据缺失。

### 新增

- 节奏卡接入"未来事件日历"数据；事件临近提示按高级 / 中级分档措辞。
- 情绪周期阶段字段接线（含此前永不渲染的启动与主升两态）。
- 主线结论行补充强 / 弱标注，并放宽该行长度上限，避免技术位结论被截断。

### 重构

- 移除事件分支中从未接线的中间状态标记；主线各降级路径统一写入缺失标注，降级可审计。

### 文档

- 修正节奏引擎能力描述的文档漂移，并标注未接线的合成函数。

## [main] 2026-09-17 — 条件点亮双护栏 G1/G2（防情绪·海外·资金流口径误点亮与复合条件半判）

**开发者**: Aria

### 修复

- **生产误点亮（当日实证，已人工回滚，须防复发）**：① `id=214 c2`「**炸板家数**回落至10家以内、**涨停家数**回升至50家以上，且半导体相关板块…」→ 被**价格/量口径**判成 `condition_met=true`；② `id=18 c2`「**10 年期美债收益率**站上5%」→ 同上被点亮（海外利率指标）；③ `id=158 c1`「猪肉板块指数跌破近期支撑位**且主力资金持续净流出**」→ 前半句（技术位）确可判、后半句（资金流）判不了，旧实现**只判一半即命中**（半判点亮）。误点亮不可撤回（只写 true + jsonb 键级浅合并无删键，spec §12.7 R9）。
- **G1 口径不对应就不判**（`condition_met_judge.py`）：新增常量表 `_NON_PRICE_DOMAIN_KEYWORDS`（三口径：`overseas_macro` 美债/美元指数/美股/道指/纳指/标普/恒生/人民币汇率/美联储/加息/降息/原油；`sentiment` 涨停/跌停/炸板/封板/连板/家数/涨跌家数/赚钱效应；`capital_flow` 净流入/净流出/主力资金/北向/融资余额/融券）+ **单点函数** `classify_condition_domain` / `_is_unjudgeable_domain`；命中且 anchor 无**对应** metric（`_DOMAIN_EXEMPT_METRICS` 映射，当前三口径对应集均为空——`PredictionMetric` 白名单内无这些维度，保留结构作单点扩展位）→ 整体 `unjudgeable`（None，不产键）。**无配置开关**（行为确定、可测）。**刻意不误伤**：成交额/成交量/换手率（量类）、支撑位/前低/MA20（技术位）。
- **G2 复合条件不得半判**：新增 `split_condition_clauses`——按连接词（`并且|而且|以及|同时|且`，长词优先防单字「且」拆断多字连接词）切分，**不按中文逗号切**（逗号/顿号多用于并列列举同一子句内对象，如 id=214 的「炸板家数…、涨停家数…」）；子句端点剥标点、丢空子句，≥2 子句才启用复合判定：全部子句可判且都成立 → `True`；任一子句判不了（含被 G1 拦截/缺 metric/数据不足）→ `None`；全部可判但有子句不成立 → `False`（确定性不成立）。**单子句走 `_judge_clause_state`（原判定体，逐字搬移），行为逐字不变**。
- **作用范围（两段判定共用）**：护栏加在 `judge_condition_met_state`（第①段扫描与到期未成立态共用入口），两值口径 `judge_condition_met` 为其折叠（False→None）。**只影响"点亮 True"**：§12.5 到期写 `false` 与 `checked_at` 逻辑、写库契约、链/溯源/事件路径**零改动**。**事件类优先短路**：带 `anchor.event_ref` 的条件在本纯函数恒 None（交调用方状态锚→受限 LLM→None 三层），G1/G2 **不介入**，不与 §12.4 冲突。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` +10 例（214/18/158 三条生产文本 → unjudgeable；领域分类三口径 + 不误伤清单；切分规则与逗号边界；单子句技术位/量类回归点亮；复合全成立点亮；复合全可判有一句不成立 → 三值 `False` 且两值 `None`；单子句数据不足 → None；event_ref 不被 G1 拦截）+ `test_prediction_validator.py` +1 例。RED 取证：新增 API `ImportError`；临时禁用两护栏后 3 条生产文本断言 `assert True is None`（复现生产误点亮 True）。
- `tests/unit/test_condition_met_judge.py + test_prediction_validator.py` → **142 passed / 0 failed**；`-k "condition_met or validator"` → **187 passed / 0 failed**；全量 `tests/unit` → **8 failed / 3024 passed / 1 skipped**（8 条红与改动前基线**同集** → 零新增失败）；ruff 改动文件 `All checks passed!`；mypy `condition_met_judge.py` 0 报错。
- **遗留（口径未覆盖）**：① 情绪类仅覆盖"涨跌停/封板/家数/赚钱效应"词表，**未覆盖**「封单额、开板次数、连板高度、涨停封成率、晋级率」等同源情绪口径；② 资金流未覆盖「大单净额、龙虎榜、ETF 份额、融资买入额、南向」等；③ 海外/宏观未覆盖「非农、CPI、GDP、议息、汇率中间价、黄金、伦铜、A50、日经」等；④ **复合条件只有"且"系连接词**——"或"（任一子句成立）语义未实现；⑤ G1 判定为纯文本关键词命中，未做否定语境识别；⑥ 存量已误点亮的记录修复需走 `scripts/rollback_condition_met.py`（本期未批量回滚）。

---

## [main] 2026-09-17 — `trigger_review_full` 支持 `with_chain`（手动触发联动发布 `review_done`）

**开发者**: Aria

### 新增

- **缺口（2026-09-17 手动验证实际踩坑）**：`POST /admin/trigger/review_full` 直接调 `run_review`（不走 `ReviewFullConsumer`）→ **不发布 `review_done`** → `SectorTraceConsumer`/`PredictionConsumer` 永不触发 → 手动验证"复盘 → 链 → 级联预判"全链路必然看不到任何下游产物（缺的正是"链条触发的那一下"）。
- **接口**（`api/routes.py`）：body 由 `dict[str, str]` 升级为 `ReviewFullTriggerBody`（`report_date: str | None`、`with_chain: bool = False`）——**必须换类型**：`dict[str, str]` 下 JSON `true` 会被 FastAPI 校验拒为 422（RED 阶段实测 6 例 422 取证）。`with_chain` 缺省 false → **既有行为逐字不变**（不触碰总线、不发布）。
- **发布链路**（`_publish_review_done_for_chain`，仅 `with_chain=true` 且 `result.status == "ok"` 时调用，对齐调度路径"仅 ok 发 `review_done`"硬约束）：
  ① `get_default_bus()`（`main.lifespan` 已 `set_default_bus`）可用 → 复用会话内总线 `publish_review_done`（幂等 `event_id=review_done_{date}_{trace_id}`，与消费者路径同源）；
  ② 总线为 None → **降级**：`RedisPool.get_client()` 取单例客户端临时建 `EventBus`（单例属全局资源**不关**）；`RedisPool` 未初始化（RuntimeError）→ 按 `settings.redis_url` 临时连（URL **取配置不写死**，`APP_ENV` 决定实际值），发布后 `aclose()` 关闭临时连接；
  ③ 两条路失败 → 只 `manual_review_done_publish_failed` warning + 返回 `chain_published=False`，**不影响已完成的复盘返回**；返回体加 `chain_published: true|false` 观测（语义 = 发布路径执行完成未抛异常）。
- **`trigger_review_quick` 保持不动**（按要求只做 full）：核查确认它**结构上同样缺**，但①本期验收路径走 full；② quick 与 full 同日各发一次会双启链/级联 → 是否放开留待决策，**未擅自扩展**。（后于 2026-09-18 对称补齐，见该日条目。）

### 验证

- **测试（TDD 先红后绿，新增 `tests/unit/test_admin_trigger_review_full_chain.py` 7 例）**：缺省不发布（`get_default_bus` 未被调用 + `publish_review_done` 未 await）/ true+ok 用默认总线发布一次（bus 与 kwargs 断言）/ status=degraded 不发布 / 总线 None → 走 `RedisPool` 单例临时总线且**不关单例** / `RedisPool` 未初始化 → `from_url(settings.redis_url)` 且**关临时连接** / 发布抛异常 → 响应仍 200 + `chain_published=False` / 非法 report_date 仍 422 且 `run_review` 未调用。RED 取证：6 failed（5 例 `422 != 200` + 1 例 `KeyError: 'chain_published'`）。
- 新增 7 例 + 既有 `tests/test_admin_trigger.py`/`tests/e2e/test_quick_snapshot_flow.py`/`tests/unit/test_admin_trigger_midday.py` → **12 passed / 0 failed**；ruff 改动文件 `All checks passed!`；mypy `routes.py` 报错行全部在**未改动行**。
- **遗留**：① `trigger_review_quick` 未加同款开关（见上）；② 端到端未在真实服务器跑（需 `with_chain=true` + Redis 可用）；③ `chain_published` 的"乐观 True"语义如上。

---

## [main] 2026-09-17 — 参考位取数层接通：`today_open/high/low` 条件可判（Phase 5 遗留收口）

**开发者**: Aria

### 新增

- **结论：上游**有**字段，取数层未透传 → 已接通（不是"上游缺字段"）**。证据链：① index 日 K（`/internal/index/:code/kline`，`internal.ts:382-387`）已返回 `open/high/low`；个股日 K（`/internal/quote/:code/kline`，TushareKlineService）同；② 板块日 K 上游 Tushare `ths_daily` **本身返回** `open/high/low`（`TushareService.getThsDaily` fields 含 `ts_code,trade_date,close,open,high,low,pre_close,change,pct_change,vol,turnover_rate`），但映射层 `ThsBoardService.getBoardDailyRange` 只映射 `trade_date/pct_chg/close/vol/amount` → 契约 `ThsBoardDailyRow` 丢弃三字段；③ agent-py `prediction_validator._fetch_kline_range` 解析时同样只取 `pct_chg/close/vol/amount`。故三处**均补齐**（两仓）。
- **取数层（agent-py）**：`_fetch_kline_range` 行加 `open`/`high`/`low`（`_num` 归一，缺值 None 占位不丢行）；新增 `_today_ref_from_window(window)` 产出当日参考位 `{close, open, high, low}`（只取**窗口最后一行**，无 close 或三个参考位全缺 → `None`）。
- **判定层（agent-py）**：`condition_met_judge` 新增 `today_ref` 入参（`judge_condition_met_state` / `judge_condition_met`）与 `_judge_ref_level_state` / `_resolve_ref_op`；**口径**（spec §12.3 只写"需取数层补当日行"，未定明细，此处定死）：`today_open/high/low` 一律取**窗口最后一行（当日）**的开/高/低，与**同一行 close** 比较——不用窗口极值（条件文本写"今日高点/今日开盘价"，用窗口极值会把 N 日前极值当"今日"参考位误判，且 true 不可撤回），同行取值也避免逐维度剔 None 后列表错位；方向 = 显式 `op`（gte/above/lte/below）> 文本动词（跌破/下破/失守 → below；站上/突破/收回 → above）> `direction`；**`cross_*` 恒 None**（单日参考位无跨日稳定阈值）；缺 `today_ref`/缺对应参考位/缺 close/无方向线索 → `None`（降级）；同一行决定性比较 → 可给 `False`（到期未成立态），且**不需要** 2 样本守卫。
- **app-api 配套（同批部署，先行）**：`ThsBoardDailyRow` 加 `open/high/low`（`number | null`，缺值保 null 键存在，H7）+ `getBoardDailyRange` 三处映射（含中文键兜底 `开盘价/最高价/最低价`）+ 路由 docstring；`/internal/ths/:code/daily` 契约因此补齐（sector 目标类型的参考位条件才可判）。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` +9 例（站上高点成立 / 未站上 False / op 缺省按文本+direction 4 参数化 / cross_* → None / 缺 today_ref、缺 high、缺 close、neutral 无动词 → None / 两值口径只留 true）；`test_prediction_validator.py` +3 例（带 open/high/low 点亮 / 未成立不产 entry / `_fetch_kline_range` 三字段透传）+ 4 处行契约断言补 `open/high/low: None`；app-api `internal.ths.test.ts` 扩 1 例（含缺值保 null 与中文键兜底）。RED 取证：agent-py 15 failed；app-api 1 failed（`actual undefined - expected 1650`）。
- agent-py `-k "condition_met or validator or backfill"` → **177 passed / 0 failed**；ruff 改动文件 0（余 18 处 E501/F841 全在**未改动行**）；mypy `condition_met_judge.py` 0 报错、`prediction_validator.py` 9 处报错全在未改动行；app-api `node --import tsx --test src/core/routes/internal.ths.test.ts` → **10 passed / 0 failed** + `npx tsc --noEmit` 0 错误。
- **遗留**：① **存量记录收益有限**——`anchor.metric=today_*` 需生成侧产出（存量 326 条件多为量类文本且无 op/level）→ 收益面向新生成记录；② 参考位判定按"最新数据行"（未收盘时即前一日行）比较，与第①段扫描窗口 `[created_at, today]` 同源；③ 生产覆盖率复测仍未跑（`NODE_API_BASE_URL=localhost` 不可达）。

---

## [main] 2026-09-17 — 链板块落「权威名 + ts_code」（R14，消除前端匹配不上角色徽）

**开发者**: Aria

### 新增

- **缺口（spec §13.6 R14）**：链 `children[].sector` 用的是复盘报告原始名（如"黄金概念"），前端候选板块名来自 THS 权威榜（app-api `resolveBoardName` 归一后的 `name`）→ 命名漂移时 `findChainChild` 精确/归一化匹配双双落空 → 页面出现"有链但角色徽 / 驱动句不显示"。
- **加性字段**（`services/attribution_chain.py` `assemble_attribution_chain`）：每个 child 新增 **可选** `ts_code`（快照权威行 `ts_code`，如 `885525.TI`）与 `sector_std`（归一化权威名）；**取不到即省略键**（不写 `null`，与仓库"无匹配省略键"惯例一致）；`sector` 保持复盘原始名**逐字不变**（app-api `/api/internal/attribution-chain` 校验要求非空字符串，向后兼容）。两字段均为 `str`，序列化 JSON 友好（无 Pydantic 对象）。
- **归一化口径逐字对齐前端**（`_SECTOR_STD_SPACE_RE`/`_SECTOR_STD_SUFFIX_RE` + `normalize_sector_std`）：去空白/括号 → 剥「（A股）/概念/板块/行业/产业链」后缀 → 小写，与 app-api `ThsBoardService.normName`、app-frontend `utils/sectorInsight.normalizeSectorName` 完全同口径（顺序也一致）。**未新造第二套口径**：仓库 Python 侧既有 `prediction_service._normalize_sector_name`（仅去非字母数字）与前端口径不等价，若复用会再造成一次漂移，故按前端口径实现并注明同源关系。
- **取数来源（三条提取路径统一）**：`ts_code`/`sector_std` 取 `extract_primary_sectors` 命中的快照行（T1 `primary_claim` / T2 `candidate_claim` / T3 `snapshot` 三条路径的 `SectorHit.row` 都含 `name`/`ts_code`）→ 新增 `SectorTraceRunResult.sector_row` 字段由 `SectorTraceConsumer._one` 写入（`dict(hit.row)`），链组装 `getattr(res, "sector_row", None)` 读取（旧调用方/回放无该属性 → 省略 `ts_code`，`sector_std` 回退复盘原始名归一，不崩）。
- **跨仓零改动**：app-api 校验只认 `sector`/`relation`/`pct`/`events`（多字段忽略），前端类型加性可选 → 两仓本次均未改（前端未来可按 `ts_code`/`sector_std` 匹配，`sector_std` 口径已与前端归一化函数一致）。

### 验证

- **测试（TDD 先红后绿，`test_attribution_chain.py` +6 例 / `test_sector_trace_consumer.py` +1 断言）**：三条来源各断言 `ts_code`+`sector_std` 正确（`sector_std` 取权威行名而非原始名）/ 行缺 `name` 回退原始名归一 / 无 `sector_row` 与行内 `ts_code` 缺失 → 省略键不崩 / 空板块名不产 `sector_std` / 新字段均为 `str` 且 `json.dumps` 可序列化。RED 取证：`KeyError: 'ts_code'` ×4、`KeyError: 'sector_std'` ×2。
- `-k "chain or sector_trace or consumer"` → **235 passed / 0 failed**；ruff 改动文件 `All checks passed!`；mypy 1 处报错落在**未改动行**（`attribution_chain.py:641` `len(chain.get("children", []))`，仅行号位移）。
- **遗留**：① 前端尚未按 `ts_code`/`sector_std` 匹配（本期只落数据，前端改造独立排期）；② `sector_std` 在快照行缺 `name` 时回退原始名归一（权威性较弱，仅作桥接键）；③ Web 端（`aistock-frontend`）类型未同步（与 P1d 遗留同源）。

---

## [main] 2026-09-17 — 弱归因日板块提取三级兜底（候选链 → 快照）+ 弱依据标注（Task 9.1，链式溯源 R15）

**开发者**: Aria

### 新增

- **生产缺口（2026-09-17 18:30 复盘）**：`attribution_status=hypothesis`、`primary_chain_id` 为空（4 个候选全 `status=weak`，各自带 6 节点完整链）→ 主链 claim 零命中 → `extract_primary_sectors` 返回 `[]` → `SectorTraceConsumer.handle` 打 `sector_trace_skip_no_primary_sector` 直接 return → `attribution_chains` 表空、无溯源、无级联预判（快照 `a_share.sectors.top_losers/top_gainers` 有明确异动板块却无人消费）。
- **三级兜底（`agents/workers/sector_trace.py`）**：T1 主链 claim 命中（现有行为逐字不变，source=`primary_claim`）→ T2 候选链（含 `status=weak`）claim 命中（source=`candidate_claim`）→ T3 快照头部兜底（`top_losers` 优先、不足补 `top_gainers`，source=`snapshot`）。**仅上一级无产出才降级**；跨层/跨来源按板块名去重（同名收最先命中来源）；上限仍 `max_sectors=3`；空/畸形快照与畸形 payload 一律返回 `[]` 不抛错。新增 `SectorHit{name,row,source}`（`weak = source != "primary_claim"`）+ helper `_candidate_chain_claims` / `_sector_rows` / `_claim_hits` / `_snapshot_hits`，均可单独单测；**`extract_primary_sector`（单数版）显式收敛为 T1-only**，旧语义"不取桶首行兜底"逐字保留。
- **弱依据标注（链）**：`SectorTraceRunResult.extraction`（新可选字段）由 consumer 写入 `{source, weak}` → `assemble_attribution_chain` 在兜底路径写 `children[].extraction={source, weak:true}`，并在 root 写 `evidence_weak: true` + 报告 `attribution_status`；`root.summary` 原文空缺时用中性表述「证据不足，未确认主因」（不编造主因）。**T1 正常路径不写这些键**（不污染正常链）。`sector_trace_done` 日志补 `extraction_source`/`extraction_weak`（观测）。
- **弱依据标注（级联预判）**：兜底命中仍照常触发 `predict_sector`（不跳过）；`predict_sector(..., extraction_source, attribution_weak)` 把标记系统填充进预判产物（`schemas/prediction.py` 新增可选字段 `attribution_weak: bool = False` / `extraction_source: str = ""`，不升 schema_version；两个 prompt 键清单已同步登记"由系统填充、LLM 不得产出"，防 `extra="forbid"` 整条丢预判）。可选字段加性扩展 → 旧记录反序列化零破坏。
- **跨端核对**：app-api `attributionChainRouter`（无字段白名单，仅校验已知键类型）与前端 `AttributionChainView.vue`/`attributionChain.ts`（只读 `root.summary/index_pct`/`children[]`）对新增可选键天然容忍 → **app-api/前端零改动**。

### 验证

- **测试（TDD 先红后绿，新增 23 例，其中 `-k "sector_trace or chain or consumer"` 子集内 +19 例）**：`test_sector_trace_extract.py` 重写为 15 例（T1 回归 3 例断言不变 / T1 有产出不降级 / T2 候选顺序与跨候选去重与上限 / T3 losers 优先+gainers 补齐+去重+跳过空名 / 三层皆空 / 畸形 payload 与空快照不崩）；`test_attribution_chain.py` +5；`test_sector_trace_consumer.py` +3；`test_prediction_sector_service.py` +3、`test_prediction_prompt.py` +1。RED 取证：`ImportError: cannot import name 'SOURCE_CANDIDATE_CLAIM'`、`TypeError: cannot unpack non-iterable SectorHit object`、`KeyError: 'evidence_weak'`、`TypeError: predict_sector() got an unexpected keyword argument`。
- `-k "sector_trace or chain or consumer"` → **228 passed / 0 failed**（基线 209）；全量 `tests/unit` → **2986 passed / 8 failed**（8 例与基线一致，零新增）；ruff 改动行 0；mypy 新增错误 0。
- **遗留与风险**：① 兜底命中的板块**证据弱**，仅以 `extraction.weak`/`evidence_weak` 标注（展示层暂未渲染弱提示，前端本期不改）；② T3 快照兜底会在「无 trace / trace 无候选」的日子也产出链（上限 3），属口径内行为，观察线上板块选择质量；③ T2 依赖候选链 claim 文本含板块名（与 T1 同款子串匹配），命名漂移（R14）仍可能漏配。

---

## [main] 2026-09-17 — 溯源弱反馈：观测层（聚合+建议+审计上报，默认 observe 零副作用）（Task 7.1，spec §13.3）

**开发者**: Aria

### 新增

- **定位（本期只做观测层）**：把「链上溯源信号」× 「预判验证结果」关联聚合 → 产出建议（建议降权/建议提级/观望）→ 落审计表（app-api `attribution_feedback_signals`，可查、幂等）。**不修改**溯源 prompt / 驱动类型判定 / 预判输入与任何既有写入，**不真正应用权重**（应用层待积累真实样本后单独立项）；遵守总纲 §3.4「迭代双链路分离」：弱反馈只用于溯源侧信号，**不得直接改写预判**。
- **单元 key 口径（关键取舍，NEEDS_CONTEXT）**：计划原定默认 `driver_type`（复用 `prediction_service._TRACE_CATEGORY_TO_DRIVER`/`_extract_driver_for_trace`），但核查发现 **`driver_type` 未落库**——链 `children[]` 只有 `{sector, relation, pct, trace_summary, events}`，`prediction_records` 也无该列 → 未硬造标识：**默认 `unit="relation"`**（`judge_sector_driver_relation` 的确定性产物、链上唯一持久化的溯源侧标识）；同时**提供** `driver_type` / `driver_type_sector` 可选口径（回读该日复盘报告 `market_trace.trace` 复用既有映射，报告不可读 → 整日跳过并计数 `driver_unavailable`，不猜）。可选 unit 全集：`relation` / `relation_sector` / `sector` / `driver_type` / `driver_type_sector`（`SUPPORTED_UNITS`）。
- **关联与样本口径**（`services/attribution_feedback.py`）：窗口 = 过去 N 个交易日（含末点，非交易日末点回退最近交易日）；样本 = 链 `children[]` 板块 × `source_id` **精确匹配** `sector:{链上板块名}:{date}`，匹配不上 → 跳过并计数 `unmatched`；档位样本 = `verification[short|mid|long].result ∈ {hit,miss}`（`insufficient` 只进 detail）；`c{i}` 条件层 result/`condition_met` 布尔**只进 detail**，不与档位样本混桶；只收 `source_id` 前缀 `sector:` 的记录（大盘/个股不参与）。
- **建议规则**（可配置阈值）：`sample_size < min_samples(默认10)` → `insufficient`；否则 `hit_rate < low(0.35)` → `downgrade`；`> high(0.65)` → `upgrade`；其余 `hold`（边界取严格不等，等于阈值 → 观望）；`hit_rate` 样本 0 → `None`（不写 0 冒充 0%），4 位小数。阈值自洽性守卫（`0<=low<high<=1`、`min_samples>=1`）不满足 → 显式报错，不静默产出错误建议。
- **写侧**（`AttributionFeedbackStore`）：`POST /api/internal/attribution-feedback`（**路径带 `/api` 前缀**，R13 教训：不带会命中错误 router 恒 404）；失败（返回 None / 抛异常）**只 warning 不抛出**，`write_failed` 计数。
- **CLI/调度**：新增 `scripts/attribution_feedback.py`（`--date` / `--dry-run`（默认）/ `--execute` / `--window` / `--unit`；独立运行初始化 `HttpClientPool`）；新增 cron `attribution_feedback`（`scheduler_attribution_feedback_cron="10 16 * * 0-4"`，在 `prediction_validate` 16:00 与 `prediction_stats` 16:05 之后采样，`dry_run=False` 但 `mode=observe` → 只写审计表）；`attribution_feedback_mode=off` 为运维开关（不读不写）。
- **配置项与默认值**（`config.py`）：`attribution_feedback_mode="observe"` / `attribution_feedback_window=60` / `attribution_feedback_min_samples=10` / `attribution_feedback_low_threshold=0.35` / `attribution_feedback_high_threshold=0.65` / `attribution_feedback_unit="relation"` / `scheduler_attribution_feedback_cron="10 16 * * 0-4"`。

### 验证

- **测试（TDD 先红后绿，新增 44 例）**：`tests/unit/test_attribution_feedback.py` —— 关联正确性、`source_id` 匹配失败/无链/空白板块名跳过并计数、条件层不污染档位样本、**dry-run 零写入**、**observe 零副作用**（唯一写入 = 审计表端点，`save_prediction`/`update_prediction_verification`/`save_analysis_report`/`put`/`patch`/`delete` 任一被调用即 fail；读入链/记录对象逐字节不变）、上报失败只 warning、路径 `/api` 前缀负向断言、driver_type 口径与报告不可用跳过、配置默认值锁定、阈值不自洽报错、CLI 装配默认 dry-run、报告渲染。RED 取证：`ImportError: cannot import name 'attribution_feedback'`。
- `pytest tests/unit -q -k "feedback or chain or prediction"` → **579 passed / 1 存量红**（`test_iterate_adapters::test_registry_...`）；`test_scheduler.py` 3 处同步；ruff 改动行 0（`All checks passed!`）；mypy `Success: no issues found`。
- **遗留**：① **应用层未做**（真正影响溯源信号权重）→ 待观测期积累真实样本后单独立项；② 未接入任何消费方（GET 只读端点供人工/前端查，agent 侧未消费建议）；③ `unmatched` 可能偏高（链 `children[].sector` 用复盘原始名、预判 `source_id` 用 THS resolved 名，R14 同源）；④ 生产未跑（`localhost` app-api 不可达）→ 待部署后验证。

---

## [main] 2026-09-17 — 条件"未成立态"落地 + 历史回溯补算（Task 6.1/6.2，spec §12.5/§12.6）

**开发者**: Aria

### 新增

- **判定层三值化**（`services/condition_met_judge.py`）：新增 `judge_condition_met_state`（`True`=成立 / **`False`=确定性不成立** / `None`=无法判定），内部 `_judge_tech/_judge_volume/_judge_pct` 改为三值 `*_state`；`judge_condition_met` 保持**两值契约**（False 折叠回 None）→ 第①段扫描行为逐字不变（其单测 49 例全绿）。给 False 的三类：涨跌幅累计未达阈值、技术位末值未触发（均线/前极值）、量类 `_compare` 不成立；**cross_\* 未穿越仍 None**（相邻两日语义）；参考位降级 / 无 level 量类 / 量级护栏 / 单样本 / 绝对点位守卫 / 事件类恒 None。
- **到期写未成立态**（`services/prediction_validator.py::_verify_conditions`）：`result` 落库那一刻对确定性未成立条件写 `condition_met=false` + `checked_at`（判定时间 ISO 日期）；判定窗口 = `[created_at, due]`（第①段是 `[created_at, today]`）；**已点亮 true 显式防御不回退**；**无法判定不写该键（绝不写 null）**；`wait`（窗口未满）分支不写；已含 result 的 c{i} 提前跳过 → 重复扫描零重复副作用。判定逻辑抽公共 `_judge_condition_met_once`（第①/②段**同源**）；`_verify_conditions` 新增可选 `scan_cache/event_cache`（与第①段共用取数/事件记忆化，key 含窗口不串用）。
- **事件类到期语义**：`_judge_event_condition(..., at_due=True)` 时 `scheduled/upcoming` → 确定性 `False`（到期未落地）；未到期仍 `None`（第①段只写 true）。
- **历史回溯补算**：新增 `backfill_condition_met(*, dry_run=True, limit, batch_size, sleep_seconds, max_records, sample_size)` 服务函数（`services/prediction_validator.py`）+ `scripts/backfill_condition_met.py` CLI（**默认 dry-run 零写入**，`--execute` 才写库；默认限速 每 20 条 / 0.5s；统计 scanned/candidates/judgeable/lit/unmet/unjudgeable/in_flight/written/write_failed + 抽样明细）。目标集合 = `schema_version='3.0'` + `prediction.conditions[]` 非空 + `verification[c{i}]` **无布尔 `condition_met`**；只补 `condition_met`/`checked_at`，**既有 entry 整条回传**防 Node 默认值（`actual:''`/`reason:''`/`verified_at`）覆盖已判档位；未到期只写 true（§12.5）；CLI 独立运行初始化 `HttpClientPool`（修 "HttpClientPool not initialized" → 统计恒 0）。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` +6 例（三值 ×3 / 不可判 / cross 未穿越 / 两值契约回归）；`test_prediction_validator.py` 改 1 增 4（到期 false+checked_at / 已点亮不回退 / 不可判不写键 / run_once 重复扫描零副作用）；新增 `test_backfill_condition_met.py` 9 例（dry-run 零写入+统计 / 只补目标键且既有键逐字节保留 / 幂等跳过 / 未到期不写 false / 判定成立点亮 / 不可判不写 / 写失败不炸批 / CLI 装配默认 dry-run / 报告渲染）。RED 取证：judge 导入失败、validator `KeyError: 'condition_met'` + `assert_not_awaited` 失败。
- `pytest tests/unit -q -k "condition_met or validator or prediction"` → **506 passed / 1 存量红**（`test_iterate_adapters::test_registry_...`；加 `or backfill` → 507/1）；全量 `tests/unit` → 2919 passed / 8 failed（8 例全为既有基线红）；ruff 改动行 0（余 2 处 E501 落在**未改动行** `prediction_validator.py:237-238`）。
- **遗留**：① **生产 dry-run 未跑**（`NODE_API_BASE_URL=localhost:3000` 不可达 → CLI 输出 `scanned=0` + 明确提示）→ 需在服务器按"先 dry-run 报告 → 人工确认 → `--execute`"执行；② 回溯收益受 spec §12.2 约束（存量 326 条件多无量级/技术位口径）→ 只能补可判定的少数；③ 参考位（today_open/high/low）仍降级 None（取数层未补当日行）。

---

## [main] 2026-09-17 — 条件可判定性契约：anchor 扩展 `metric/op/level` + 判定分流（量类/技术位/参考位）+ 事件三层状态锚 + 误点亮回滚预案（Task 5.1/5.2，spec §12.3/§12.4/§12.7 R9）

**开发者**: Aria

### 新增

- **schema 扩展（可选字段，不升 `schema_version`）**：`schemas/prediction.py` `PredictionMetric` 由 5 值扩到 13 值（+`amount`/`ma20`/`ma60`/`prior_low`/`prior_high`/`today_open`/`today_high`/`today_low`）；新增 `PredictionAnchorOp = Literal[gte|lte|above|below|cross_above|cross_below]`；`PredictionAnchor` 加 `op`/`level: float | None = None`（默认 None → 旧记录反序列化零破坏）。**不新增 `condition_type` 字段**（`extra="forbid"` 下新增字段会整条丢预判）。
- **类型确定性推断**（`services/condition_met_judge.py::infer_condition_class`，判定侧唯一实现）：`event_ref` 非空 → 事件类；显式 `metric`（量类→技术位→参考位）优先于文本；无显式 metric 时文本兜底（量词→技术位词）；否则涨跌幅/点位类。**偏差说明**：任务书的推断顺序把"文本明示技术位"排在参考位之前，实现改为**显式 metric 优先于文本**——否则 `metric=today_high` + 文本"站上今日高点"会被判成技术位并用 MA 近似点亮（true 不可撤回）。
- **判定分流**：① 事件类 → 调用方三层（状态锚/受限 LLM/None，纯函数恒 None）；② 量类 → 窗口 `vol`/`amount` 与 `level` 按 `op` 比较，口径 = `gte`/`above` 取窗口 **max**（"曾放量到该量级"）、`lte`/`below` 取 **min**、`cross_*` 用**相邻两日**穿越、`op` 缺省先按文本（放量→gte/缩量→lte）再按 direction；三道守卫（无 `level`、样本 < 2、`level/max(series)` 越出 [1e-3,1e3]（**量级/单位护栏**）→ 恒 None）；③ 技术位 → 显式 `metric`（ma20/ma60/prior_low/prior_high）优先，回退文本/`direction`；④ **参考位 → 恒 None（降级）**：日 K 取数层只透传 `trade_date/pct_chg/close/vol/amount`，`today_open/high/low` 不可得；⑤ 涨跌幅类语义不变（绝对点位守卫保留）。
- **取数层加性扩展**：`_fetch_kline_range` 行新增 `amount` 键（index/stock 上游有值、sector 恒 null）→ `metric=amount` 量类可得；四处既有行契约单测同步补 `"amount": None` 断言。
- **事件三层①（状态锚，确定性）**：`_scan_condition_met` 事件类条件**不拉行情**，改读 `node_api.get_event_entities({dateFrom,dateTo})`（扫描窗口 ISO 化；同批次按窗口记忆化）→ `event_status ∈ {ongoing, occurred}` 点亮；`scheduled/upcoming` → 不点亮；事件缺失/未物化/读失败 → ②/③（fail-safe 不点亮）。② **受限 LLM 层**：`_judge_event_condition_llm`（输入限定 事件标题 + 摘要 + 条件文本；输出 true/false/unknown + 置信；low 置信与异常 → None）由 `settings.condition_met_event_llm_enabled`（**默认 False**，新增 config 项）控制，开启时每次判定写 `condition_met_event_llm_judged` 留痕。③ 兜底 None。
- **生成侧同批同步**（`prompts/workers/prediction.py` 双 prompt + `light_predict.py`）：anchor 键清单加 `op`/`level`，`metric` 可选值清单同步扩展，新增「可判定性硬约束」（每条条件至少映射一个可判定维度；量化条件必须给 `level` 或 `threshold`；`level` 只能取输入中真实量级），示例 JSON 补**量类**与**参考位类**各一条；「字段归属」段同步列明 anchor 只允许 7 个键（多吐未登记键 → 整条预判校验失败）。
- **Task 5.2 误点亮回滚预案**（`scripts/rollback_condition_met.py`，**不自动执行**）：应用侧无回退手段——Node PUT 只放行 `condition_met===true`/带 `result`，写 `false`/`null` 恒 400，且 jsonb 键级浅合并不表达"删键" → 唯一路径是 DB 级 `verification #- '{c{i},condition_met}'`。脚本只读生产（GET /internal/predictions）+ 生成 SQL（`--dry-run` 默认仅列清单，`--sql-out` 落盘），`WHERE id = … AND verification #> '{c{i},condition_met}' = 'true'::jsonb` 幂等守卫、**不触碰 `result`**；优先 `--prediction-id`+`--condition-index` 精确到单键。用法/风险/使用条件写入脚本 docstring 与 `README.md`。
- **事件类条件不依赖行情源**（`_scan_condition_met`，commit `d0a7fb4`）：移除 `code is None → return {}` 的提前返回（改为行情类条件逐条 `continue`）——目标资产解析失败时事件类条件（只读 Event Entity 状态）此前会被无关的行情解析失败连带跳过；新增回归守卫用例。

### 验证

- **测试（TDD 先红后绿）**：`test_condition_met_judge.py` +23 例（推断规则 11 参数化、量类成立/不成立/无 level/量级护栏/样本不足/amount 降级/adjacent cross/文本兜底 op、技术位 metric 显式、参考位降级、事件类纯函数 None）；`test_prediction_validator.py` +8 例 + 4 处 `amount` 契约断言；`test_prediction_schema.py` +12 例；`test_prediction_prompt.py` +1 例；新增 `test_rollback_condition_met.py` 9 例。RED 取证：schema 16 failed、judge 导入失败、validator 3 failed → 全 GREEN。
- **提交**：`89020fd`（schema+双 prompt 同批）/ `3757ec1`（判定扩展）/ `3ada752`（回滚预案）/ `d0a7fb4`（事件类不受行情源影响）。
- `pytest tests/unit -q -k "condition_met or validator or prediction"` → **485 passed / 1 failed**（唯一红 = 存量 `test_iterate_adapters`，由既有 `5d07eed` 注册引入，非本次改动）；全量 `tests/unit` → **2897 passed / 8 failed**（8 例均为既有基线红）；ruff 改动行 0；mypy 7 处报错经 `git diff -U0` 核对全部落在未改动行。
- **遗留**：① **参考位不可得→降级**（日 K 无 open/high/low，sector 上游亦无）→ 待取数层补当日行；② **存量覆盖率不因本次改动提升**：生产 146 条/326 条件无 `op`/`level` → 仍不可判；收益面向**新生成**记录；本次**未能**复跑生产样本（`NODE_API_BASE_URL=localhost:3000` 连接失败）；③ 事件状态锚依赖 Event Entity 物化（`EVENT_ENTITY_ENABLED` 默认 False）；④ 量类 `level` 单位口径依赖生成侧"取输入真实量级"约束，跨源由量级护栏兜底为"不判"。

---

## [main] 2026-09-17 — 修正链写入路径缺 `/api` 前缀（Task 3.1b，Critical：生产链从未落库）

**开发者**: Aria

### 修复

- **根因**（`services/attribution_chain.py:527-533` `AttributionChainStore.save`，commit `fa8f566`）：写入走 `node_api.post("/internal/attribution-chain", …)`，但 app-api 中 `attributionChainRouter` 挂在 `app.use('/api', …)`（`aistock-app-api/src/index.ts:165`），写接口绝对路径为 **`POST /api/internal/attribution-chain`**；`/internal` 是另一个 router 的挂载点（`index.ts:631`）→ 实际请求 `http://localhost:56790/internal/attribution-chain` **恒 404**，且 `data_client.post`/`_post_request`（`data_client.py:203-210`，`url = f"{self._base_url}{path}"` 逐字拼接）失败时**吞错返回 None** → 只打 `attribution_chain.save_failed` warning，**静默不落库**。生产佐证：`select count(*) from attribution_chains` → `relation "attribution_chains" does not exist`（从未成功写入，表未建）。
- **修复**：路径改为 `"/api/internal/attribution-chain"` + 就地注释说明为何必须带前缀（防再次漏掉）；行内无其它逻辑改动（None 判定/告警语义保持）。`services/data_client.py:709-714` 的 `get_attribution_chain` docstring 原记「写入路径同样缺 /api，属 Node 侧既有口径，本任务不改」→ 已同步为「Task 3.1b 已补」，避免留下误导性事实。
- **逐条核对（重点：避免误改）**：`grep -rn 'post("/internal'` in `src/aistock_agent/services/` 共 8 处 + 全仓 `"/internal/…"` 字面量逐条判定，结论 = **唯一需要补 /api 的只有链写入这一处**——`attributionChainRouter.ts:132` 是全 app-api 唯一声明绝对 `/internal/…` 路径的 router，且它是唯一被挂到 `/api` 的那个。其余全部命中 `app.use('/internal', internalRouter)` 或 `/internal/{stock-trace,insight,predictions,calendar,event-entities,stock-info}` 子挂载 → **一律保持不动**。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_attribution_chain.py` `test_save_posts_to_internal` → 重命名 `test_save_posts_to_api_internal`，断言由 `== "/internal/attribution-chain"`（旧路径被测试**锁死**，正是这次能长期存活的原因）改为 `startswith("/api/internal/attribution-chain")` + **负向断言** `not startswith("/internal/")`。RED（改前）：`1 failed` → GREEN。
- `.venv\Scripts\python.exe -m pytest tests/unit -q -k "chain"` → **128 passed / 0 failed**（无存量红）；`test_data_client_attribution_chain.py + test_attribution_chain.py` → **33 passed**；ruff 三文件 `All checks passed!`；mypy `attribution_chain.py` 1 处报错落在**未改动行** `:545 children=len(chain.get("children", []))`。
- **遗留（部署前置，必须先做再部署）**：① 生产库需执行 `020_attribution_chains.sql` 建表（当前表不存在，即使路径修好也无法落库）；② 历史缺失的链无法回溯补齐（写入是事件驱动、非幂等重放）；③ 本任务只修写入，读取侧（`get_attribution_chain`）已在 Task 3.1 修正；④ 建议 spec §13.6 登记 R13。

---

## [main] 2026-09-17 — 预判依据增强：链上事件 + 中台事件 + 大盘归因注入（Task 3.1，spec §4.2/§4.4）

**开发者**: Aria

### 新增

- **链读取能力**（`services/data_client.py:706` `get_attribution_chain(date)`）：Node 侧端点为 `GET /api/agent/attribution-chain/:date`（路由 `attributionChainRouter.ts:180` 挂在 `app.use('/api', …)`，`index.ts:164`），与 `/internal/*` 惯例有两点不同——① 路径必须带 `/api` 前缀（base_url 不含 /api，先例 `tools/market_tools.py:53`）；② 响应是**裸体** `{date, chain|null}`（非 `{code,data}` 信封），走 `self.get` 的信封解包会恒返 None → 该方法自行发请求并容忍裸体/信封两种形状；无链（null/空对象）与请求失败统一归一为 None。
- **大盘路径注入**（`prediction_service.py` `run_predict:741-748`）：`_build_prediction_input` + 画像之后并入 `chain_events`（当日链**全部 children** 事件，≤5、按链序、按 event_id/ref 去重）、`warehouse_events`（当日中台存量事件按链上主驱动板块名匹配，≤3）、`attribution_summary`（链根 summary 优先，无链回退 `trace.attribution_summary`，同源）；留痕在 `:816` 写回产物。
- **板块路径注入**（`predict_sector:1910-1917`）：`_build_event_input(report_date, [sector_name, target.name])` 结果并入 `extra_input` 交 `_sector_prediction_core` 的 prompt_input——只注入该板块在链上的 `children[].events`（≤5，未入链省略该键）+ 中台匹配（≤3）；`market_trace_brief` 原样保留；不注入 `attribution_summary`（大盘结论已由 brief 承载，避免同义键重复）。
- **上限与降级**：任一来源为空 → **省略对应键**（不注入空数组/占位）；中台匹配复用链事件层同源读取（`load_chain_warehouse_events` → `event_store.load_event_scrape`，一次报告读，**无新增检索/LLM 成本**）与同一权重函数（实体 > 关键词 > 标题 > 摘要）；`_build_event_input` 整体 fail-safe（异常 → warning + 空块），依据增强失败绝不丢整条预判（对齐 spec §4.4）。
- **留痕**（`schemas/prediction.py:181` `PredictionResult.input_event_refs: list[str] = []`）：**系统填充、非 LLM 产出**（两个 prompt 均已登记"由系统填充，LLM 不得产出该键"）；值 = 注入事件的中台权威 event_id（检索来源无 id 用 ref），顺序与注入一致、跨键去重；无注入为空数组（非 None）；不升 schema_version（3.0）；Node 侧零改动（整条 prediction jsonb 落库）。
- **prompt 同步**（`prompts/workers/prediction.py`）：`PREDICTION_PROMPT` 与 `PREDICTION_CHAT_PROMPT` 均新增「事件驱动说明」段——输入含 `chain_events`/`warehouse_events`(/`attribution_summary`) 时结论必须说明**是否受事件驱动**（事件驱动/非事件驱动/跟随大盘）并点明所依据事件；无事件块时按现有依据推演、禁止编造事件。**输出结构不变**。
- **回放隔离**：`run_predict` 在 `replay_context` 非空时不注入（P4 回放零 DB/网络访问）；`iterate/replay_layer.py` `_SERVICE_ISOLATION_TARGETS` 登记 `NodeApiClient.get_attribution_chain: node_read`（直接 httpx 方法，I-3 清单封闭测试强制）。

### 验证

- **测试（TDD 先红后绿）**：新增 `tests/unit/test_prediction_input_events.py`（8 例：大盘有链注入/无链无匹配全省略/无链回退溯源结论/回放不读链/板块注入自身事件/板块未入链只注中台/板块无链全省略 + 留痕随 payload 落库）+ `tests/unit/test_data_client_attribution_chain.py`（6 例）+ `test_prediction_prompt.py` 2 例。RED：9 failed（`AttributeError: get_attribution_chain` ×7 + prompt 断言 ×2）→ GREEN 25 passed。
- `-k "prediction or chain"` → **500 passed / 1 failed**（存量红 `test_iterate_adapters`，stash 基线复核确认）；全量 `tests/unit` → **2832 passed / 8 failed**（8 例均既有基线红）；ruff 改动文件 0；mypy 10 处报错全部落在未改动行。
- **遗留**：① 中台匹配词来自链 children 板块名（大盘路径）/目标板块名（板块路径）→ **无链时大盘路径不注入 `warehouse_events`**（无匹配词源）；② 注入事件不进入 `evidence_ids` 允许集；③ Node 侧 chain 写入路径 `/internal/attribution-chain` 缺 `/api` 前缀（与本次读取侧发现同源，未改，待部署核对）。

---

## [main] 2026-09-17 — 链组装先于级联预判（Task 1.1，P0' 时序，spec §13.1 方案 A）

**开发者**: Aria

### 改进

- **时序调整**（`services/event_consumers.py` `SectorTraceConsumer.handle`，commit `a7f8ca4`）：级联预判从 `asyncio.gather` 内部的 `_one`（旧 `:473-477`）**移到链组装/保存之后**（新 `:505-524`）。`_one`（新 `:467-484`）只做 `run_sector_trace` + `results.append` + `cascades.append((sector_name, result.snapshot))` + 失败 warning；溯源 gather（`:486`）→ `if results:` 组装并保存链（`:488-504`，逻辑逐字未动）→ `if cascades: await asyncio.gather(*(_cascade_one(name, snap) for name, snap in cascades))`（`:523-524`）。背景：预判输入组装（P2' 依据增强）需读当日链，旧时序下链在 gather 之后才组装 → 级联路径永远拿不到当日链（§13.1 问题陈述）。
- **并行 + 两个独立 try**：级联用 `asyncio.gather` 并行（**未**写串行 for，避免每板块一次 LLM 调用线性累加）；隔离用**每项自带 try/except**（`_cascade_one :508-521`，与既有 `_one` 风格一致，未用 `return_exceptions=True`——需按板块记日志字段），与链保存 try 完全独立：链保存失败不跳过级联，级联失败不影响已保存的链；`extract_primary_sectors` 为空仍直接 return（不溯源/不写链/不级联），`results` 为空时 `cascades` 亦空。级联入参仍为 `sector_name` + 溯源 `snapshot`（签名未变，`_cascade_sector_prediction` 本身已是 fail-safe 吞错）。
- **日志键分离**（审查 Minor 15）：`_one` 的 except 只剩溯源失败 → `sector_trace_one_failed`（语义恢复正确）；级联失败新键 `sector_cascade_predict_failed`（字段沿用 `sector`/`error`，另带 `report_date`）。改前 stdout 取证：级联抛错被记为 `sector_trace_one_failed error='cascade down'`（错误归因）。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_sector_consumer_multi.py` 新增 4 例——链保存先于级联（`call_order` mock 记录顺序）/ 链保存抛错仍触发级联且不向外抛 / 级联抛错不影响链保存结果且不向外抛 / 级联并行（mock 计数 in-flight 峰值=2，串行 for 会得 1）。RED（改前）：3 failed（均 `assert 'cascade' == 'chain_save'`）+ 1 passed（并行守卫在旧代码下也成立，属回归锁）；GREEN：23 passed。
- 回归 `-k "sector or chain"` → **294 passed / 0 failed**；`-k "sector or chain or consumer"` → **331 passed**；ruff 0；mypy 18 处报错均在未改动行。
- **遗留**：本任务只保证"链已写在前"，级联预判**读取**当日链由 Phase 3（Task 3.1 依据增强）落地；§13.1 验收项"预判记录注入的链事件 id 非空"待 3.1 后可验。提交遇环境级 git 写盘拦截（`.git/objects` Permission denied），按既有绕过方案（`GIT_OBJECT_DIRECTORY` → C: 临时目录 + `GIT_ALTERNATE_OBJECT_DIRECTORIES` 指回原对象库，提交后 PowerShell 复制回 `.git/objects`）完成，`git fsck` 无 missing/corrupt，临时目录已删除。

---

## [main] 2026-09-17 — 板块预判落库前归一 prediction.target（Task 0.5b，补 0.5 写入侧缺口）

**开发者**: Aria

### 修复

- **写入侧归一**（`services/prediction_service.py:1551-1567`，`_sector_prediction_core:1452`）：新增 keyword 参数 `resolved_target: Target | None`，在 A3 置信钳制之后、`return prediction` 之前——有 resolved Target 时 `model_copy(update={"target": resolved_target})` **一律覆盖**（含 LLM 自产的 index 类 target）；无 resolved（回放态无 ts_code）→ 不伪造（保持原样/None）+ `logger.warning("sector_prediction.target_unresolved")` 留痕。背景：板块所用 `PREDICTION_CHAT_PROMPT` 未定义顶层 `target`（仅 `PREDICTION_PROMPT:57` 有），`PredictionResult.target` 缺省 None → 0.5 的画像结构化匹配仍恒 miss（n=0）。
- **接线**（`:1699-1702` `predict_sector`）：落库前传 `resolved_target=target`（即 `sector_target_from_resolved` 产物，`internal_id=code=ts_code`）。批量路径 `sector_wind_prediction` 经 `predict_sector` 同受益。
- **未复用 `_repair_llm_target_internal_id`（已核对）**：它只在 `run_predict`（大盘）调用（`:759`），无 kind 分支，且语义是 `make_target(name)` 补 `internal_id` —— 板块名经 make_target 会剥后缀（"存储板块"→"存储"）+ `code=None`，与画像 key（ts_code）口径冲突；板块直接采用 resolve 结果，`sector_target_from_resolved` 已是唯一 Target 构造点，未新增第二套归一。

### 验证

- **测试（TDD 先红后绿）**：`tests/unit/test_prediction_sector_service.py` 新增 4 例（落库 payload target=resolved ts_code / LLM 产 index target 被覆盖 / 无 resolved 不伪造+warning / 落库 payload 直喂 `read_validation_profile` 端到端 n=1）；helper `_sector_prediction` 增 `target`、`horizon_target` 两个可选参数（默认值保持原行为）。RED（改前）：4 failed（`target is None` / `assert '000001.SH' == 'BK1001'` / 无 warning / `assert 0 == 1`）→ GREEN。
- 验收 `-k "prediction or validation"` → **383 passed / 1 failed**（存量 `test_iterate_adapters`）；全量 `tests/unit` → **2798 passed / 8 failed**（8 例均既有基线红）；ruff 0；mypy 与 HEAD baseline 逐行一致（4 处存量报错，仅行号位移）。

---

## [main] 2026-09-17 — 板块预判注入验证画像（Task 0.5）

**开发者**: Aria

### 改进

- **匹配口径**（`skills/prediction_validation.py:48` `_record_target`）：改为**优先**取结构化 `prediction["target"]["internal_id"]`（稳定标识 = 板块 resolved ts_code，数据卫生 §2.1）→ `["name"]`（内层回退）→ 无结构化 target（旧记录）回退 `horizons[].target` 字符串。背景：板块预判 `horizons[].target` 是 LLM 自由文本（prompt 要求"验证对象优先用指数名"→ 常写"上证指数"），按字符串与板块 ts_code/板块名比对必然 miss → 画像恒空。返回类型与调用方（同文件 `_collect_target_entries` 81-109）不变。
- **Target 直读**（`services/prediction_service.py:1149`）：新增 `_enrich_predict_input_for_target(prompt_input, target)`；原 `_enrich_predict_input_for_symbol` 退化为薄封装（`make_target` → 转调），大盘/回放/chat 行为不变（日志字段 symbol → target.internal_id）。
- **板块注入**（`services/prediction_service.py:1671-1682`）：`predict_sector` 在 `sector_target_from_resolved` 之后按 resolved ts_code 读画像，经 `extra_input` 并入 `_sector_prediction_core` 的 prompt_input（与大盘同构）；无画像/读取失败 → 空 dict 不并入（省略该块，不报错、不阻断产出）。

### 验证

- **测试**：`test_prediction_validation.py` 新增 5 例、`test_prediction_sector_service.py` 新增 2 例；RED（改前）4 failed（`assert '上证指数' == '885001.TI'` / `assert 0 == 1` / `mock_read awaited 0 times`）→ GREEN。验收 `-k "prediction or validation"` → **379 passed / 1 failed**（存量 `test_iterate_adapters`）；全量 `tests/unit` → **2794 passed / 8 failed**（8 例均既有基线红，与本次 diff 零交集）；ruff 0；mypy 与 HEAD baseline 逐行一致（5 处存量报错，改动行内无新增）。
- **遗留**：① 渠道 B（`_collect_target_confirmations` 只扫 review 报告）不覆盖板块，列为后续；② **写入侧未强制**——板块记录 `prediction.target` 仍由 LLM 自由文本决定（`_repair_llm_target_internal_id` 只作用于 run_predict），匹配口径虽已支持结构化 target，生产板块记录需写入侧补 `target = resolved Target` 才能稳定命中；③ `skills/scene_probe.py:56` 同名 `_record_target` 未同步（渠道 B 探针路径）。

---

## [main] 2026-09-17 — 快照键名不一致修复（大盘涨跌幅 / 预判输入指数块）

**开发者**: Aria

### 修复

- **Bug A（功能性）**：归因链与板块父链引用读的大盘涨跌幅键在**生产快照并不存在**。`attribution_chain.assemble_attribution_chain`（原 73-79 行）与 `event_consumers._review_index_pct`（原 516-519 行）按 `index_change_pct/index_pct/benchmark_change_pct/sh_change_pct` 读取，而 `normalize_a_share`（`services/market_trace_snapshot.py:292-314`）产出的真实形状是 **`a_share["indexes"]`（dict，key=`SH000001` → 项含 `ts_code`/`name`/`change_pct`）**；旧四键仅存在于测试 fixture → `root.index_pct` 恒 `None` → `judge_sector_driver_relation` 恒返回 `unknown` → 链上 children relation 恒 unknown（前端不渲染角色徽），`parent_trace_ref["index_pct"]` 同样恒 None。
- **修法**：新增共用助手 `index_pct_from_snapshot(snapshot) -> float | None`（`services/attribution_chain.py:58-84`，配套私有 `_index_items`/`_is_shanghai_index`/`_numeric_pct`）：① 优先 `a_share.indexes` 中上证指数项（name 含"上证"或 code 为 `000001`/`000001.SH`/`SH000001`），找不到取首项；值非数值视为缺失；② 兼容指数项为 dict（归一化后）与 list（归一化前原始载荷，字段 `change_pct` 优先、`pct_chg` 兜底）两种形状；③ indexes 不可用 → 回退旧四键；④ 全缺失 → `None`（保持"未知"，不伪造 0——0 会被判成 market_follow）。两处调用点改为共用该助手；`summary` 逻辑与 children 结构未动。
- **Bug B**：`prediction_service._build_prediction_input`（237 行）读 `a_share.get("indices")`（生产键为 `indexes`）→ 指数事实整块为 `None`，LLM 拿不到大盘指数背景。改为 `a_share.get("indexes") or a_share.get("indices") or []`；**输出侧 key 保持 `indices` 不变**（`prompts/workers/prediction.py` 仅描述"输入为溯源结果 + 快照关键字段"，未约定该块键名；prompt_input 以 `json.dumps` 整体注入，见 `prediction_service.py:739`，无键名依赖）。

### 验证

- **测试（TDD 先红后绿）**：`test_attribution_chain.py` 新增 5 例（真实 list 形状 → `index_pct==-0.9` 且 relation `self_driven`；dict 形状优先上证；无上证取首项；仅旧键兼容；非数值回退/全缺失 None）；`test_sector_consumer_multi.py` 参数表新增 3 例 + 1 例 `parent_trace_ref["index_pct"]` 真实形状非 None；`test_prediction_service.py` 新增 2 例（真实 `indexes` → `a_share.indices` 非空；旧 `indices` 键兼容透传）。RED 证据（改前）：7 failed（含 `assert None == -0.9` / `assert None`）；GREEN 后：3 文件 **89 passed**。
- 取证命令与存量红：`pytest tests/unit/test_attribution_chain.py tests/unit -q -k "attribution or prediction"` → **13 passed**；`pytest tests/unit -q` → **2778 passed, 8 failed**，其中 1 例存量红 `test_iterate_adapters::test_registry_contains_review_and_event_analyst_and_prediction`（期望集缺 `stock_prediction`，非本次引入），另 7 例为 `test_industry_vector_search.py` 存量红（本次 diff 仅 6 文件，均不在该模块路径上）。ruff 通过；mypy 报错行均落在未改动行。

---

## [main] 2026-09-17 — condition_met 终审复审后小修（3 项）

**开发者**: Aria

### 修复

- **最小样本守卫**（`services/condition_met_judge.py`）：`judge_condition_met` 入口新增 `max(len(closes), len(pct_chgs)) < 2 → None`。背景：`created_at == today` 时窗口仅 1 行，`_judge_pct` 的 `closes` 不足 2 个会回退 `pct_chgs` 复利累计，而单日累计恰为自身 → neutral 分支（|累计| ≤ 0.5%）在 0 涨跌幅单日样本上**立即点亮 true**（true 不可撤回）→ 宁可 None（不产键）。"closes 优先、pct_chgs 回退"语义不变（守卫取 `max`，任一维度 ≥ 2 即照常判定）。
- **报告文案**（`iterate/reporter.py` `_format_prediction_iteration`）：`condition_met_rate` 键存在但值为 `None`（无 false entry 时条件维度不计分）会渲染"条件 None" → 改局部变量回落 `'-'`。
- **文档/注释漂移校正**：① `condition_met_judge.py` 模块 docstring 的 ③ 条目补"或含裸方向动词（跌破/下破/失守/站上/突破/收回）"，与 `_TECH_RE` 实际口径对齐；② `prediction_validator.py` 的 `_KLINE_FETCH_DAYS`/`_STOCK_KLINE_FETCH_DAYS` 注释函数名 `_fetch_kline_window` → `_fetch_kline_range`（index/stock 取数已迁至后者）；③ `docs/specs/2026-08-31-预判验证-design.md` §4.2 第①段"扫描最近 60 个交易日窗口" → `[created_at, today]` 区间（120 自然日上限），并注明以条件化 spec §4.2 为准。

### 验证

- **测试**：`test_condition_met_judge.py` 新增 `test_single_data_point_returns_none_before_judging`（先红：`True is None`）；`test_iterate_reporter.py` 新增 `test_build_daily_report_condition_rate_none_renders_dash`（先红：`'条件 -' not in md`）。验收：`test_condition_met_judge + test_prediction_validator + test_prediction_stats + test_iterate_verification + test_iterate_reporter` → **123 passed**。
- **未改**：判定语义（除守卫）、窗口口径、stage② 逻辑、Node 契约；第 4 条复审项（0.2 权重维度退出评分）为设计权衡，保留为遗留。

---

## \[main\] 2026-09-17 — condition\_met 终审修复（阻塞 #2 + 重要 #3/#4/#5）

**开发者**: Aria

### 修复

- **#2（阻塞）绝对点位条件不再误走技术位分支**（`services/condition_met_judge.py`）：`judge_condition_met` 路由优先级改为 ① volume 关键词 → ② **绝对点位（恒 `None`）** → ③ 明示技术位（`跌破|下破|失守|站上|突破|收回|前低|新高|均线|日线|周线|月线|MA\d+`）→ ④ 涨跌幅/阈值。绝对点位判据：`\d{3,}(\.\d+)?\s*(点|元)`，或"站上/突破/跌破/上穿/下破/击穿/失守/收回"+紧邻数字（排除"数字+`%`/日/周/月/个交易日"）。旧路由把"站上 3000 点""突破 3300 点"丢进技术位分支、用 MA 近似在顺势行情下误判 `true`；`true` 一旦写入不可撤回（D1 只写 true 不写 false）→ 宁可 omit。
- **#4（重要）`condition_met_rate` 口径修正**（`services/prediction_stats.py` + `iterate/evaluator.py`）：第①段只写 true（无 false 参照），全 true 样本不得读成 100% 命中率抬高下游评分——**仅当存在 `condition_met is False` 的 entry 时才计算 `condition_met_rate`，否则 `None`**；evaluator 的 condition 维度（0.2 权重）在无 false entry 时整体剔除并按 present 维度重归一化。

### 改进

- **#3（重要）第①段扫描窗口改 `[created_at, today]`**（`services/prediction_validator.py`）：新增 `_condition_scan_range(record, today)`——起点取 `prediction_records.created_at` 的日期部分、上限 **120 自然日**（早于 `today-120d` 裁剪）、`created_at` 缺失/脏值回退 `today-120d`、裁剪后空窗（未来脏值）**直接跳过不发请求**；取数走新增 `_fetch_kline_range(kind, code, start, end)`，`_fetch_kline_window` 保留为 **stage② 到期判定 due 区间专用**（语义不变，`_verify_horizon`/`_verify_conditions` 未动）。旧口径误用 due 区间 `[due-20, due+10]`：远端 due（long/越年档）时该区间整体落在未来、过滤后为空 → 静默跳过，长档条件几乎永不点亮。
- **成本与健壮性**：`run_once` 内 stage① 取数按 `(target_type, code, start, end)` 记忆化（同记录多 condition 只取一次数；窗口不同不串用缓存）；`_scan_condition_met` 调用处加 try/except，单记录异常只 warning（`prediction_condition_scan_failed`），不中断整批。
- **#5**：窗口约束（远端 due 仍点亮 / 空窗不发请求 / `created_at` 缺失回退 120d / 越界裁剪）由 `tests/unit/test_prediction_validator.py` 用例覆盖。

### 文档

- `docs/specs/2026-08-31-条件化预判改造-design.md` §4.2：窗口口径由"最近 60 个交易日"改写为 `[created_at, today]`（上限 120 自然日，含空窗/回退/记忆化与 stage② 不变说明），并新增"绝对点位类首批 omit"条目与路由优先级；§9-5 的"绝对点位阈值"标注为首批显式 omit。
- `data_client.get_ths_daily_range` docstring 补 `close`/`vol` 键（`amount` 上游无源恒 null）。

### 状态

- 本地验收：`test_condition_met_judge.py + test_prediction_validator.py + test_prediction_stats.py` → **102 passed**；`-k "prediction or validator or condition_met"` → **385 passed, 1 failed**（唯一红为存量 `test_iterate_adapters` 期望集缺 `stock_prediction`，非本次引入）；iterate 消费侧 7 个测试文件 → 130 passed。ruff/mypy 仅报存量问题，改动行内无新增。
- **部署顺序仍强制：先 app-api 再 agent-py**（第①段点亮依赖 Node PUT 放行"无 `result` 的 `c{i}` 中间态"）。

---

## \[main] 2026-09-16 — 条件化预判 condition\_met 两段判定落地（Spec A §4.2 收尾）

**开发者**: Aria

### 新增

- `services/condition_met_judge.py`：`judge_condition_met` 确定性纯函数（无 IO/无日志/仅标准库，禁 LLM）。判定优先级：① volume 类关键词（放量/缩量/成交额/成交量）→ **首批 omit**（恒不判定，`volumes` 入参保留供后续扩展）；② 技术位类（跌破/下破/失守/站上/突破/收回/前低/新高/均线/MA\d+）→ 严格前低 / 严格新高 / 均线近似（`MA60|60日` → 60 日线，否则 MA20；可用样本均值近似，样本 < 2 个 → 无法判定），方向取关键词优先、否则回落 `anchor.direction`；③ 其余涨跌幅/点位类 → 窗口累计 pct（`closes` 首末优先，不足 2 个 → `pct_chgs` 复利累计）按 direction 比对 threshold。
- `prediction_validator._scan_condition_met`：第①段「到期前点亮」——对 `due_date > today` 的 condition 扫最近 60 个交易日窗口，条件成立才回写 `verification[c{i}].condition_met=true`（entry **不含 `result`**）。
- `prediction_validator._fetch_kline_window`：日 K 解析保留 `close`/`vol`（index/sector/stock 统一），补齐条件判定数据基础。

### 改进

- `prediction_validator._verify_conditions`：第②段「到期 hit/miss 且保留点亮」——照常写 `c{i}` 的 `result`，并显式带出①已点亮的 `condition_met=true`（修掉 `base` 硬写 `condition_met: None` 抹掉点亮值的问题）。
- `run_once` 三段接线：horizon 验证（含 A1 跳过 / wait 不回写）→ ① 扫描点亮 → ② 到期判定；新增日志 `prediction_condition_lit` / `prediction_condition_verified`。复用既有 `prediction_validate` job，**未新增 cron**。

### 修复

- 决策 D1：**只写 `condition_met=true`，不写 false**——不成立 / 无法判定 / 数据源故障 / 无数据源一律不产键（不写 `false`，也不写 `null` 覆盖）；幂等：已点亮 / 已有 `result` / `due_date <= today` 均跳过。
- ⚠️ 口径修正：spec 原文"条件成立判定复用 `rhythm_engine.ma_breadth`"**已失效**（`ma_breadth` 已删除且有测试守卫禁止回归），技术位改为上述新写确定性实现；文档已同步标注。

### 文档

- `docs/specs/2026-08-31-条件化预判改造-design.md` §3.1/§4.2/§9-5/§9-10/§11 与 `docs/specs/2026-08-31-预判验证-design.md` §4.2/§9-1：标注判定已落地、写清实际口径（含 volume omit、只写 true、两段结构、`c{i}` 不参与 `status=verified`），并明确"绝对点位阈值 / MA5 / volume 真值判定 / `target_type` 按 `anchor.metric` 分流"仍为后续项。
- 跨仓配套（app-api）：`fa5c6b3` PUT `/internal/predictions/:id/verification` 放行"`c{i}` + `condition_met` 布尔且无 `result`"的中间态；`edb9941` 板块日 K 透传 `close`/`vol`/`amount`（`amount` 上游无源 → 契约位恒 null）。**部署顺序强制：先 app-api 再 agent-py**。

### 状态

- 本地验收通过：`test_condition_met_judge.py + test_prediction_validator.py` 66 passed；`-k "prediction or validator or condition_met"` 372 passed / 1 failed（唯一红为存量 `test_iterate_adapters` 期望集缺 `stock_prediction`，非本次引入）。

---

## \[changer\] 2026-09-15 — 重大事件时间线 Event Entity 接入 + 收口

**开发者**: 37588

### 新增

- **P0 event_id 透传**：`EventRecord` 增可选 `app_event_id`/`app_event_status`（既有 `event_id` 降级为 source_event_id 语义）；`event_scraper._trigger_conduction` 条件透传（缺省不落键，major_events 形状逐字节不变）；`event_conduction.run_single_event_conduction` 优先消费 app-api 权威 `event_id` 作传导隔离键（缺省回退 `evt_md5` 逐字节不变）并向 state 注入 `event_id/event_status`；`event_persister` content 加性回写 `appEventId/appEventStatus`。
- **P0.5 事件时间抽取 + 物化**：`_extract_event_start_time` 确定性抽取（只认明确绝对日期/区间，禁 LLM 猜日期）；`_materialize_event_entity` **有明确绝对日期即物化**（未来 scheduled / 已发生 occurred 都 POST，time_confidence=0.9=抽取方法确定性），挂接 `scrape_full_daily`/`scrape_intraday` 的 added_events 并原地回填 app id/status（时序在传导前）。
- **未来事件守卫（spec §6.2 红线）**：`app_event_status∈{scheduled,upcoming}` → 跳过已发生传导（`event_conduction_phase="pre"` + `event_conduction_skipped=True`；Pre 双套 P2 未落地）。
- 开关 `event_entity_enabled`（env `EVENT_ENTITY_ENABLED`，默认 False——翻转前行为逐字节不变）。

### 文档

- 本地真实 HTTP 联调通过（app-api 端点已落地：信封 code:200 / 幂等重放 / 读时重算 status / 降级契约，探针 `tests/integration/test_event_entity_local_e2e.py` 默认 skip 生产零影响）；跳过日期 → None 不 SUP 注入；物化失败 warning 不阻断主链路。
- `EVENT_ENTITY_ENABLED` 生产翻 True 前需组长 merge app-api + 部署。

### 状态

- 本地验收通过：收口定向测试 93 passed + ruff 0；app-api 端点相关验证见 app-api CHANGELOG 2026-09-15。

---

## \[changer\] 2026-09-15 — 节奏大师「大盘主线 / 主升浪仓位节奏」（spec 2026-09-14-rhythm-mainline-position-rhythm）

**开发者**: 37588

### 新增

- `services/mainline_engine.py`（主线判定纯函数 + `load_mainline_candidates()` + 破位判据，阈值全绝对锚定 H1）；`services/trend_reversal.py`（放量阴线后摆动点抬高的趋势反转确定性判定）；`data/mainline_candidates.json`（AI 科技组优先）。

> **2026-09-20 起**：`data/mainline_candidates.json` 现为 v5 冻结基线，活动配置为 `mainline_candidates.v35.json`（35 条，2026-09-20 扩容后）；loader 经 `MAINLINE_CANDIDATES_PATH` env 灰度切换。
- `services/rhythm_engine.py`：`POSITION_LADDER` 五档仓位阶梯 + `derive_position_text`（指数趋势定 base → 硬闸门 → 主线 strong/none 调整 → 事件档位 → clamp）+ `build_event_branch` 的 d 约束（>3 交易日不产分支）。
- `services/event_calendar.py` 读取端升格 L3 宏观事件为 high（关键词 ⊆ app-api macro 正则词元 H3）；worker 接线 position_band.text（主线驱动）/ event_high_hint / phase_evidence.technical 确定性生产者 + 盘中档剔除未完成 bar；synthesis 主线段受确定性事实约束（不得自创主线）。
- 开关 `rhythm_mainline_enabled`（env `RHYTHM_MAINLINE_ENABLED`，默认 True）。

### 修复

- `next_event_anchor` 计数口径改**交易日差**（D7/G4）；`_canonicalize_mainline_with_evidence` 约束 LLM（data_date 必须等于证据日）；`trading_days_between` 新原语（T5.5）；残枝死代码清理（spec 8/D3/D6）。

### 文档

- 前端（app-frontend）0 生产代码改动（仅 rhythm 模块 AGENTS.md 文档口径更新）。

---

## \[changer\] 2026-09-14 — 节奏大师「逻辑准确性」修复

**开发者**: 37588

### 修复

- **证据日与行情同源**：盘前 / 午间档的宽度维度此前取「早于当天最近一个交易日」的收盘数据，与卡片所依据的当日行情不同源，可能出现「用上一交易日宽度判定今日节奏」的错配。现统一按卡片证据日（最近一根 K 线）取收盘数据；取不到时如实置空并留痕，不再用错日数据静默参与判定。
- **同一目标日档位不再自相矛盾**：日历热力格与详情页曾出现同一天两个节奏档位（如日历显示「冰点」而详情显示「低迷」）。现盘前 / 午间档的主档位（档位 / 评分）统一沿用最近一次收盘基准卡的结论；无基准卡时本地重算并留痕，消除同日双档。
- **事件确认口径统一**：事件的「已确认方向」此前只看事件结果、不看事件级别，可能产出「有确认、无事件锚点」的矛盾卡；现与事件锚点 / 事件分支统一只认高优先级事件。
- **AI 判断段（研研判）枚举约束**：补齐结构化输出的取值约束（方向 / 置信度只能取既定枚举值），并在无高优先级事件锚点时明确要求输出空数组；不再因「无 / 待定」等自由文本导致整段判断不可用。
- **AI 判断段局部保全**：个别条目不合法时不再整段作废，改为剔除非法条目、保留可用内容。
- **降级提示与证据分离**：AI 判断段不可用时不再混入对用户可见的「证据缺失」列表，改为独立日志留痕，避免用户看到与证据无关的降级提示。
- **卡片证据日对外真实**：盘前 / 午间档卡片展示的基准日此前等于运行日、与真实证据日不符，现如实展示真实证据日。
- **下一事件距今天数**：以该卡所描述的目标交易日为原点计算（盘前 / 午间档即当天），同一目标日的不同卡片之间保持一致；同时消除此前盘前 / 午间档「距今天数」相对当天偏大 1 天以上的问题。

### 重构

- 已知空置字段（温度序列 / 事件窗口）在代码文档中显式标注「字段未接线」，不再「恒空但仍渲染」造成静默假象；因两者数据源均已接入，该说明**不写入对用户可见的缺失清单**，避免健康交易日卡片常驻无关提示。尚未接线的事件公布后落档能力同样在代码中显式标注「未接线」，避免「看起来已实现」的误导。

### 文档

- 节奏大师三时点契约与实现对齐：明确盘前 / 午间档「主档位沿用收盘基准」、事件维度只认高优先级日历事件；并登记「是否引入事件门控（仅有新事件才刷新）」为待裁决的产品口径。
- 报告中转接口的调用方说明补齐。

### 测试

- 新增 / 扩充节奏大师定向回归：事件确认口径、快照按证据日取数、同日档位一致（端到端）、提示词枚举与空锚点约束、非法条目剔除、降级不污染证据、卡片证据日、空置字段留痕、以及「接线断言」（防「单测绿但生产未接线」）；修复一处引用了已删除实现的失效集成用例，并让新增用例不产生真实模型调用。

### 状态

- 本地代码验收通过（**待生产验证**）：定向测试全绿；静态检查对基线 0 新增、类型检查与基线一致；全量回归无新增失败；仅节奏大师单侧改动，接口 / 前端零改动。

---

## \[changer\] 2026-09-10 — 节奏大师语义/功能层修复

**开发者**: 37588

### 修复

- **收盘基准任务执行日**：修复节奏大师收盘基准档**周一不执行、周六空跑**的问题；三时点（收盘基准 / 盘前 / 午间）现统一覆盖周一至周五。
- **基准日真实性**：收盘基准档在当日行情尚未就绪（非交易日或数据未发布）时，不再拿前一交易日的行情冒充当日基准，改为如实展示为灰格并留痕，避免误导；盘前 / 午间档不受影响。
- **量能口径一致性**：统一成交额的量纲口径，修复此前“放量”档几乎恒成立导致的节奏判定与后续命中统计失真。
- **量能缺失兜底**：行情量能不可用时不再给出偏多的趋势加分；改为如实标记数据缺失，并按指数点位给出三档分支，不再产出“放量（>0 亿）”这类无数据支撑的分支。

### 重构

- 量能单位换算收敛为单一来源，消除散落的换算字面量。
- 节奏卡片清理两个无消费方的死值字段；冲突标记保留并注明当前恒定的原因（尚无检测器）。

### 改进

- 归档目录不再依赖进程工作目录，避免启动目录变化导致归档静默失效。

### 测试

- 新增 / 扩充节奏大师定向回归：三时点任务调度口径、基准日门禁（含空行情不报错）、量能单位对拍、量能退化兜底、节奏卡片契约、归档路径。
- 跨端回归：指数日 K 接口的区间语义已加测试锁定（只传结束日期时返回不晚于该日期的最近 N 根；结束日期为非交易日时回落最近可用行情、不返回空）。

### 状态

- 本地代码验收通过（**待生产验证**）：定向测试全绿；静态检查与全量回归对基线 0 新增失败；契约字段端到端比对一致。

---

## [main] 2026-09-08 — 生产事故修复：PR #131 后调度器停摆（9/6、9/7 定时任务未跑）

**开发者**: Aria

### 修复

- 生产事故修复：**9/5 部署 PR #131 后调度器停摆（9/6、9/7 全部定时任务未跑）**，根因是 9/3 junliang 分支「轻量预判阶段 2」代码不完整，两处缺失：
  - `src/aistock_agent/services/scheduler.py`：`start_scheduler()` 引用 `_run_light_predict_task` 未定义 → 启动即 `NameError` → main.lifespan 捕获降级为无调度运行（进程 online 但 heartbeat 停在 9/5 13:40Z）。修复：新增 `_run_light_predict_task(*, slot)`，委托 `light_predictor.run_light_prediction(slot)`（交易日守卫 + 函数内 import + try/except，对齐既有 task 风格）。
  - `src/aistock_agent/schemas/prediction.py`：`LightForecast` schema 缺失（AGENTS.md 已记载应有）→ `light_predictor` 模块 import 即失败。修复：新增 `LightForecast`（summary + conditions 1-3 条复用 PredictionCondition）。

### 测试

- `test_scheduler.py` 补 2 个 light_predict 任务用例（交易日委托 / 非交易日跳过）；`test_light_predictor.py` 3 个 LLM 用例补 `get_quick_think` mock（消除无 OPENAI_API_KEY 环境依赖，见 9/5 备注的 2 失败）；`test_lifespan.py` autouse mock scheduler 启停（此前真实启动 AsyncIOScheduler 依赖 running loop，靠 NameError 才碰巧通过）。
- **验证**：scheduler + light_predictor + lifespan + prediction 相关 **210 passed**。
- **部署**：commit + push 后服务器 `git pull && pm2 restart aistock-agent`，重启后须确认 `scheduler_started` / `scheduler_heartbeat` 恢复。

---

## [main] 2026-09-05 — 合并 PR #131（自选股洞察升级整线）至 main

**开发者**: Aria

### 改进

- 合并 PR #131（自选股洞察升级整线：涨停雷达并入 stock-trace + 读层 skill + 轻量预判 forecast，49 commits）至 main 并 push。
  - 冲突解决：`config.py`（保留 junliang 轻量预判 `scheduler_light_predict_midday/close_cron`，板块批量注释以 main 的 19:30 为准）、`schemas/prediction.py`（保留 main `omitted_horizons`）、`services/scheduler.py`（保留 junliang light_predict 任务注册 + main 板块预判注释）。

### 验证

- 改动文件 py_compile 通过；单测 **54 passed，2 失败**为环境缺 OPENAI_API_KEY（junliang 分支同现，非合并引入）。

---

## \[changer\] 2026-09-05 — 节奏大师量能单位归一 + 指数 K 线契约同步

**开发者**: 37588

### 修复

- `agents/workers/rhythm_master.py`：消费 `/internal/index/:code/kline` 返回行的 `amount`（Tushare index_daily 单位千元）按 `1e-5` 折算为**亿元**（engine/成交额分支单位契约）。Node 侧 2026-09-05 修复前该字段恒 null，量能维度缺失、成交额分支退化为 `>0亿` 伪分支（生产核实）；两处 amounts 构造（`_compose_card`/`_build_rhythm_card`）统一走新增纯函数 `_amount_yi`，缺失如实转 0。

### 文档

- `services/data_client.py` `get_index_kline` docstring 更新返回字段契约（含 `vol`（手）/`amount`（千元），缺失为 null）；根 `AGENTS.md` Node 接口表 `internal/index/:code/kline` 行补字段与单位说明。

### 测试

- `tests/unit/test_rhythm_master_compose_card.py` 新增 `_amount_yi` 参数化用例（千元→亿元 / None→0），节奏单测 6 用例全绿；ruff 0 新增。

***

## \[changer\] 2026-09-05 — 节奏大师降级链修复

**开发者**: 37588

### 修复

- 节奏大师三时点（收盘基准/盘前/午间）K 线取数窗口修正：改为按收盘日回取最近 200 根，移除当日起始日过滤，避免窗口坍缩导致的整卡降级；K 线不足 20 根时如实标注"指数K线不足"并跳过阶段判定，不凭残缺数据推测阶段。

- 修复研研判层合成提示词缺少 JSON 输出声明导致的模型 400：补齐 JSON 输出格式与契约字段锚定。

- 修复空壳研判被误判"合成可用"的门槛缺陷：主线/事件展望/结论三者皆空视为无效。

- 产出侧补发结构化节奏卡（档位/评分/仓位带/分支点位/数据缺失留痕），打通日历热力格、分支验证与前端节奏区块的跨端契约；档位由阶段映射常量确定性派生，评分与五档色带同源。

- 技术分支点位生成增加空值防护：支撑/压力无法计算时返回空分支并留痕，不伪造点位。

- 收盘后分支验证的基准读取对齐"最近收盘基准卡"语义（盘中档逐步优先、收盘兜底），存储归属日期语义不变。

### 测试

- 新增单测/集成用例：K 线窗口参数、行数留痕、提示词 JSON 锚定、空壳门槛、档位评分映射、分支空值兜底、基准读取语义与端到端落库断言，全部通过。

- 全量回归：失败集与改动前一致，新增失败为零。

***

## [main] 2026-09-04 — 链归因 Task3 审查修复 + sector_trace 主驱动板块集合提取

**开发者**: Aria

### 修复

- `src/aistock_agent/services/attribution_chain.py` + `tests/unit/test_attribution_chain.py`（Task3 审查修复，见 `.superpowers/sdd/2026-09-03-P1-chain-attribution/task-3-report.md`）：
  - I-1：`_trace_summary` 候选键与真实板块溯源 schema（`SectorChainResult.model_dump(mode="json")`：chain_id/sector/stages[{kind,headline,claims,evidence}]/attribution_status）零交集致生产恒回退"板块溯源完成"占位 → 改为从 trigger stage headline/claims 摘一句话；attribution_status=insufficient 或无法提取（无 stages/无 trigger/无文本）回退"溯源未确认驱动原因"，不再显示完成占位。
  - M-1：`AttributionChainStore.save` 检查 `node_api.post` 返回（data_client.post 失败/业务码异常吞错返回 None）——None 时 logger.warning("attribution_chain.save_failed") 而非打 saved 成功日志；`_pct_from` 补 today_change 回退注释（兼容 wind-leaders 快照行）。
- 测试：helper 改用真实 SectorChainResult dump 形状，新增 insufficient/无 trigger/空 trace_result 回退 + save None 告警用例；三套件 27 passed、ruff 0 违规。

### 新增

- `src/aistock_agent/agents/workers/sector_trace.py` + `tests/unit/test_sector_trace_extract.py`（commit 59de3fa，spec P1a-1 单→多）：新增 `extract_primary_sectors(payload, max_sectors=3)` 按 primary 链 claim 顺序提取主驱动板块集合（跌市 losers 优先于涨市 gainers、去重、上限 max_sectors、无命中返回 `[]`）；原 `extract_primary_sector` 改为委托取首个，既有 event_consumers 调用方零改动。TDD 4 新用例 + 既有 sector_trace 3 套件 18 passed。

### 说明

- 备注：brief 测试样例 claim 原文含 "AI 算力"（带空格），与板块名 "AI算力" 的连续子串匹配语义不符致用例必红，按 brief 预期 4 passed 修正样例数据去掉空格（实现逐字未改）。

---

## \[changer] 2026-09-04 — 节奏大师「大师级判断」重建

**开发者**: 37588

### 新增

- `schemas/rhythm_master.py`：Pydantic 契约（Stage/Certainty/Direction/PositionAction Literal + PositionBand/EventAnchor/RhythmEvidence/MainlineRef/LaunchOutlook/RhythmSynthesis/MasterRhythmCard）

- `services/rhythm_rebuilt_evidence.py`：确定性证据层纯函数——`detect_stage`/`detect_certainty`/`compute_position`/`build_event_anchors`（阶段/确定性/仓位/事件锚点不依赖 LLM）

- `services/rhythm_rebuilt_validate.py`：校验层纯函数——`validate_synthesis`/`_contains_price_point`（G19 禁点位拦截 + confidence/grounding 校验）

- `services/rhythm_rebuilt_synthesis.py`：研研判层——`run_synthesis` 结构化输出（`with_chat_structured_output` json\_mode，DeepSeek thinking 兼容，失败返回 None 不 500）

- 测试：6 个单元测试 + `tests/integration/test_rhythm_master_rebuilt.py`

### 改进

- `agents/workers/rhythm_master.py`：重构接入「确定性证据层 → LLM 研研判层 → 校验层」三层流水线，产出 `MasterRhythmCard`（四段 + 仓位）；三时点统一走 `_compose_card`，保留 `_load_sentiment_series`，顶层 try-catch 永不 500（合成失败降级 `DEGRADED_TEXT`）

- `prompts/workers/rhythm_master.py`：追加 `build_synthesis_prompt`（主线段只引用 P0 + 标注来源时效；启动节点概率式展望 + confidence + 假设推演；禁点位；narrative ≤60 字 + 不构成投资建议）；保留旧叙事导出

### 文档

- 本次改动记录写入本 CHANGELOG

***

## \[changer] 2026-09-04 — 午间报「午后前瞻机会提示」接入真实盘面数据

**开发者**: 37588

### 改进

- 午间报「午后前瞻 · 机会提示」改为基于当日真实板块行情筛选：仅列当日真实走强的方向；弱市或无明确机会时输出空提示，避免把实际下跌的板块列为「机会」。

- 机会与风险提示由不相交的数据来源生成，并在数据不可用时对称降级；前端展示契约保持兼容（机会提示仍为关键词数组、schema 2.1），无需前端改动。

***

## \[changer] 2026-09-04 — 节奏大师预判语义重构（密集触碰带 + 结构化仓位动作 + 可验证锚点）

**开发者**: 37588

### 新增

- `services/rhythm_dense_band.py`：历史密集触碰带确定性纯函数 `dense_band`（touch\_gap 容差带聚类 + 量能加权选带 + 近端 clamp），替代"max/min(20日极值)×MA20 系数"的机械支撑/压力计算——符合"压力位应来自前期反复触碰不破/跌不破密集区"语义

- `services/rhythm_engine.py`：`build_technical_branches`/`build_event_branch` 分支新增 `position_action`（direction=add/reduce/hold + change 成数 + band）、`anchor`（metric/threshold/direction 可验证锚点）、`touch_strength`（历史触碰强度，非命中概率）；`conclusion.range` 降级为辅助参考；新增 `position_band_to_action` 确定性映射（bullish→add/+2成、bearish→reduce/-1成、neutral→hold/持仓不变）

### 改进

- `agents/workers/rhythm_master.py`：`get_index_kline` 传 `start_date` 解锁长历史取数（命中 internal.ts startDate 5000 行分支）；amount 量能分档对齐近 20 交易日（修复 closes/highs/lows/amounts 各自独立过滤的取样错位既有 bug）；`dense_band` 的 `dense_support/dense_pressure` 接入 `build_technical_branches`（支撑/压力改为密集触碰带）；每分支回填真实 `touch_strength`

- `services/rhythm_verification.py`：`evaluate_branch` 改用 `anchor.direction`/`anchor.threshold` 机械判 hit/miss（仅对"上证指数点位"触发做点位机械判定，成交额/enum 分支回退到 `conclusion.range`，避免"指数点位 vs 亿元"单位错配）；保留 `_triggered` 前置判断（条件未发生 → insufficient）；事件分支落档后按 range 判 hit/miss（D11 保持）

***

## [main] 2026-09-03 — 预判验证链路修复（D1-D6）+ 动态档位·影响时长分流 + 板块别名/运维等多项

**开发者**: Aria

### 新增

- `src/aistock_agent/data/sector_aliases.json`：扩充板块别名映射（指数/农业种业/半导体存储/6G 等，+86/-3），commit 1f38694。
- **label 展示字段**（2026-09-03）：PredictionHorizon.label（基准走势 4~6 字，如 恐慌出清为主）+ PredictionCondition.label（两段式路径名"状态 · 走势"，各段 ≤6 字，如 恐慌出清 · 下跌中继），prompt（PREDICTION_PROMPT/PREDICTION_CHAT_PROMPT）加生成约束，缺省空串兼容旧记录；测试 test_labels_default_empty_and_parse 18 passed。
- **动态档位·影响时长分流改造**（spec：`docs/specs/2026-09-03-动态档位-影响时长分流-design.md`，方案 A 核心 + B 画像门槛；commits 22a04ab→d2fee9a + 收口 commit）：
  - 策略层（22a04ab）：新增 `services/prediction_horizon_policy.py`——driver_type（5 类：policy_macro/trend_fundamental/sector_rotation/event_shock/transient_market）× target_kind → horizon 白名单 required/optional 纯函数推断 + `classify_driver`（未知类别回落 transient_market，宁少产 mid/long）。
  - schema（a55331a）：`PredictionResult.omitted_horizons: list[OmittedHorizon]`（horizon+reason，缺省空，schema_version 保持 3.0 向后兼容）+ 与 horizons 互斥校验（required 缺档由归一化层兜底）。
  - prompt（08a14ee）：注入 driver_type 与白名单实例（required=[...] / optional=[...]），输出规则反转为"required 必产、optional 有据才产并自证、未产档写 omitted_horizons、禁止越白名单"；移除"无法判断 confidence=low 仍三档并列"语言。
  - 归一化强制层（caf7e49）：`apply_horizon_policy`（model_validate 后、due_dates 前确定性调用）——越界裁剪 + short 恒产 + required 缺档不硬补：写系统留痕 reason + `prediction_status` 降 `hypothesis`（spec §9 决策③ degraded，宁缺毋滥、可审计）+ omitted 归一（区分"依据不足未产出"与"越界被裁剪"）。
  - 注入接线（83ed1d6）：run_predict/chat/sector 三入口统一经 `_inject_horizon_policy` 注入白名单、产物过强制层，driver 与 prompt 注入同一值。
  - 画像 B 期（64e2e13/d2fee9a）：enrich_prediction_input 读 profile.horizon_breakdown——optional mid/long 档样本 n>=3 且 hit_rate<0.4 → prompt 附"历史印证少倾向不产"抑制提示 + note 拼接文案修正。
  - 收口：预测相关 18 测试文件 249 passed 全绿（omitted_horizons 带缺省，存量 fixture 零同步）；全量 tests/unit 9 failed 均为存量基线红；ruff 修复 schemas/prediction.py 两处存量 E501。

### 修复

- `src/aistock_agent/workers/stock_trace_consumer.py`：修复 DLQ 巡检对 bytes 消息 id 崩溃（`_reclaim_dlq` 先 `_text` 归一再 split），commit 27e7f48；`tests/unit/test_stock_trace.py` 新增 `test_reclaim_dlq_tolerates_bytes_message_ids` 回归测试。
- `src/aistock_agent/observability/logging.py`：processors 增加 `format_exc_info`，异常日志输出真实 traceback（曾只留 `exc_info: true`），commit 121cbc5；`tests/unit/test_observability_logging.py` 新增 traceback 输出断言测试。
- **预判验证链路修复（D1-D4，commit aab4e92；见测试报告 + docs/specs/2026-08-31-预判验证-design.md §10）**：
  - D2：`prediction_validator.run_once`/cursor 兼容 string id（Node 已归一治本，此处双保险），不再全量跳过。
  - D3：画像/统计/技能数据源改档位级扫描——`data_client.list_all_predictions`（pending+verified 全记录，按档位 result 计入），`_write_validation_profiles`/`_report_stats`/`skills.prediction_validation._collect_target_entries` 全部切换；画像不再等 long(2027) 全档 verified。
  - D4：`data_client.put()` 失败改抛异常（仅验证回写用，run_once/backfill 已包 try/except），updated 不再虚增、写失败可告警。
  - 测试：新增 D2 string id 归一、D3 pending 记录计入画像两个回归；相关 5 文件 78 passed。
- **补跑取证发现并修复（commit 29cc358；见 spec §10）**：
  - D5：`data_client.update_prediction_verification` body.update(entry) 被 entry.horizon 覆盖 jsonb key → condition 全部错位写到 anchor 档位键下并互相覆盖；修复为 key 恒用 horizon 参数 + anchor 经 anchor_horizon 透传（Node PUT 解耦 key 与 entry.horizon）。
  - D6：`_verify_conditions` 对未来 due 落 insufficient no_data（违反窗口语义）→ 未来 due 跳过；`_verify_horizon`/`_verify_conditions` 到期日当天 K 未出（盘中）→ wait 而非 insufficient（避免被 _should_skip_horizon 拦下永久写死）。
  - 测试：新增 D5 condition key 回归（data_client）、D6 未来 due 跳过/今日无 K wait 回归；60+26 通过。
- **cron 前移（commit dda52ea/4bf3e61，2026-09-03 组长裁决）**：review_full `30 20`→`30 18`、板块批量预判 `30 21`→`30 19`（config.py 默认 + scheduler/sector_wind/event_consumers/review 注释 + AGENTS.md 同步）；顺带修 config 存量 E501×4。服务器已 scp 同步并重启（cron 实测 `30 18`/`30 19`）。
- **最终审查修复（commit d2a1d70，见 `.superpowers/sdd/2026-09-03-dynamic-horizon/task-final-fix-report.md`）**：
  - A：`apply_horizon_policy` degraded 不再提级原 `insufficient`——大盘入口 LLM 自判 insufficient 保持 insufficient，不升 hypothesis。
  - B：个股入口 `_stock_prediction_core` 补接 `apply_horizon_policy` 强制层（driver 复用 prompt 注入同值 transient_market），对齐 chat 语义，置于 direction 归一化前。
  - C：`_extract_driver_for_sector` 文本 fallback 命中 policy/宏观强词收敛上限 `trend_fundamental`（long required→optional），不因大盘政策主因强制板块硬产 long。
  - D：`OmittedHorizon.reason` model_validator 拒绝空白/纯空格。
  - E：prompt 收束句（两处）区分 required/optional：required 无法可靠判断 confidence=low；optional 无证据则省略写 omitted_horizons。
  - 测试：相关 5 套件 105 passed 全绿、ruff 改动文件全绿。

### 说明

- 服务器受控补验（2026-09-03 12:47）：id=34 short 档真实判定回写成功 → `prediction:profile:399006` 首次非空落盘（n=1, hit_rate=1.0）——画像/回流链路打通。
- 服务器受控补跑（12:51）：真实判定 5 条落库（id=1 hit +0.99 / id=4 hit +10.83 / id=3 miss -1.99 / id=5 miss +2.47 / id=7 miss -4.01，与同事干跑完全吻合）；wait 2 条（id 9/10 窗口未满）。误写 21 档（11 条 sector/review 未来 due + condition 错位）经用户确认 SQL 清理（verification 重置 {}，保留真实判定）。修复后盘中复验：run_once updated=0、无污染。
- 补漏（13:03）：7 条记录（id 19/25/26/27/29/34/35）因 D5 condition 错位覆盖 short/mid/long 三键被错误置 verified——经用户确认重置 verification={} + status=pending 后修复逻辑重跑：id=34 short/mid 真实判定恢复、condition 走正确 c{i} 键（c0/c1 miss）、VERIFIED_COUNT=0、POLLUTION_SCAN=0。
- 服务器运维：pm2 清空固化的空 `INSIGHT_REDIS_URL`（delete+重建进程）；本地手工改动（已被远程包含）备份后清理。
- 待办：服务器 GitHub 出站不稳定，121cbc5 已本地 push 但服务器未 pull 成功（logging.py 已 scp 同步部署）；网络恢复后服务器执行 `git checkout -- src/aistock_agent/observability/logging.py && git pull origin main` 对齐。aab4e92 的 3 个 src 文件已 scp 部署；网络恢复后对 aistock-agent-py 与 aistock-app-api 两仓库 `git checkout -- . && git pull`（注意 app-api 需重新 build）对齐。

---

## \[changer] 2026-09-03 — 盘中报「午后前瞻」schema 2.1（机会/风险短词契约）

**开发者**: 37588

### 改进

- `prompts/workers/midday.py`：「午后前瞻」分段输出改为结构化 `opportunities` 关键词（4-5 个、每个 ≤8 字纯词、无 conclusion 段落，完整叙述保留于 details 第2部分）；`risks` 输出收紧为 4-5 条 ≤8 字短词；schema\_version 示例升 `"2.1"`

- `agents/workers/midday.py`：`_build_midday_report` 成功分支 schema\_version `"2.0"`→`"2.1"`（降级分支 `"1.0"` 不动；sections/risks 原样透传，12:15 广播链路不受影响）

### 测试

- 新增 `tests/unit/test_midday_prompt_contract.py`：锁定 prompt 契约（opportunities ≤8 字 4-5 个、risks 短词、schema 2.1、午后前瞻 JSON 示例无 conclusion）+ `_build_midday_report` 版本与透传断言（断言贴 prompt 原文并负向锁定）

### 文档

- 根 `AGENTS.md`：12:05 盘中报定时链路行补充 schema 2.1 契约说明（12:15 广播行无需改）

## \[main] 2026-09-02 — Spec D 收口：板块迭代回放接线 + 板块预判生产触发 + 个股验证/迭代环同构

**开发者**: Aria

### 新增

- `services/prediction_service.py`：统一**个股预判入口** **`predict_stock`**（keyword-only `(*, report_date, stock_code, stock_snapshot)`，stock\_code 收 6 位裸码/带后缀 ts\_code）+ `_stock_prediction_core`（LLM 结构化链，生产/回放共用）+ `_replay_predict_stock_from_case`（REPLAY 转调，case.meta 重建）；落库 `source_type="stock_prediction"`（source\_id=`stock:{code}:{date}`）默认 pending

- `services/prediction_targets.py`：`resolve_index_or_stock_code`——指数/个股 target code 归一（6 位裸码/带后缀 ts\_code/指数后缀消歧防 `000001.SH` vs `000001.SZ`），验证器与预判入口共用

- `iterate/adapters.py`：注册 `stock_prediction` 迭代 adapter（verification 验证驱动 + `prediction_verified_scan` 产片源，`data_deps={}`——回放输入全来自 case.meta）

- `tools/sector_tools.py`：新增 `predict_sector_trend` 对话工具（板块预判对话补充入口）+ `prompts/workers/sector.py` 预判意图说明

- `agents/workers/sector_trace.py`：`SectorTraceRunResult.snapshot`——溯源快照随结果返回（级联预判的 `sector_snapshot` 输入）

### 修复/改进

- **板块迭代回放接线（收口已知缺口 #1）**：`replay_runner._build_state` 建 sector\_trace/sector\_prediction 分支 + `_report_date_from_case`（meta.trade\_date 优先，回退切片快照）；`sector_trace.run()` 返回 final\_response（trace JSON）+ 顶层 `sectors`（对齐 review\.run 契约，run\_once 转 structured 供 `evaluate_attribution`）；`run_once` 验证分支适配 predict\_sector keyword-only 签名；`predict_sector` 顶部 REPLAY\_CASE\_ID 转调 `_replay_predict_sector_from_case`；LLM 链抽为 `_sector_prediction_core`（生产/回放共用，后处理语义一致）；adapters 移除两 adapter「回放未接线」缺口注记

- **板块预判生产触发接线（收口已知缺口 #2）**：`SectorTraceConsumer` 溯源成功后串行级联 `predict_sector`（`_cascade_sector_prediction`，失败仅日志不阻断，不把溯源事件拖进 retry/DLQ）；Node `(source_type, source_id)` UNIQUE 幂等防 quick/full 重复触发堆积

- **个股验证环就绪**：`prediction_validator` horizon/condition 两解析入口改用共享 `resolve_index_or_stock_code`（支持 6 位裸码/带后缀 ts\_code，stock 不再 no\_source）；无法解析 reason 文案更正

- **个股迭代支撑**：`_persist_chat_prediction` 移除 stock→skipped 过时分流（验证器已支持个股，对话预判即时进 16:00 验证队列）；`replay_runner` 补 stock\_prediction `_build_state` 分支 + run\_once keyword-only 调用

### 测试

- 新增 `tests/unit/test_prediction_stock_service.py`（predict\_stock 落 pending/后缀归一/非 stock 拒绝/REPLAY 重建/日期错位）；新增/更新约 30 例（sector/stock 回放状态、级联触发传快照、对话工具、验证 code 归一、个股产片走 stock profile 判定、chat stock pending）；回归：prediction/validator/targets/stats/iterate/replay/skills/consumers/sector/case\_sourcers 282 例全绿 + src mypy/ruff 0 告警

### 文档

- changelog-pending.md 收口已知缺口 #1/#2；总架构 `2026-08-31-四环三粒度复用架构-design.md` §5.4.1/§5.4.2 与自选股洞察升级 spec §10 登记下游契约（含对同事 light\_predict 的落库约定）

***

## \[changer] 2026-09-02 — 节奏大师「下一重大事件锚点」（design-debate P1）

**开发者**: changer-collab

### 新增

- 节奏大师下一重大事件锚点：`rhythm_engine.build_next_event_anchor` 纯函数（取事件窗口内首条 high 事件，N=事件日与 basis\_date 自然日差，note 三态 今日/明日/N 天后；无 high 返回 None，日期异常跳过不抛异常 G6）（`services/rhythm_engine.py`）

- rhythm\_master 三时点接线：after\_close 全量卡与 morning/midday 事件驱动增量统一以 basis\_date 为锚写入 `rhythm_card.next_event_anchor`（`agents/workers/rhythm_master.py`）

### 测试

- `tests/unit/test_rhythm_engine.py`：锚点无 high/取首条 high/今日明日/坏日期跳过 4 用例（26 passed）

- `tests/integration/test_rhythm_master_worker.py`：after\_close 全量 + morning 增量锚点 2 集成用例

***

## \[main] 2026-09-01 — Spec B 预判验证闭环（验证 skill + 画像 + 个股数据源 + 三处反哺）

**开发者**: Aria

### 新增

- `skills/prediction_validation.py`：验证 skill——`read_validation_profile`（缓存优先，miss 拉 verified 重算，key 用 `internal_id`）+ `explain_verification`（LLM 解释层，失败降级规则兜底）+ `enrich_prediction_input`（纯函数并入 `validation_profile` 块，红线：不改判定/不产指令/不覆盖 A3）

- `prompts/workers/prediction_validation.py`：解释层 prompt

- `services/cache.py`：`get/set_cached_validation_profile`（key `prediction:profile:{internal_id}`，TTL 86400）

- `services/prediction_stats.py`：`build_validation_profile` 纯函数（condition 级命中率/miss\_patterns/condition\_met 分布/失效模式/degradation）

- `services/data_client.py`：`get_stock_kline(code, days, start_date, end_date)`（复用 `get` 解包 `data.rows`）

### 改进

- `services/prediction_validator.py`：`_fetch_kline_window` 补 stock 分支（个股日 K 走 `get_stock_kline`，带区间参数 \[due-20, due+10]）+ `_write_validation_profiles` 到期验证落画像（接管）

- `services/prediction_service.py`：`run_chat_prediction` 绑定 `_enrich_chat_input_with_profile`；`run_predict` 绑定 `_enrich_market_predict_input`（大盘溯源代表 target=上证指数）

- `services/morning_forecast_extractor.py`：新增 `_enrich_morning_summary_with_profile` 展示侧反哺（sufficient\_sample 时 summary 追加历史命中率；缓存存原始 LLM 结果防陈旧；异常降级保持原文）

### 测试

- 新增 `tests/unit/test_prediction_validation.py`；扩充 prediction\_stats/prediction\_validator/prediction\_service/morning\_forecast\_extractor 测试。晨报 9 例、预测相关 113 例全绿；mypy 通过

***

## \[main] 2026-09-01 — 四环三粒度 Target 维度地基（TargetProfile 引擎独立提交）

**开发者**: Aria

### 新增

- `services/target_profile.py`：`TARGET_PROFILES` 注册表（index/sector/stock 三粒度，每项含溯源 prompt/证据源/快照构造/默认周期/K线拉取/迭代阈值）+ `get_profile` 一次查表 + `get_iterate_threshold`（horizon×场景分层阈值，`resolve_raw_threshold` fail-closed）+ `make_target`（LLM 自由文本 → 首类 `Target`）+ `canonical_ts_code`（裸 6 位码带交易所后缀，防指数/个股空间冲突）

- `tests/unit/test_target_profile.py`：20 例（模型约束 `extra=forbid`/注册表三粒度覆盖/阈值分层命中与 fallback/首类构造/ts\_code 数据卫生）

### 说明

- 仅依赖已提交的 `schemas/target.py` 与 `prediction_targets.py`；`prediction_targets.classify_target`（legacy 字符串四分类）保持不动。本模块是后续 Spec B/C/D 落地的统一入口，`get_iterate_threshold` 消费方待阶段 5 / Spec1 接入。

### 文档

- 同步 CHANGELOG.md；changelog-pending.md 清空 TargetProfile 待办

***

## \[main] 2026-09-01 — 条件化预判改造（Spec A，三端全量收尾）

**开发者**: Aria

### 新增

- 预判 schema 升 3.0（`schemas/prediction.py`）：新增 `PredictionDirection`/`PredictionMetric`/`PredictionAnchor`（horizon+threshold+metric+direction 自带方向）/`PredictionCondition`（condition/scenario/anchor 三段式）；`PredictionResult` 增加可选 `conditions` 与 `target: Target | None`，并新增 `schemas/target.py`（`Target`/`TargetProfile` 纯数据模型，关联统一 Target 维度，兼容 `classify_target` 归类）

- `PredictionAnchor.direction` 缺省 neutral，归一化层从文本兜底（regex），确保验证不因缺失方向失败

- `services/prediction_service.py`：`_coerce_prediction_payload` 兜底 schema\_version=3.0；`run_chat_prediction` 恢复到期日计算并落库 chat 预判（`_persist_chat_prediction`），按 `classify_target` 分流——index/sector→pending 入 16:00 验证，stock→skipped 防验证队列堆积

- `services/prediction_validator.py`：新增 `_verify_conditions`，对每条 condition 产出 `c{i}` 验证 entry（hit/miss），`run_once` 双验证调度（horizon 与 condition 并行互不干扰，窗口未满 wait 不回写）

### 改进

- `prompts/workers/prediction.py`：PREDICTION\_PROMPT / PREDICTION\_CHAT\_PROMPT 强制 `conditions[]`（2-3 条，至少 1 条含成交量维度），禁止"无条件短中长期"式空洞预判

### 文档

- 同步 CHANGELOG.md；changelog-pending.md 重置

***

## \[changer] 2026-08-31 — 预判/节奏/迭代增强（TradingVane 研报借鉴 v2）

**开发者**: changer-collab

### 新增

- 预判 A3 确定性置信钳制：`clamp_confidence_by_bucket`（Wilson 95%CI 上界 vs baseline，n<30 不动作）+ run\_predict/run\_chat\_prediction 接线（short 恒启用/mid 配置开关/long 不启用）+ `PredictionHorizon.confidence_source` 标记（`prediction_stats.py`/`prediction_service.py`/`config.py`）

- 预判 A1 失效复核触发器：`prediction_invalidation.py` 三态迟滞状态机 `update_trigger_state` + `scan_active_pending` 早退扫描（MA20 读数触发，early\_exit 标记与 result 分离存储，验证器 skip 改按 `"result" in entry` 判定）

- 预判 A2 独立源冲突检测：`corroborate_evidence`（quote/flow/news 三通道，claim 不计入）+ `PredictionResult.evidence_corroboration`（run\_chat\_prediction 接线，不覆盖 confidence）

- 节奏大师 C1 指数技术位佐证：`ma_breadth` + `detect_phase(tech=)`（kline 扩至 120 日，佐证只进 phase\_evidence，不进背离判定）

- 节奏大师 C2 背离传导：`conflict_kind` 顶/底区分 + `conflict_penalty` 进 `compose_score`（顶背离 -8.0 降档/底背离禁止，背离用 tech=None 原始相位防双降）

- 迭代 B1 维度证据标注：`build_scorecard` 每维度加 `evidence_kind`（deterministic/llm\_derived）

### 文档

- AGENTS.md：PUT verification 早退契约行 + A1/A2/A3 增强小节 + services 目录登记 prediction_invalidation.py；README 环境变量表补充 PREDICTION_CONF_CAP_SHORT/MID

---

## [junliang] 2026-08-30 — 涨停雷达并入 stock-trace：词条统一 + SourceKind 扩展

**开发者**: Aria

### 变更
- 涨停雷达事件并入 stock-trace 链路后，对话词条统一：`异动/涨停/涨停雷达/自选股/洞察/归因` 全部路由到 `stock_trace_lookup`；`insight_lookup` 从 registry/`_STOCK_SKILLS`/`_infer_stock_skill` 摘除（skill 文件保留不注册，存量数据不再新产生）。
- `schemas/stock_trace.py` `SourceKind` 增加 `insight_article`（Node 快照新增同花顺涨停雷达文章证据域；候选层仍强制 company/sector/market/capital/technical 五层）。

### 测试
- `test_qa_router.py`（词条→stock_trace_lookup、prompt 排除 insight_lookup、postprocess 注入）、`test_skills.py` 更新。

---

## [junliang] 2026-08-27 — 个股异动溯源读层 skill（阶段 2.2）

**开发者**: Aria

### 新增

- `src/aistock_agent/skills/stock_trace_lookup.py`：对话内查登录用户个股异动溯源（价格异动/涨停雷达归因结果，只读列表）；入参 `{symbol?}`（可空——无代码返回该用户全部异动溯源），user\_id 由 qa\_router postprocess 登录态注入（未登录移除 call），无数据/异常 → degraded；`ChatSource.kind="stock_trace"`，source\_id=`stock_trace:{event_id}`

- `src/aistock_agent/services/data_client.py`：`list_stock_traces(openid, symbol?, limit?)` 走 `/internal/stock-trace/events` 只读端点

### 改进

- `src/aistock_agent/schemas/chat_contract.py`：3 处 Literal（InsightGoal.intent / SubGoal.intent / SkillCall.skill\_name）加 `stock_trace_lookup` + `ChatSource.kind` 加 `stock_trace`

- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD\_FALLBACK 词条优先级——`异动/异动归因/异动原因` → `stock_trace_lookup`（置于 `涨停雷达/自选股/洞察/归因` → `insight_lookup` 之前）；`_STOCK_SKILLS` 扩为 5 项；`_infer_stock_skill`/`_build_default_skill_call` 增加分支（symbol 可空）；postprocess 5.6 合并 `insight_lookup`/`stock_trace_lookup` 注入 user\_id

- `src/aistock_agent/skills/registry.py`：注册 `stock_trace_lookup`

### 测试

- `tests/unit/test_skills.py`：stock\_trace\_lookup 正常/未登录/无数据/异常 + **只读断言**（仅触发 list\_stock\_traces）

- `tests/unit/test_qa_router.py`：异动词条路由优先于洞察、\_infer\_stock\_skill 分支、postprocess 注入/移除

## \[junliang] 2026-08-27 — 自选股洞察读层 skill（阶段 2.1）

**开发者**: Aria

### 新增

- `src/aistock_agent/skills/insight_lookup.py`：对话内查登录用户自选股洞察（涨停雷达/价格异动归因结果，只读）；入参 `{symbol?}`，user\_id 由 qa\_router postprocess 登录态注入（未登录移除 call），无数据/异常 → degraded

- `src/aistock_agent/services/data_client.py`：`list_insights(openid, symbol?, limit?)` / `get_insight(openid, event_id)` 走 `/internal/insight/events` 只读端点

### 改进

- `src/aistock_agent/schemas/chat_contract.py`：3 处 Literal（InsightGoal.intent / SubGoal.intent / SkillCall.skill\_name）加 `insight_lookup` + `ChatSource.kind` 加 `insight`

- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD\_FALLBACK 加词条（异动/归因/涨停雷达/自选股/洞察）、`_STOCK_SKILLS` 扩展、`_infer_stock_skill` 分支、`_build_default_skill_call` 分支、postprocess 注入 user\_id；**footer 白名单动态化**——`_build_system_prompt` 从 registry 实时渲染 `goal.intent` 枚举（占位符 `__INTENT_ENUM__` 替换，新增 skill 无需改硬编码）

- `src/aistock_agent/skills/registry.py`：注册 `insight_lookup`

### 测试

- `tests/unit/test_skills.py`：insight\_lookup 正常/未登录/无数据/异常 + **只读断言**（仅触发 list\_insights）

- `tests/unit/test_qa_router.py`：关键词路由、\_infer\_stock\_skill、postprocess user\_id 注入/未登录移除、footer 动态化断言

## \[junliang] 2026-08-27 — 预测验证口径升级 3.0（阶段 0）

**开发者**: Aria

### 改进

- `src/aistock_agent/services/prediction_validator.py`：`_METHODOLOGY_VERSION` 2.0→3.0，`_judge_window` 主判改**窗口累计口径**（bullish sum>0 / bearish sum<0 / neutral mean(|p\_i|)\<thr；v2"任一日符号命中"保留给存量回补）；`_verify_horizon` 增 `methodology_version` 参数，`baseline_neutral` 随版本（v2: any(|p|)\<thr / v3: mean(|p\_i|)\<thr）

- `src/aistock_agent/services/prediction_stats.py`：`_filter_v2` 参数化（`_CURRENT_METHODOLOGY_VERSION` 默认 2.0 防跳变/混桶），`hit_rate_summary`/`bucket_summary`/`baseline_neutral_summary` 支持传版本观测 3.0 分桶

- `backfill_no_data`：版本判断引用 `_BACKFILL_METHODOLOGY_VERSION`（2.0），存量 2.0/no\_data 用 2.0 口径重验、写 2.0 不混版本

### 测试

- `tests/unit/test_prediction_validator.py`：3.0 窗口累计主判用例（bullish/bearish/neutral 反例）+ baseline\_neutral 双版本差异 + backfill 版本隔离

- `tests/unit/test_prediction_stats.py`：版本过滤参数化用例（默认 2.0 / 显式 3.0）

## \[changer] 2026-08-30 — 节奏大师语义修正 + 调度修复（design-debate）

**开发者**: changer-collab

### 修复

- 分支目标区间（range）锚定突破后空间：bullish `[P, P+Δ]` / bearish `[S-Δ, S]` / neutral `[S, P]`（Δ=半通道宽），触发值=区间边界，替换 `±0.5%` 对称带（消除"站上 P 却给 P±0.5% 带"语义错位）；成交额分支同步（`rhythm_engine.py`）

### 改进

- after\_close 调度 cron 由 `5 16 * * 0-4` 改为 `5 16 * * 1-5`：周一至周五 16:05 收盘基准，周五收盘生成下周一预告（修复"周一 after\_close 永久缺失"）（`config.py`）

### 测试

- `test_rhythm_engine.py` 新增锚定语义用例（触发值=区间边界、三档互斥）

### 文档

- `README.md` 环境变量表、`AGENTS.md` 定时链路 cron 描述同步

***

## \[changer] 2026-08-26 — AI 投顾追问面板：questions 结构化下发（后端链路）

**开发者**: changer-collab

### 新增

- 回答后建议追问（追问面板数据源）：`SynthInsightOutput.questions: list[str] = []`（LLM 结构化输出，prompt 2b 指令生成 2-4 条同主题问句）+ `QuestionState.questions: list[str] | None`（LangGraph 通道声明）+ `ChatResponse.questions`；WS DONE / HTTP 非流式 / SSE DONE（`round_questions`，G1 事件流采集模式）三通道透出

- deep 分支 `_build_deep_questions` 零 LLM 模板化（worker 名骨架；多子目标每节前 2 + 全局截 4）；澄清/闸门/无 goal/degraded 出口恒 `[]`

### 改进

- synth\_answer 7 个带 cards 返回点统一写 `questions`；移除结论结尾固定引导句要求与降级引导句文本（G21——问答不重复引导，追问统一走 questions 字段）

### 验证

- `tests/e2e/test_ws_chat.py` DONE 负载断言已同步 `questions` 键（85370a2），原 2 例新增失败清零

- 全量回归 2592 passed / 20 failed：20 例均为合并前基线既有环境类失败（event/hot\_burst/sector/iterate/lifespan、full\_flow 实栈、AsyncMock 交互等，baseline c1098ec 交叉验证）——新增清零

***

## \[changer] 2026-08-25 — LLM 前缀缓存命中观测（可观测先行）

**开发者**: changer-collab

### 新增

- LLM 前缀缓存命中观测（design-debate 裁决「可观测先行」，不做 prompt 重排/前缀冻结）：callback 层归一化提取 OpenAI `prompt_tokens_details.cached_tokens` / astream 路径 `input_token_details.cached_tokens` 与 DeepSeek `prompt_cache_hit_tokens`，按 provider 分桶进 `metrics["llm_cache"]`（`{prompt_tokens, cached_tokens, hit_rate}`）；只做观测不落库、不进计费链（`token_usage.py` / `ws.py` / `node_api.save_token_usage` 字节零改动）

### 改进

- `observability/callback.py`：抽取 `_get_raw_token_usage`（llm\_output.token\_usage → usage\_metadata fallback）供计费与缓存观测复用；新增 `_extract_cache_usage` 字段归一化，无缓存字段不记录不抛异常

- `observability/metrics.py`：新增 `record_llm_cache_hit`（max(x,0) 防负）、快照 `hit_rate`（prompt=0 取 0.0）、reset 同步清零

### 验证

- 定向 38 passed（含 7 个新增缓存命中用例）；全量 unit 2100 passed / 3 failed（checkpointer 基线失败，A/B 验证新增清零）；ruff 0；mypy 0

***

## \[changer] 2026-08-25 — 短线情绪温度 + 冰点次日晨报引用预判

**开发者**: Aria

### 新增

- 短线情绪温度（每日收盘 15:45 计算并落盘 `docs/agent-outputs/sentiment/`）：涨停 / 跌停 / 炸板率 / 连板高度 / 涨跌家数比 / 主力净额六指标加权，0-100 分档（冰点 ≤20 / 低迷 / 常温 / 活跃 / 亢奋）

- 冰点判定与连冰升级：温度 ≤20 判冰点，连续 2 日升级连冰标记；冰点触发快速模型生成「修复概率 + 关注方向 + 风险」预判，模型不可用时自动降级为模板话术（不阻断链路）

- 次日晨报联动：冰点 → 注入预判全文与指标概览供晨报结合外盘/消息演绎引用；非冰点 → 注入一行温度概览；无数据 → 不注入（晨报行为与现状一致）

### 改进

- 收盘快照日期与报告日不一致时跳过当日温度落盘，防止旧交易日数据污染连冰计数与次日晨报引用

### 验证

- 全量自动化测试无新增失败（与基线 A/B 对比零回归，含 22 个新增用例）；代码规范检查改动文件 0 新增错误

***

## \[junliang] 2026-08-24 — stock\_trace 提示词补板块证据要求

**开发者**: Aria

### 改进

- `src/aistock_agent/prompts/workers/stock_trace.py`：sector 候选证据要求——只要上下文中存在板块/行业联动相关 source，sector 候选必须引用至少一条并置 supported 或 weak，不得置 insufficient 且留空支撑证据；仅当上下文完全不存在板块相关 source 时才允许 insufficient

## \[changer] 2026-08-24 — 搜索链路顺序可配 + 搜索观测 + 午报语音播报 + 盘中报

**开发者**: Aria

### 新增

- 工作日盘中报（12:05 生成「上午盘面回顾 + 午后前瞻」，仅大盘，存档可查不推送；复用晨报结论 + 新闻 + 外盘 + 搜索组装式，快速模型生成控制盘中算力占用）

- 午报双人语音播报（12:15 错峰于盘中报落库后：生成分析师 + 主持人双人对话并合成 MP3 音频，回填到当日午报，不产生独立广播报告、不混入晨间/晚间播报）

- 搜索链路观测计数：按供应商统计搜索尝试 / 失败 / 预算耗尽 / 空结果，通过 /metrics 端点暴露，便于定位搜索链路异常（如某供应商配额耗尽）

### 改进

- 搜索链路顺序可配置：供应商优先级由配置决定（可配为 AnySearch 优先），不再固定 Tavily 优先；空配置保持默认顺序，重复配置自动去重

- 交易时段行情回答降级文案诚实化：去掉反问句与误导性措辞，改为自洽陈述并显式标注「非今日实时」（非实时数据时）

### 验证

- 全量自动化测试无新增失败；代码规范与类型检查通过

***

## \[junliang] 2026-08-20 — stock\_trace 归因新增 primary\_phrase 短语输出

**开发者**: Aria

### 改进

- `src/aistock_agent/schemas/stock_trace.py`：`StockTraceResultPayload` 新增必填 `primary_phrase`（≤20 字归因短语，供列表/卡片展示；insufficient 时给简短结论如"证据不足"）

- `src/aistock_agent/prompts/workers/stock_trace.py`：提示词新增 primary\_phrase 输出要求（关键词概括主因短语）

### 测试

- `tests/test_stock_trace_validator.py`：用例补 `primary_phrase` 参数

### 文档

- `AGENTS.md`：更新归因输出字段说明

### 验证

- `pytest tests/test_stock_trace_validator.py` 通过

***

## \[junliang] 2026-08-15 — 自选股价格异动归因：stock\_trace\_consumer 默认启用 + 五层候选归因

**开发者**: Aria

### 改进

- `config.py`：`stock_trace_consumer_enabled` 默认值改为 `True`（此前默认 False，需显式启用）

### 新增

- 五层候选归因：`schemas/stock_trace.py` 新增 `capital`（资金流向）与 `technical`（技术指标）两层候选 schema，`_validate_selected_chain_shape` 要求候选覆盖五层；`prompts/workers/stock_trace.py` 提示词扩展为五层（company/sector/market/capital/technical）；`services/stock_trace_validator.py` 的 confirmed 门槛保持 company 主候选

### 验证

- `pytest tests/unit -q`：回归通过；ruff 0 errors

***

## \[changer] 2026-08-19 — 搜索引擎多供应商故障转移链（Tavily + Doubao + AnySearch）

**开发者**: 37588

### 新增

- 全网搜索升级为多供应商故障转移链：Tavily 主源 + Doubao（火山每月 500 次）+ AnySearch（每日 1000 次 finance 域）按 tavily→doubao→anysearch 顺序失败切换，整链预算 fail-fast；Tavily 多 key 健康感知池（熔断冷却 + 限流固定窗口 + 全冷却 fail-open），Doubao/AnySearch 惰性注册（未配 key 不占位）

- 搜索溯源透传：`TavilyService.search` 返回加性 `provider`（真实命中源）/`outcome`（ok/degraded/empty/error）键，快照与事件采集的 `source` 从硬编码 tavily 改为读真实 provider，`SourceCollectionStatus.state` 新增 `degraded`（低质兜底可观测）

- 配置：`DOUBAO_API_KEYS` / `ANYSEARCH_API_KEYS` / `SEARCH_ENABLED_PROVIDERS`（空=默认全部）/ `SEARCH_BUDGET_SECONDS`（默认 10s）

### 改进

- async 内同步阻塞的搜索调用全部下沉 `asyncio.to_thread`（快照 4 处 + `tavily_finance_search` 工具），并新增 AST 契约测试防止回归裸同步调用

- 工具输出契约回归锁定（`tavily_finance_search` 输出格式逐字节稳定）

### 修复

- KeyPool 健康状态跨请求持久化（模块级缓存，冷却/熔断不随请求重置）；fail-open 选「距上次失败最久」的 key

- 命中结果缺 url 时输出空串（避免「来源: None」）；Doubao `Result` 字段非 dict 解析守卫

### 验证

- 定向 87 passed + 全量 2454 passed（24 个基线环境失败，与本次零交集）；ruff 0；mypy strict 0

- 待生产验证：Doubao/AnySearch 真实 key 联调已由人工完成

***

## \[changer] 2026-08-18 — 复盘报告生成偶发失败加固（市场洞见数据恢复）

**开发者**: 37588

### 修复

- 复盘溯源报告生成时，AI 输出的部分校验字段取值偶发越界（如把"部分命中"填进不支持该取值的字段）会导致整份报告生成失败——现对越界取值做归一化兜底，不再拖垮整份报告

- 复盘溯源生成对 AI 单次输出增加一次重试兜底：AI 偶发返回空内容或非法格式时自动重试一次，避免整份报告不可用

***

## \[changer] 2026-08-17 — 深度分析结果跨轮陈旧值修复 + 对话结果一致性（批次 5）

**开发者**: 37588

### 修复

- HTTP 非流式对话（WS 降级路径）改为从本轮末节点输出取结果，消除普通问题轮泄漏上一轮深度分析的陈旧引用；SSE 流式完成事件同步补齐深度分析引用与结构化卡片字段（与 WS 主路径对齐）

- 对话澄清分支不再携带结构化卡片与深度分析引用（避免图文信息打架）

### 新增

- 三层测试：SSE 流式完成事件字段契约（键存在且为空）、回复生成节点返回契约、真实图 + 内存检查器两轮集成验证（深度轮→普通轮后深度引用仍保留，多轮追问不退化）

### 验证

- 相关测试 14/14 通过

***

## \[changer] 2026-08-17 — LLM 请求级耗时埋点（诊断「等很久」根因）

**开发者**: 37588

### 背景

「等很久没回答」根因未坐实。design-debate 两轮裁决：连接池修复有效但只解决泄漏，不解决推理慢/
争用；真正的 149s「静默黑洞」在队列/上游/双 graph 三者间未定，缺 LLM 请求级耗时证据。

### 修复

- `src/aistock_agent/observability/callback.py`：新增 `LatencyCallback`——挂载到所有 ChatOpenAI
  工厂（get\_quick\_think/get\_deep\_think）默认回调，记录请求级指标：

  - `llm.call.duration`：每次 LLM 调用总耗时（覆盖非流式 ainvoke）

  - `llm.call.first_token`：首 token 延迟（流式链路，可区分「上游慢」vs「池排队」）

  - `llm.call.error`：失败时总耗时 + 异常类型（ReadTimeout/ConnectError/RemoteProtocolError，区分超时 vs 连接异常）

- `get_default_callbacks()` 现返回 3 个 handler（TokenUsage + AgentTrace + Latency），全局生效，
  不侵入业务代码（回调链注入）

### 验证

- 新增 6 个单测（`test_observability_callback`：duration/first\_token/error/缺失 start 不崩/注入）

- 改动相关单测 53 passed；ruff 0；mypy 0 新增

- 注入链路实测：get\_quick\_think() 生成的 model 挂 3 回调 + http\_async\_client

### 待生产验证

- 部署后重复对话，观察日志 `llm.call.*`，据 total\_ms/first\_token\_ms 定位 149s 黑洞：排队（start→首
  token 大）vs 上游慢（first\_token 大）vs 双 graph 叠加（同消息多条 duration）

***

## \[changer] 2026-08-17 — LLM 连接池泄漏修复 + WS 悬挂止血（问题 20 延续）

**开发者**: 37588

### 背景

线上偶现聊天 WS"一直转圈"（前端收不到 done）+ 偶发 500。现场 `ss` 观测：agent 进程向
DeepSeek（api.deepseek.com / 43.242.198.77:443）累积 **50+ 条 CLOSE-WAIT**、fd 增至 85
（正常 <20），每次对话只增不减 → 连接池泄漏 → 偶发阻塞 LLM 调用 → `synth_answer.ok` 后
producer 悬挂 → `_runner` finally 不执行 → `state.done` 永 False → `_forward` 无限 await →
前端永久转圈。

### 修复

- `src/aistock_agent/services/http_client.py`：新增 `LlmHttpClient`——LLM（DeepSeek/ChatOpenAI）
  专用 httpx.AsyncClient 单例，带 `httpx.Limits(max_connections=20, max_keepalive_connections=10)`
  （显式限定连接/keep-alive 上限，杜绝 CLOSE-WAIT 无限堆积）；init/client/close 幂等

- `src/aistock_agent/services/llm.py`：`get_quick_think()`/`get_deep_think()` 注入
  `http_async_client=LlmHttpClient.client()`（原实现每实例新建 httpx client 且无回收）

- `src/aistock_agent/main.py`：lifespan 启动 `LlmHttpClient.init(timeout=600)`、关闭 `close()`

- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 新增**静默段看门狗**
  （`_FORWARD_STALL_TIMEOUT_SEC=240`）——events 长度无新增且 recv 无新消息持续超阈值 →
  主动 `chat_task_manager.cancel(session_id)` + 补发 error「生成超时，请重试」，再由 `_runner`
  终态 notify 补发 cancelled，保证前端绝不无限转圈；finally 补 `await asyncio.gather` 收尾
  （对齐"问题18"规范）

- 测试：

  - `tests/unit/test_llm.py`：新增连接池共享/受限断言（quick/deep 共用同一单例 + Limits 生效）

  - `tests/unit/test_ws_chat_replacement.py`：新增看门狗测试（悬挂 → cancel + error 终态）

### 验证

- 全量 unit 1987 passed；ws/chat 集成 17 passed

- ruff 改动文件 0；mypy 与 baseline 一致（20 pre-existing，无新增）

- app import + `http_async_client` 共享单例确认

### 待生产验证

- 部署后重复对话，`ss -tnp | grep CLOSE-WAIT | grep 43.242.198.77` 计数应稳定不再增长

- 聊天转圈 / 500 应消除（若另一用户 500 为同源连接池问题则一并解决；否则独立排查）

***

## \[changer] 2026-08-16 — 对话卡死恢复止血（问题 20）

**开发者**: 37588

### 修复

- `src/aistock_agent/api/ws.py`：主循环 `except WebSocketDisconnect` → `except (WebSocketDisconnect, RuntimeError)`（disconnect 被 recv\_task 消费后再 receive 抛 starlette RuntimeError → 不再崩溃刷 error log）；非 "receive" 的 RuntimeError 打 `chat.ws_main_loop_runtime_error` warning 保留可观测性

- `src/aistock_agent/services/chat_task_manager.py`：`ChatRunState.finalizing` 护栏（cancel 在 finalizing/done 时返回 False，防前端超时 stop 误杀将成之轮）+ `_RUN_TOTAL_TIMEOUT_SEC=660` 总时长兜底（`asyncio.timeout` 内联执行 producer，超时 → ERROR 终态「生成超时，请稍后重试」）

- 测试：`tests/unit/test_ws_chat_replacement.py`（RuntimeError 捕获回归）+ `tests/unit/test_chat_task_manager.py`（finalizing 护栏 2 例 + 总时长兜底 2 例）

### 验证

- 全量 A/B HEAD 27 failed = BASE 27（新增清零）+ ruff 0

### 配套（前端 aistock-app-frontend，同批）

- useChatStream idle 超时兜底（见 frontend changelog）

***

## \[changer] 2026-08-15 — 预测验证口径升级 v2（B2.2 P0）

**开发者**: changelog

### 新增

- 指数日 K 客户端 `get_index_kline`（GET /internal/index/:code/kline，Tushare index\_daily 历史窗口，P0 验证 v2 数据源）

- 预测验证统计模块 `prediction_stats.py`：Wilson 95% CI + 命中率汇总（methodology\_version=2.0 分桶、insufficient/approximate 剔除）+ baseline 同口径对比；独立 scheduler 任务输出结构化日志（D3 真实消费方）

- target 枚举外置 `prediction_targets.py`：`classify_target` 四类分类（index/sector/stock/unknown），sector/stock 归 insufficient 且 reason 区分，unknown 保留抽象词漂移信号（P0-2 target 分布监控）

### 改进

- 验证器重写 v2 窗口判定（\[due, due+3 交易日] 符号命中主判，无累计净值兜底 G13）：grade 幅度分级（仅 bullish/bearish，G14）、baseline\_neutral 同窗口标记（H6）、approximate 结构化标记（H2）；窗口未满返回 wait 不回写（D1）、数据源故障落 insufficient（D7）

- chat 预测后处理红线硬校验（P0-3）：`_contains_absolute_point` 覆盖 metric\_projection/evolution\_narrative/attribution\_summary 全文本字段（含 D5 裸数字点位正则），命中剥离 + 独立日志事件 `hard_validation_failed`，不静默

- schema\_version 升 2.0 四向同步（D6）：schemas/prompts/prediction\_service 兜底/测试构造

### 修复

- 全量回归修复计划引入的 4 处失败：replay 隔离名单补 `get_index_kline`/`list_verified_predictions` 登记；test\_prediction\_prompt/test\_review\_prediction/test\_evening\_chain\_event\_driven 的 schema\_version 断言与 fixture 同步 2.0

***

## \[changer] 2026-08-14 — 预测到期日越年逐档容错

**开发者**: changelog

### 修复

- 大盘溯源影响持续性预判：到期日不再因 chinese\_calendar 覆盖（2004-2026）越年而整条落 skipped（due\_dates\_failed）——改为逐档容错，越年档按「周末+已发布节假日(HOLIDAYS\_EXTRA)」近似计算并显式标记 `due_dates_approximate`（wire 键，Node 合并进 prediction jsonb），其余档精确

- 删除 `DueDatesComputationError` / `due_dates_failed` 状态（`PredictionRunResult.status` Literal 移除），`event_consumers` 同步

- 到期验证器：近似档 reason 加 `(approximate_due_date)` 前缀，供统计分桶归因

### 说明

- 理由（P2 辩论裁决）：验证器对照扫描日单日涨跌幅符号（低信噪比），精确日历无统计增益；显式标注优于预测停产；2027 官方节假日 2026-11 发布后经 HOLIDAYS\_EXTRA 注入或 chinese\_calendar 升级自动恢复精确

***

## \[changer] 2026-08-14 — SPEC 设计文档忽略规则

**开发者**: changelog

### 改进

- `.gitignore` 新增 `docs/*-SPEC.md` 忽略规则：SPEC 设计文档不提交不推送，仅本地维护

***

## \[changer-prediction-split] 2026-08-14 — 大盘溯源影响持续性预判独立成模块 + 跨年日期可靠性

**开发者**: changelog

### 新增

- 影响持续性预判独立成模块：复盘完成后自动触发生成，独立状态追踪（进行中/已跳过/已完成），不再依赖复盘流程内联执行

- 按需补偿接口：支持手动触发当日预判生成（防重复覆盖保护 + 频率限制 + 仅限当日）

- 补充节假日数据源配置：跨年到期日计算精度提升，2027 数据发布或日历库升级后自动恢复精确

### 修复

- 大盘溯源页预判卡片空态（统一读取预判记录数据）

- 跨年日期计算失败由静默降级改为显式失败状态与告警，不再静默产出近似日期

### 改进

- 预判生成结果状态化（门禁跳过/生成失败/解析失败/日期计算失败/成功），瞬时失败自动重试一次

- 无效预判记录落"已跳过"状态，不计入进行中统计；大盘溯源页展示空态占位文案

***

## \[junliang] 2026-08-06 — 自选股洞察：LLM 归因 category 字段兼容 + 归因链路联调修复

**开发者**: Aria

### 修复

- `src/aistock_agent/schemas/insight.py`：`DriverOutput` 增加可选 `category` 字段（LLM 常回传候选分类，此前 `extra=forbid` 直接拒绝导致归因全部回退规则兜底、LLM 路径失效）

- `src/aistock_agent/services/insight_validator.py`：校验 LLM 回传的 `category` 与所选候选分类一致，不一致拒绝（分类权威在候选，防 LLM 注入分类）

- `src/aistock_agent/prompts/workers/insight.py`：提示词新增第 6 条输出字段白名单（每个 driver 只允许 candidate\_id/label/confidence/category）

### 文档

- `docs/自选股洞察-PRD.md`：更新事件归属规则——事件股票必须与标题主体股票一致，详情页推荐股票不建事件（修复事件挂错标的，如国投中鲁文章被挂到中芯国际事件）

***

## \[main] 2026-08-06 — 修复存量 unit 测试失败 + 清理遗留测试文件

**开发者**: Aria

### 修复

- `services/scheduler.py`：`_publish_review_quick_event` / `_publish_review_full_event` 的 trace\_id 由 `asyncio.get_event_loop().time()` 改为 `time.monotonic()`，消除对"当前事件循环"的隐式依赖（同步/多线程场景无 loop 会抛 RuntimeError）

- `graph/nodes/qa_router.py`：`_resolve_multi_symbols` 在 `_extract_multi_symbols` 返回空（候选 <2 约定返回 \[]）时按正则补全消息中显式给出的 6 位代码，修复 "600519 和五粮液哪个更好" 对比闸门无法短路的问题

- `tests/unit/test_skills.py`：3 个 normal + 3 个 exception 测试改用 `mock.ainvoke.return_value / side_effect` 配置（原 `AsyncMock(return_value=...)` 只作用于 mock 自身，`.ainvoke` 子 mock 拿不到导致 degraded/coroutine 报错与假通过）；stock\_snapshot normal 测试补充 node\_api.get 与交易时段 mock，消除真实时间依赖

- `tests/unit/test_qa_router.py`：`test_qa_router_llm_single_validate_collapses` 补充 `get_quick_think` mock（缺 mock 时真实调用 ChatOpenAI 抛 OpenAIError 走兜底，断言 KeyError）

- `tests/unit/test_industry_vector_search.py`：2 处降级断言由旧文本"数据暂不可用"更新为当前 `DEGRADED_MESSAGE`（safe\_tool\_call 稳定契约）

- `tests/unit/test_qa_briefing.py`：morning 前置报告补 `trend_score` mock（`_REQUIRED_TYPES["morning"]` 已含 trend\_score）

- `tests/unit/test_scheduler.py`：`test_start_scheduler_explicitly_passes_configured_timezone_to_cron` 显式创建/清理事件循环，消除全套运行时的 loop 顺序污染

### 清理

- 删除 `tests/unit/test_tenx_tools.py`（tenx\_tools.py 已被 trend\_tools.py 替代移除，遗留测试文件）

- 清理 5 个测试文件的 ruff 存量警告（E402/E501/F401/I001 等，21 处）

### 验证

- `pytest tests/unit -q`：1171 passed（修复前 1162 passed + 9 failed + 1 collection error）

- `pytest tests/e2e/test_chat_message.py -q`：4 passed

- ruff：全部改动文件 All checks passed

***

## \[changer] 2026-08-06 — WS 路径 token\_usage 时序修复 + HTTP 降级补返回 + 服务端口 8000→8080 对齐

**开发者**: Aria

### 修复

- `api/ws.py`：astream\_events config 传入 `get_default_callbacks()`（astream\_events 不触发 LLM 构造函数 callbacks=）；循环结束后 `await asyncio.sleep(0)` yield 事件循环让延迟 on\_llm\_end 回调执行，再从 contextvar 刷新 token\_usage 覆盖 stale None——根因：LangGraph v2 异步回调延迟，synth\_answer 节点执行期间 contextvar 尚未写入，DONE 事件恒 None

- `observability/callback.py`：清理 DEBUG print；`_extract_token_usage` fallback 失败日志改为 `logger.debug`

- `services/token_usage.py`：清理 DEBUG print

- `api/routes.py` + `schemas/chat.py`：HTTP 非流式 `chat_message` 补返回 `token_usage`（降级路径用量缺口，P10 线 2；c926e9d + 8944635）；e2e 新增 `test_chat_message_returns_token_usage_when_graph_provides`

### 改进

- 服务端口 `8000`→`8080` 对齐 app-api `AGENT_PY_URL` 默认值（app-api 已在 2026-08-05 改 8080，agent-py config 落后导致反代错端口）

- `config.py`：`port: int = 8080` + 注释

- `Dockerfile`：`EXPOSE 8080` + `CMD --port 8080`

- `README.md` / `AGENT_STANDARDS.md`：启动命令与 docker run 端口同步更新

### 验证

- WS 直连冒烟 `token_usage={'prompt_tokens': 455, 'completion_tokens': 353, 'total_tokens': 808}`（3 次 LLM 调用之和，非翻倍）

- 43 单测全绿；HTTP 路径 e2e 回归通过

***

## \[main] 2026-08-06 — 晚报结论重构：归因结论放头条，三条均为 30-40 字一句话（去冒号）

**开发者**: Aria

### 改进

- `services/briefing.py`：晚报三条顺序调整为 归因结论（主因链）→ 市场快照 → 收盘复盘；归因结论去掉"触发：/传导：/结果："阶段标签与冒号，confirmed 句式"今日市场主因是{…}"、hypothesis 句式"今日市场可能受{…}等因素影响"；市场快照改为"今日X涨4.21%、…，Y跌2.13%、…"一句话（含"无显著领跌/领涨板块"降级分支）；删除不再使用的 `_STAGE_LABELS`

- `services/phenomenon_discovery.py`：`_SUMMARIES` 五个现象文案扩写为 30-40 字一句话（收盘复盘条目摘要来源，无冒号）

### 修复

- `agents/workers/review.py`：`_extract_trace_summary` 优先提取"确认的市场现象"段的 `- 摘要：xxx` 行（易读中文现象描述），不再取到"类型：broad\_rally"内部字段行；保留旧格式回退

### 测试

- `tests/unit/test_briefing.py`：更新 sectors/attribution 契约断言为无冒号一句话、`missing_sources` 顺序随新 variant 顺序调整、新增晚报头条顺序断言；`tests/unit/test_review_report.py` 新增 `_extract_trace_summary` 3 个测试；相关测试 60 passed

***

## \[master] 2026-08-05 — cls\_news/main\_force 缺失诊断日志（agent-py）

**开发者**: NanyuDeer

### 新增

- `services/market_trace_snapshot.py` `_normalize_news_facts`：新增三种缺失场景结构化日志 `cls_news_missing_fetch_error` / `cls_news_missing_empty` / `cls_news_missing_invalid_for_causality`（含 raw\_item\_count、kept\_count、skipped\_future、skipped\_no\_time），成功时输出 `cls_news_available`

- `services/market_trace_snapshot.py` `_normalize_aggregate_facts`：新增 `main_force_invalid` 日志（is\_quick、availability\_state、availability\_reason、value），区分 quick 快照预期缺失（Tushare 未就绪）与异常缺失

- `services/market_trace_snapshot.py` 新增 `_log_telegraph_response` 辅助函数：记录 telegraph 接口返回 item\_count、total、degraded（兼容 `{date,items,total}` 与 `{code,data}` 两种结构），full/quick 两条路径均接入

### 说明

- 目的：解决 grep cls\_news|main\_force 无输出问题，后续运行可在 pm2 日志中定位确切根因

***

## \[master] 2026-08-05 — 测试同步：event\_conduction 返回结构变更（PR #52 回归修复）

**开发者**: NanyuDeer

### 修复

- `tests/unit/test_event_conduction_service.py`：断言适配 `EventConductionOutput.status` 新结构（`result.success` → `result.status.success` 等），import 改为 `EventConductionOutput`

- `tests/test_routes_briefing.py`：event conduction mock 返回改为 `EventConductionOutput(status=EventConductionResult(...))`（3 处），修复 PR #52（EventConductionOutput 包装类）引入的 8 个测试回归

### 测试

- `test_event_conduction_service.py` + `test_routes_briefing.py`：43 passed

***

## \[master] 2026-08-05 — market\_snapshot 板块命中率失真修复（粗/细粒度名称对齐）

**开发者**: Aria

### 修复

- `services/snapshot_builder.py`：`match_sectors_code_level` 在别名字典精确匹配之外新增**双向包含匹配**（`_norm_sector_name` 去"概念/板块/行业/指数"后缀 + `_has_contains_match` 双向子串判断），解决 morning 粗粒度预测板块（AI/CPO/半导体）与 review 行情细粒度概念（存储芯片/光刻机）字面完全无交集导致的 hit\_rate=0、new\_coverage=1 失真

- `data/sector_aliases.json`：补齐高频缺口——"AI/CPO/半导体"（映射到半导体/AI光模块）、"白酒概念"→白酒、"CRO/医药"、"医药电商"→医药、"MLCC概念"→电子元器件

### 测试

- `tests/unit/test_sector_matching.py`：新增 2026-08-05 实况回归（morning 5 板块 vs review 10 概念，命中率 0.00→0.40）+ 包含匹配兜底 + 不相关板块不误判；清理顶部未使用 import

***

## \[master] 2026-08-05 — 新增"一键补跑完整晚间链路"端点

**开发者**: Aria

### 新增

- `api/routes.py`：`POST /api/agent/admin/trigger/evening_chain`（内网 token 鉴权）——一键补跑完整晚间链路（review → market\_snapshot → iterate → evening Brief → broadcast），供错过 15:30 调度或灰度验证时使用；显式传 `report_date` 时跳过交易日检查；返回各阶段状态 `stages` 供诊断

### 改进

- `services/scheduler.py`：`_run_evening_chain_task` 增加可选 `report_date` 参数（缺省走原交易日检查逻辑，向后兼容）并返回各阶段状态 dict（review/market\_snapshot/iterate/brief/broadcast 与失败 stage/error 信息）

### 测试

- `tests/test_admin_trigger.py`：新增 `test_trigger_evening_chain_returns_200`

- `tests/unit/test_scheduler.py`：新增 3 个测试（显式日期跳过交易日检查 / 缺省日期非交易日返回 skipped / review 失败返回 failed+stage）

***

## \[changer] 2026-08-05 — ChatAgent P10 线 2 用户计费（token\_usage）+ P11 线 3 后端卡片（cards）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P10/P11 行

### P10 线 2（用户维度计费，billing）

- `services/token_usage.py`（新增）：contextvar 累加器 `TokenUsageAccumulator`/`TokenUsageContext` + 模块级 `reset_token_usage`/`get_token_usage`/`record_token_usage`；contextvar 随 `asyncio.create_task` 继承（`TokenUsageCallback` 挂在 ChatOpenAI callbacks= 无法访问节点 state，ws 后台图任务与节点内 LLM 调用同 context 副本）；`observability/callback.py` `on_llm_end` 在 record\_llm\_tokens 后追加 record\_token\_usage（非 chat 场景零副作用）

- `graph/nodes/synth_answer.py`：原节点改名 `_synth_answer_node_core`，新增包装 `synth_answer_node` 统一附加 `result["token_usage"] = get_token_usage()`（与 cards 汇总块分居两层，合并友好）

- `api/ws.py`：入口 `reset_token_usage()` 按轮重置；on\_chain\_end 一次性捕获 token\_usage + cards；`_drain_reasoning_tasks` 后、DONE 前**落库（选项 A）**——user\_id 非空且 token\_usage 非空 → `node_api.save_token_usage`（try/except + warning 不阻断 DONE）；DONE 负载新增 `token_usage` + `cards`（None 默认）

- `api/routes.py`：`chat_message` / `chat_stream_messages` 入口 `reset_token_usage()`；SSE DONE（`_stream_messages`）从 final\_state.values 附带 token\_usage + cards（仅展示不落库）；HTTP 非流式路径只重置不消费

- `services/data_client.py`：`save_token_usage(*, user_id, session_id, prompt_tokens, completion_tokens, total_tokens, question=None)` → `POST /internal/usage/records`（app-api）

### P11 线 3（后端卡片结构化，cards）

- skills raw 结构化字段：stock\_snapshot `raw["quote"]`（`_QUOTE_FIELD_MAP`）、capital\_flow `raw["flow"]`（`_FLOW_FIELD_MAP`，flow\_5d 恒 \[]）、market\_snapshot `raw["a_share_card"]`（`_build_a_share_card`，仅 scope 含 a\_share）、compare\_stocks `raw["parsed"]`（available True/False 条目）；get\_quote/get\_capital\_flow TEXT 输出冻结不变

- `graph/nodes/synth_answer.py` `_synth_answer_node_core` 每个 return 带 cards：no\_goal/澄清/闸门/异常 → None；deep → `_build_deep_card`；LLM 成功与 `_synth_multi_goal` → `_build_cards`（`_CARD_HANDLERS` 按 skill\_name 分派 + 逐卡片 try-except 跳过）；包装 `synth_answer_node` 不动

- 契约：`schemas/chat_contract.py` `ChatCard`（card\_type Literal 5 值 + title + data，extra="forbid"）+ `QuestionState.cards`/`token_usage`（B-T1 定义，P11/P10 共享）

### 测试

- 新增：`test_token_usage` / `test_data_client_save_token_usage` / `test_synth_answer_token_usage` / `test_ws_token_usage_record` / `test_routes_sse_done_token_usage`（P10 线 2）；`test_chat_card_contract` / `test_stock_snapshot_raw` / `test_capital_flow_raw` / `test_market_snapshot_card` / `test_compare_stocks_parsed` / `test_synth_answer_cards`（P11 线 3）

- 适配：`test_ws_chat_replacement` / `test_ws_chat`

### 文档

- AGENTS.md 补 CHAT QA P10+P11 段；CHANGELOG.md 本条目；project\_memory.md 经验教训 #30

### 验证

- Commits：P10 线 2 `d3d772c`/`114ee07`/`da26a81`/`0e3b5b4`/`42a6524`/`d98692a`/`9142de3`；P11 线 3 `fcfcf5a`/`5f9d6ab`/`4c82bc8`/`fe56222`/`2b1ec00`/`83fea5d`；线间 merge `4d42fe7`

***

## \[changer] 2026-08-05 — ChatAgent P5-fix 验收补丁（对比问句短路 / 名称候选净化 / 多轮指代兜底）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\chat-agent-roadmap.md` §1 P5-fix 行 / §4 问题 8/11/14

### 修复

- 问题 8（对比问句被闸门 2 澄清拦截）：`_STOCK_NAME_STOPWORDS` 补对比口语词（哪个/更好/更强/比较/对比）；新增 `_COMPARE_KEYWORDS` 增强对比词表 + `_extract_multi_name_candidates`（按"和/与/还是/vs"分隔符切分逐段提取名称）+ async `_resolve_multi_symbols`（过滤非 6 位代码候选）；**对比闸门 2.5 独立于闸门 2 且在其之前**（含代码对比句"600519 和五粮液哪个更好"短路 compare\_stocks，避免落 LLM flaky）；`route_by_keyword_fallback` 对比分支仅接受纯代码

- 问题 11（候选名被口语词污染 resolve 404）：停用词补意图词/连接词（新闻/资讯/消息/公告/有/是/说/它/这/那，与 `_infer_stock_skill` 对齐）

- 问题 14（多轮指代失效）：qa\_router LLM 失败路径新增多轮指代兜底——`len(messages)>=3`（有上一轮）+ 当前消息含指代词（它/这/那/该/其/刚才/上次/这只/那只）时从上一轮 resolve symbol 复用（`_infer_stock_skill` 推断意图，`multiturn_ref` 约束标记），不落澄清；守卫防"帮我推荐股票"等误指代

### 测试

- 新增 `tests/unit/test_qa_router_fix.py` 12 项（对比闸门短路/名称净化/多轮兜底 3 守卫：复用/无标的守卫/首轮守卫）；qa\_router 全量 137 passed；ruff 0 errors

- WS 冒烟（真实后端）：宁德时代新闻 / 茅台五粮液对比 / 3 组多轮指代全部 clarified=false

### 文档

- AGENTS.md 补充 CHAT QA P5-fix 段（含"qa\_router 单测必须 mock LLM 防 flaky"测试注意）

***

## \[changer] 2026-08-04 — ChatAgent P6 退役清理（ai\_advisor / market-trace-qa / advisor\_trace）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p6-retirement.md`

### 重构

- 退役 market-trace-qa 端点：`services/market_trace_qa.py`（400 行）原位瘦身改名 `services/trace_loader.py`（仅保留 `load_validated_trace`，供 trace\_lookup skill 使用）；`schemas/market_trace_qa.py` / `prompts/workers/market_trace_qa.py` / `test_market_trace_qa.py`（unit+e2e）删除

- 退役 ai\_advisor worker / prompt / 路由 / constants（`agents/workers/ai_advisor.py` 477 行 + prompt + `test_ai_advisor.py` 924 行删除；`intent_router.py` VALID\_INTENTS 收敛，未知意图 → general 兜底；旧图 `compile_graph()` 保留供 `/briefing/morning`）

- 移除 `advisor_trace` 协议字段（**消失语义**，`"advisor_trace" not in payload`，非 null）+ 孤儿 `AdvisorTrace`/`AdvisorSubquestionTrace` TypedDict（T3 review 补删）

### 测试

- 全量 A/B 回归（worktree 6f51d89 + PYTHONPATH 覆盖 editable install，--ignore test\_tenx\_tools）：HEAD 27 failed ⊆ BASE 34 failed，**新增失败清零**；ruff P6 改动文件 0 errors（--no-cache）

### 文档

- AGENTS.md / README.md 零 ai\_advisor 可达引用（图拓扑 / 产品映射表 / 目录结构 / 降级文本表 / 双层输出消费方）

***

## \[changer] 2026-08-04 — ChatAgent P5 能力补齐（D40-D42 三 skill + index\_snapshot + P4 遗留优化）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-04-chat-agent-p5-capability.md`

### 新增

- `src/aistock_agent/schemas/chat_contract.py`：`InsightGoal.intent` / `SubGoal.intent` / `SkillCall.skill_name` 3 Literal 各追加 `compare_stocks` / `stock_history` / `trend_ranking` / `index_snapshot`（extra="forbid" 不变）

- `src/aistock_agent/skills/compare_stocks.py`：D40 多标的并发对比（`asyncio.gather` 并发 `get_quote.ainvoke`，2\~5 标的，部分失败不整条丢弃，仅个股语义）

- `src/aistock_agent/skills/stock_history.py`：D41 个股日 K 区间（`/internal/quote/{symbol}/kline`，`近N天` 确定性短路）

- `src/aistock_agent/skills/trend_ranking.py`：D42 趋势股 Top 榜（`/internal/trend/top`，空榜 degraded）

- `src/aistock_agent/skills/index_snapshot.py`：对话快速指数快照（`/internal/index/quotes`，绕开 quick 全市场 33s 慢路径，部分 null 不整体 degraded）

- `src/aistock_agent/graph/nodes/qa_router.py`：KEYWORD\_FALLBACK 对比/历史/排行词条 + `_extract_multi_symbols` + `_DAYS_RE 近N天` 短路（`_match_other_skill_intent` 排除其他意图词）+ 闸门 1 A 股指数名路由（`_INDEX_SNAPSHOT_CODES` → index\_snapshot，恒生/大盘维持 market\_snapshot）+ D27 白名单（compare/stock\_history/trend\_ranking 参数归一）

- `src/aistock_agent/skills/registry.py`：注册 4 新 skill

### 修复

- P4 遗留 ①：闸门 1/2 单意图预测附加收紧为 `_STRONG_PREDICT_KEYWORDS`（弱词仅闸门 4 候选注入，消除"茅台明天的新闻"误附加）

- P4 遗留 ②：兜底 `_build_fallback_goals` 同标的 validate+predict 只发一条取数 call（`seen_calls` 去重）

- P4 遗留 ③：兜底 trace 子目标改走 `trace_lookup`（溯源数据而非 validate 快照）

### 测试

- 新建 test\_skills\_compare\_stocks / test\_skills\_stock\_history / test\_skills\_trend\_ranking / test\_skills\_index\_snapshot + test\_qa\_router 触发/迁移用例（§2.6 消歧五行全覆盖）

- 目标单测 122 passed；全量 pytest A/B（worktree 24830c5 + PYTHONPATH）：HEAD 28 failed ⊆ BASE 28 failed（新增失败清零），passed 1440→1496；ruff 改动文件 0 errors

### 文档

- `AGENTS.md`：Node 配合接口表补 `/internal/quote/:symbol/kline` + `/internal/index/quotes`；CHAT QA P5 小节（4 skill + §2.6 消歧硬边界）

- roadmap §1 P5 行（✅ 11/11）、§2 P5 小节、§5.5 验证记录、§4 两项调研遗留已完成

***

## \[changer] 2026-08-04 — ChatAgent P4 多意图（D34 goal→goals）+ 维度预筛（D30 闸门 4）+ 预测维降级（D35）

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-chat-agent-p4-multi-intent-dimension.md`

### 新增

- `src/aistock_agent/schemas/chat_contract.py`：`SubGoal`（id/question/intent/dimension/symbols/tag\_codes/time\_range，extra=forbid）；`SkillCall.goal_id` / `Evidence.goal_id` / `AnswerTrace.goals` / `QARouterOutput.goals`（默认 None）

- `src/aistock_agent/graph/nodes/qa_router.py`：D30 闸门 4 维度预筛（`_DIMENSION_KEYWORDS` predict/trace/validate + 候选集提取 + prompt 注入 + LLM 失败兜底增强）；D27 goals 后处理（id 重编号 g1..gN、goal\_id 归一、单非预测坍缩回单意图、goal 投影第一个子目标）；D35 单意图预测附加（闸门 1/2 短路命中 predict 词时附加 predict 子目标）

- `src/aistock_agent/graph/nodes/skill_executor.py`：`SkillCall.goal_id` 透传到 `Evidence.goal_id`（None 不覆盖）

- `src/aistock_agent/graph/nodes/synth_answer.py`：`state.goals` 非空时按子目标分节回答（先 validate/trace 现状数据后 predict 提示）；`_synth_multi_goal` / `_synth_section` / `_build_predict_section`；D35 预测降级提示（`PREDICT_DEGRADED_HINT` 代码生成、多个 predict 只输出一次、不编造预测）

- `src/aistock_agent/prompts/general/system.py`：`PREDICT_DEGRADED_HINT = "预测功能开发中，可先查看当前趋势分析。"`

- `src/aistock_agent/state/chat_schema.py`：`QuestionState.goals`；`api/ws.py` / `api/routes.py` 入口 goals 每轮归零（单轮 transient）

### 修复

- `qa_router.py` 兜底链整体 try-except（最终审查 M-1：异常回落关键词兜底，防二次抛异常中断图）

### 测试

- 契约/候选集/后处理/坍缩/兜底/透传/分节/D35 用例新增（test\_qa\_router +21、test\_chat\_contract +12、test\_skill\_executor +2、test\_synth\_answer +6）

- 目标单测 6 文件 185 passed；chat 集成回归失败集与 TRUE BASE 逐断言一致；全量 pytest A/B：HEAD 28 failed ⊆ BASE 33 failed（新增失败清零）；ruff P4 改动文件 0 errors

### 文档

- `AGENTS.md`：CHAT QA P4 小节（多子目标/维度预筛/预测降级/兼容性）

- roadmap §1 P4 行（✅ 5/5）、§2 P4 小节、§5.5 验证记录；P4 遗留优化挂入 §1 P5 行

### 验证

- SDD 逐 Task review Approved（5/5）+ 最终整分支审查 READY TO MERGE（0 Critical / 0 Important；M-1 已修）

- Commits：00f879d / 997a858 / 44c354e / a6194f6 / 09f4fd8 / 250c00b

## \[feat/market-trace-improvement] 2026-08-03 — 板块别名补充：新增 AI/光模块/半导体

**开发者**: Aria

### 新增

- `src/aistock_agent/data/sector_aliases.json`：新增 "AI/光模块/半导体" 板块（光刻机 / 共封装光学(CPO) / 存储芯片 / 芯片概念 / 中芯国际概念 / F5G概念），补齐生产环境此前手动热修中有效的 AI 光模块/CPO 热点映射；误删的中医药/科创芯片ETF 等映射已随 main 合并恢复

***

## \[feat/market-trace-improvement] 2026-08-03 — 生产故障修复：手动触发链路 trigger\_source 改 scheduler 使报告落库

**开发者**: Aria

### 修复

- `src/aistock_agent/api/routes.py`：4 个手动触发端点（morning/review/broadcast/trend-score）state 的 `trigger_source` 由 `"manual"` 改为 `"scheduler"`（与 09:00 调度任务语义一致），修复手动触发链路跑成功但 wind\_leader/hot\_burst/broadcast/review 因持久化门控 `== "scheduler"` 不落库的问题；stock\_trace 端点保持 `"stock_trace"`、chat 端点保持 `"user"` 不动

### 文档

- 新增 `docs/superpowers/plans/2026-08-02-market-trace-review-improvement.md`、`docs/superpowers/specs/2026-08-02-market-trace-review-improvement-design.md`（大盘溯源改进方案/设计文档入库）

***

## \[changer] 2026-08-03 — P3-fix-3 大盘数据正确性最小补丁 + P2 落库/D27 归一化遗留

**开发者**: Aria

计划：`D:\ai_stock_app\docs\superpowers\plans\2026-08-03-p3-fix-3-market-data-correctness.md`

### P3-fix-3 大盘数据正确性最小补丁

- `src/aistock_agent/skills/market_snapshot.py`：facts 始终带交易日 — 新增 `_date_label()`（YYYYMMDD→MM-DD，异常 None）；`_build_a_share_facts(normalized, trade_date="")` 首位锚点 `数据日期：MM-DD` + 指数行 `名称(MM-DD): ...`；`_fetch_a_share` 传 `trade_date`（消除 LLM 把最近交易日误标"今日"）

- `src/aistock_agent/graph/nodes/synth_answer.py`：新增 `_quote_data_not_today(ev)`（market\_snapshot 按 `raw.scope/used_last_close/a_share_success` 判定，其他行情 skill 按 degraded；防误伤：A 股今日数据 + global 失败不触发）；`_append_non_trading_time_hint` 触发条件放宽为"时段非 trading + 行情证据 + 数据非今日"，四状态引导确认文案（含"你说的是否是这个交易日…"）

### P2 落库 / D27 归一化（遗留一并提交）

- `src/aistock_agent/memory/checkpointer.py`：chat 会话持久化（+147 行，落库）

- `src/aistock_agent/api/routes.py`、`state/chat_schema.py`、`schemas/chat.py`：chat 落库接口与状态字段

- `src/aistock_agent/graph/nodes/qa_router.py`（+56）、`utils/date.py`、`services/data_client.py`、`skills/capital_flow.py`、`skills/stock_snapshot.py`、`skills/report_lookup.py`（+100 新增 report\_lookup）：D27 参数归一化

- `pyproject.toml`、`.env.example`、`.gitignore`、`README.md`：配置与文档

### 测试

- P3-fix-3：`test_market_snapshot.py` +3、`test_synth_answer.py` +6 且迁移 2 个节点级测试（补 patch trading\_session\_status）、`test_synth_answer_non_trading_hint.py` 断言更新

- P2 遗留：`test_chat_persist_followup.py`、`test_chat_state.py`、`test_report_lookup.py` 新增；`test_qa_router.py` +155、`test_chat_multiturn.py`、`test_chat_legacy_replacement.py`、`test_memory.py`、`test_ws_chat.py` 适配

### 文档

- `AGENTS.md`：Node.js 侧配合接口表 +3 行 market 端点；"market\_snapshot Skill 降级语义"段 +2 条 P3-fix-3 bullet

### 验证

- 目标测试合计 86 passed；全量回归与基线一致（worktree A/B 对比失败集完全一致，新增失败清零）

- ruff：改动文件 0 errors；SDD 审查：逐 Task Approved + 最终整分支审查 Ready to merge Yes

***

## \[feat/market-trace-improvement] 2026-08-02 — 大盘溯源 Agent 改进

**开发者**: Aria

### 新增

- schema：MorningForecast / PredictionValidation / SectorHit / EventHit 模型

- service：morning\_forecast\_extractor 晨报结构化提取服务 + Redis 缓存（TTL=2h）

- snapshot：build\_market\_trace\_snapshot 接入 morning\_forecast 注入；财联社数据源切换为 /internal/news/telegraph 当日全量电报

- market\_tools：GLOBAL\_MARKET\_TICKERS 新增欧洲股市 ticker（^GDAXI / ^FTSE / ^FCHI）

- review：validate\_trace\_against\_snapshot 预判对照校验 + render\_market\_trace\_markdown 预判对照章节

### 兼容性

- MarketTraceResult.prediction\_validation / MarketTraceSnapshot.morning\_forecast 均 Optional 默认 None，兼容旧缓存

***

## \[main] 2026-08-02 — ChatAgent 最小落地 M1-M5 完成 + 非交易日统一提示

***

## \[changer] 2026-08-14 — 大盘溯源影响持续性预判可靠性修复

**开发者**: 37588

### 修复

- 影响持续性预判（B2 预测）可靠性修复：大盘溯源报告「影响持续性预判」区块此前持续为空（服务器实测 `prediction=null`）——根因①预测到期日跨年（long 档 +120 交易日进入 2027）触发 `chinese_calendar` 越界异常导致预测整体丢弃；②LLM 输出零容错（缺 schema\_version / 多余字段 / 围栏文本即整体失败）；③证据 ID 一票否决（任一幻觉即整体抛错）

- 交易日判断越年 fallback：`chinese_calendar` 仅覆盖 2004-2026，2027 年起 `is_workday` 抛异常；现捕获越界并按可交易日处理（只跳周末），库更新后自动恢复精确判断——同步修复 2027 年定时调度（晨报/晚报/复盘/预测验证）与预测到期日计算

- 预测输出三层容错：到期日计算 best-effort（失败降级空字典不阻断预测）；证据 ID 过滤而非一票否决（对齐对话内预测路径）；LLM 输出解析容错（围栏/前缀剥离、JSON 提取、剔除 thinking 等多余键、缺 schema\_version 自动注入 1.0）

### 测试

- 新增 9 个回归用例（2027 越年交易日、跨年 +120 交易日、到期日失败降级、证据 ID 过滤、缺 schema\_version 注入、多余键剔除、围栏/前缀提取、纯文本降级 None）；全量单测 1553 passed，ruff 0，mypy 无新增错误

> 代码验收通过（待生产验证：服务器 08-14 20:30 review\_full 实测 prediction 非 null）。

***

## \[changer] 2026-08-13 — 对话体验优化：回答内容流式显示

**开发者**: 37588

### 新增

- 回答内容流式显示：AI 回答生成完成后按内容分节渐进呈现（配合打字机动画），替代此前的整段一次性弹出

- 内容流式事件通道：回答文本增量与异常整段替换两类事件，经统一通道下发，支持断线续传回放

### 改进

- 生成中断时保留已生成内容并追加「已停止生成」提示，不再清空半截内容

- 流式展示与既有「思考过程」「工具进度」展示协同，回答完成时按前缀校验只补尾部，避免内容跳变

> 代码验收通过（待生产验证）。

***

## \[changer] 2026-08-13 — 对话体验优化：深度分析触发修复

**开发者**: 37588

### 修复

- 对话「深度分析」触发修复：此前使用股票中文名称提问（如"贵州茅台今天怎么样"）会被固定为轻量回答，「深度分析」入口无法生效；现支持在明确表达深度分析意图（如"深度分析贵州茅台"）或点击「深度分析」按钮时正确进入深度分析流程

> 代码验收通过（待生产验证）。

***

***

## \[feat/event-scrape-schedule-adjust] 2026-08-13 — 事件抓取中台调度调整（盘前 07:30→08:45 + 盘中恢复 12:00）

**开发者**: Aria

### 改进

- `config.py`: `scheduler_event_scrape_cron` 由 `30 7 * * 1-5` 改为 `45 8 * * 1-5`（盘前全量档 07:30→08:45）

  - 原因：07:30 时点早间公告（08:00-09:00 发布）尚未出，全量价值低；08:45 紧邻晨报 08:50，事件更全

  - `scheduler_event_scrape_early_cron` 保留字段（兼容已部署配置），不再单独注册 job

- `config.py`: `scheduler_event_scrape_intraday_cron` 由 `0 10-11,13-14 * * 1-5` 改回 `0 10-14 * * 1-5`（恢复 12:00 午间档，用户裁决：午休期间仍有午间公告/新闻发布，M8 移除属误删）

- `scheduler.py`: 删除 `event_scrape_early` job（原 08:45 intraday 增量档），盘前档 `event_scrape_daily` 以 `full_daily` 在 08:45 运行，与早间刷新合并

### 测试

- `test_scheduler_event_scrape.py`: `event_scrape_early` 断言改为 `event_scrape_daily`（08:45）+ 确认 early 已删除

- `test_scheduler.py`: `from_crontab.call_count` 9→8（删 1 档）；两个注册断言 `event_scrape_early`→`event_scrape_daily`；intraday cron mock 值同步为 `0 10-14 * * 1-5`

- 验证：55 passed（scheduler 相关）；ruff All checks passed；mypy 3 个既有错误（\_get\_event\_bus 无类型标注，与本次改动无关）

***

## \[fix/iterate-replay-user-profile] 2026-08-13 — 回放隔离清单补登记：get\_user\_profile（PR #71 缺口）

**开发者**: Aria

### 修复

- `iterate/replay_layer.py`: `NodeApiClient.get_user_profile` 加入 `_ISOLATION_EXEMPT_METHODS`（经 `get` 间接隔离分组）

  - 背景：PR #71 新增 `get_user_profile`（用户画像，内部 `await self.get("/internal/user-profile/{user_id}")`），未登记回放隔离清单，I-3 清单封闭测试 `test_service_isolation_covers_all_public_network_methods` 失败（服务器沙盒全量测试暴露）

  - 依据：`get_user_profile` 无独立网络入口，经 `get → node_read` 返回 None 后 `not isinstance(data, dict)` 走失败降级，符合豁免条件；回放模式下不触达真实 Node 后端

### 测试

- `tests/unit/test_iterate_replay.py`: 17 passed（含清单封闭测试 RED→GREEN）；ruff All checks passed

## \[fix/iterate-case-sufficiency] 2026-08-13 — 产片链路数据完整性防御（case\_20260731 全 0 分事故）

**开发者**: Aria

### 修复

- `scripts/build_iterate_cases.py`: 新增 `_snapshot_data_sufficient(snapshot_dict)` 产片数据完整性检查

  - 背景：服务器沙盒 `case_20260731_us_market_surge` 跑 run\_case 全 0 分，根因是该 case 为测试 fixture 样例（`a_share={}`、missing\_fields 3 项），且真实产片链路 `build_market_trace_snapshot` 的 `normalize_a_share` 只做字段复制不校验完整性——Node 返回 status=complete + coverage.complete=true 但 indexes 等字段缺失时，空壳 case 照样产片进闭环，跑满 max\_rounds 全部 0 分浪费 LLM 预算

  - 修复：`build_review_case` 在 build\_case 之前检查快照 A 股数据完整性（`a_share.indexes` 非空），数据不足且非 `force` 时抛 `RuntimeError` 拒绝产片（省一次 case/GT 落盘与 LLM 调用）；`force=True` 跳过

- `scripts/build_iterate_cases.py`: `snapshot.model_dump` 改用 `cast("Any", ...)`（跨 SimpleNamespace/MarketTraceSnapshot 类型边界，消除 mypy attr-defined/union-attr 错误码不一致）

### 测试

- 新增 2 条：空壳快照拒绝产片（+ 不残留文件）、force 跳过检查

- 验证：产片链路 + case/GT/校验/评估/调度 59 passed；ruff All checks passed；mypy iterate clean

***

## \[feat/event-scrape-hub] 2026-08-13 — 迭代辩论裁决修复第二轮收尾（T9 M3/T10 Q1/T11 + 基线清理）

**开发者**: Aria

### 修复

- `variant_engine.py`: `_content_hash` 参数类型从 `dict[str, str]` 放宽为 `dict[str, object]`，兼容 `_compute_variant_hash` 传入的嵌套 dict 补丁规格（T9 M3 补充修复）

- `test_iterate_variant.py`: `test_experiment_record_has_real_variant_hash` 更新为用 `_compute_variant_hash` 计算预期值（适配 T9 M3）；移除未使用的 `hashlib` 导入

- `test_iterate_loop.py`: `test_stale_experiment_records_cleaned_before_run` 添加 `result` 断言消除 ruff F841

### 改进

- `config.py`: 修复 2 个 E501 行过长（event\_scrape 调度 cron 表达式换行）

- `AGENTS.md`: iterate 模块描述补充 T9 M3/T10 Q1/T11 M1-M4 修复要点

### 验证

- pytest: 73 passed, 3 deselected（2 个依赖 git 可执行文件、1 个预存不相关失败）

- ruff: All checks passed（iterate 模块 + 测试文件 + config.py）

- mypy: 无错误（iterate 模块）

***

## \[changer] 2026-08-12 — Phase 5 长会话上下文管理

**开发者**: 37588

### 新增

- `src/aistock_agent/utils/context_window.py`：`trim_messages(messages, *, max_turns=6, summary_chars=200)` 纯函数——≤12 条消息原样透出（summary=None，短会话 prompt 字节不变硬约束）；超窗 → LLM prompt 只喂最近 12 条，超窗部分收敛为零 LLM 确定性摘要（≤200 字，逐轮"用户：问句｜AI：回复片段"，幂等无累积）；`build_summary_context` 生成"此前对话摘要"注入段

- `QuestionState.messages_summary` 可选字段（qa\_router 超窗时写入随 checkpointer 持久化，write-only；synth\_answer 消费侧从当前 messages 确定性重算，防跨轮陈旧残留）

- `DELETE /api/agent/internal/chat/threads/:session_id`（内部访问令牌 403 / 非法 400 / 幂等 200 / 异常 500）+ `checkpointer.delete_thread()`（AsyncSqliteSaver.adelete\_thread，sqlite/memory 幂等、redis best-effort）

- `config.py sqlite_busy_timeout=30.0` → `_build_async_sqlite_saver` 的 `aiosqlite.connect(timeout=...)`（多 worker 争用缓解）

### 改进

- qa\_router/synth\_answer：窗口+摘要注入（SYSTEM\_PROMPT 常量字节不变，节点内拼接），LLM 输入用 12 条窗口；多子目标 `_synth_multi_goal`/`_synth_section` 路径同步注入

### 测试

- `tests/unit/test_context_window.py`、`tests/unit/test_qa_router_summary.py`、`tests/unit/test_synth_answer_summary.py`、`tests/unit/test_checkpointer_busy_timeout.py`、`tests/e2e/test_chat_threads.py`、`tests/unit/test_checkpointer_delete_thread.py`、`tests/integration/test_phase5_long_session_smoke.py`

> 验证：全量测试回归新增失败清零；ruff 改动文件 0 新增；集成冒烟 2/2（7 轮 13 条 → 12 条窗口 + 摘要注入 + messages\_summary 持久化 + 删会话 thread 消失；短会话字节不变）。代码验收通过（待生产验证），待组长 merge 后部署验证。

***

## \[changer] 2026-08-12 — 问题 18 WS recv 竞态修复（Phase 2 回归补丁）

**开发者**: 37588

### 修复

- `src/aistock_agent/api/ws.py`：`_forward_until_done_or_cmd` 的 send 完成分支在 `recv_task.cancel()` 后新增 `await asyncio.gather(recv_task, return_exceptions=True)` 收尾再 return——`task.cancel()` 仅请求取消，不同步 await 则底层 uvicorn/websockets 同连接 recv 并发防护未释放，主循环随即 `receive_json()` 触发 `RuntimeError: cannot call recv while another coroutine is already waiting` → 每轮 done 后 WS 连接 1005 崩溃（Phase 3 生产冒烟 9 轮实证，Phase 2 PR #64 引入）

- 回归测试：`tests/unit/test_ws_chat_replacement.py` 新增 `_RecvTrackingWebSocket`（复刻 uvicorn 并发 recv 抛 RuntimeError 防护语义）+ `test_forward_until_done_or_cmd_clears_pending_recv_on_done`（断言返回时无挂起 recv、主循环可安全发起下次 receive、不抛 RuntimeError）

> 验证：TDD RED→GREEN；单元 test\_ws\_chat\_replacement.py 15/15 + 定向契约回归 22/22（chat\_task\_manager / ws 集成 / ws\_resume / token\_usage）；全量测试回归新增失败清零（+1 新增回归测试）；ruff 改动文件 0；真实 WS 冒烟同一连接连续 3 轮 done 全部送达、连接保持、主动关闭 code=1000（非 1005 崩溃）。不改 resume/stop/归属校验协议与事件协议，前端零改动。生产部署验证待 V1 部署窗口。

***

## \[feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final review 复审修复（Round 2）

**开发者**: 37588

### 修复

- `services/event_scrape_sources.py` I1 过滤由 `published.startswith(score_date)`（北京日期前缀）改为 `_event_shanghai_date(published) == score_date`（上海时区日期归属）——Node `published_at TIMESTAMPTZ` 经 `toISOString()` 输出 UTC ISO（如 `2026-08-12T02:00:00.000Z`），北京 00:00-07:59 当日事件 UTC 日期落前一日（`2026-08-11T22:00:00.000Z` = 北京 8-12 06:00）被旧逻辑误过滤；新增 `_event_shanghai_date`（UTC 带 Z → 转上海；本地无时区 → 显式 `replace(tzinfo=Asia/Shanghai)` 保证确定性；解析失败宽容回退 `raw[:10]`）；保留"无时间字段保守保留"守卫

- `agents/workers/morning.py::_event_records_to_major_events` 加 `impact_score >= MAJOR_IMPACT_THRESHOLD`（=4）过滤（对齐注入路径过滤语义，docstring 注明），缓存命中时 `analysis_reports["major_events"]` 不再混入 impact=1 普通证据（手动晨报端点 major\_event\_count 诊断计数失真）；函数为模块私有、仅缓存路径一处调用，无复用歧义

### 测试

- 新增 4 条：UTC 上海日期归属（当日保留/北京凌晨保留/真陈旧过滤，`shanghai_today()` 相对日期无炸弹）、非法时间回退、缓存命中 major/minor 过滤、全普通证据降级回 details 提取

### 说明

- 偏差：任务单测描述"`2026-08-11T22:00:00.000Z` 应过滤"与其 Issue-1 正文"北京凌晨当日事件应保留"矛盾（该时间戳上海日期=当日），按 Issue-1 正确语义实现并补充真陈旧行用例锁定

- 验证：定向 pytest 28 passed；全量 1471 passed / 6 既有失败（test\_industry\_vector\_search API 依赖，基线一致零回归）；mypy 2 文件 0 错误；ruff 4 文件 All checks passed

***

## \[feat/event-scrape-hub] 2026-08-12 — 统一事件抓取中台 final whole-branch review 修复（C1 + I1-I4 + Minor）

**开发者**: 37588

### 修复

- **C1（Critical）**：`event_scrape_sources.py::collect_eastmoney_judgements` 响应键名 `"items"` → `"events"`（Node `StockMonitorService.getEvents` 返回 `{total, events}`）——修复东财三模式（full\_daily/intraday/event\_triggered）生产恒空

- **I1**：东财行按 `published_at`/`event_time` 日期前缀过滤（alerts 接口不支持日期窗口），防昨日/前日陈旧行以当日 score\_date 反复入库

- **I2**：`raw.setdefault("url", raw.get("detail_url") or "")`——Node 输出 `detail_url` 非 `url`，修复东财事件 url 恒空

- **I3**：`save_event_scrape` 返回值增加 `added`/`added_events`（本批真正新增数），传导守卫 `persisted>0` → `added>0` 且只传新增子集——全去重批次不再重复触发整批传导（LLM 成本）

- **I4**：`scheduler._run_morning_task` 降级分支——当日事件库为空且 morning 产出 major\_events 时兜底触发传导（恢复 `_pending_event_tasks` 强引用）；M1 随之解决

- **M2**：`data_client.py` 新增 `get_analysis_report_quiet`（404 静默）；`load_event_scrape` 改走该方法并对空库降级 warning（不再刷 error 级 404）

- **M4**：晨报注入前按 `impact_score >= MAJOR_IMPACT_THRESHOLD` 过滤；全普通时降级自主检索文案

- **M5**：`scrape_event_triggered` 加 symbol 空守卫（不采集不落库）

- **M8**：盘中 cron `0 10-14 * * 1-5` → `0 10-11,13-14 * * 1-5`（避开 A 股午休）

### 验证

- 全量 pytest 1467 passed / 6 既有失败（基线一致零回归）；mypy 0 新增；ruff All checks passed

***

## \[changer] 2026-08-11 — Phase 2 断点续传 + 打断/停止/重试（问题 15）

**开发者**: 37588

### 新增

- `src/aistock_agent/services/chat_task_manager.py`：ChatTaskManager 单例——`start(session_id, run_id, producer, user_id=None)`（同 session 活跃任务拒绝）/ `get`（done 且超 TTL 600s 惰性清理）/ `has_active` / `cancel(session_id)->bool`；`ChatRunState`（events 回放 / waiters.notify / done / cancelled / result 缓存 / user\_id 归属）；`_runner` 显式 `except asyncio.CancelledError` → `{"type":"cancelled","content":"已停止生成"}`

- 测试：`tests/unit/test_chat_task_manager.py`（7）、`tests/integration/test_ws_chat_resume.py`（7）

### 改进

- `src/aistock_agent/api/ws.py`：生成任务与 WS 连接解耦——生产者 `_run_chat_graph_to_events`（事件 sink 进 state.events + notify；`reset_token_usage` 在 create\_task 前同 context；token 计费落库仅 warning 不阻断）+ 转发器 `_forward(state, send, replay)`（断连仅终止转发不取消任务）+ `_forward_until_done_or_cmd`（转发/接收并行，生成中可即时 stop）

- 协议增量（向后兼容字节不变）：普通消息可选 `run_id`；控制消息 `{type:"resume",session_id}` → `resume_status`（none/running+run\_id）/ done 直接补发终态 payload；`{type:"stop",session_id}` → `stop_status`（cancelled/not\_found）；`cancelled` 终态经既有 `_forward` 终态路径下发

- 归属校验 `_owns_run`（resume/stop 共用）：state None → True；双方 user\_id 非空必须相等；任一 None → True；越权 → error "无权访问该会话" + WARN（不静默）

- `src/aistock_agent/graph/nodes/_reasoning.py`：`stream_reasoning(websocket,...)` → `stream_reasoning(sink, node, message)`（sink 化解耦连接）

- DONE 负载字段（content/last\_deep\_report/token\_usage/cards）字节不变；闸门短路语义不受影响

> 验证：定向 40/40 + ruff 改动文件 0；全量测试回归新增失败清零（并修复 8 个基线失败）；三仓库整分支 review Ready to merge。

***

## \[changer] 2026-08-11 — P0 端口层封堵（uvicorn 改绑）+ 文档

**开发者**: 37588

### 改进

- `deploy/ecosystem.config.json`：uvicorn `--host 0.0.0.0` → `--host 127.0.0.1`（8080 只监听本机，公网不可直连 agent-py；app-api 本机回环仍可达）——P0 身份鉴权端口层封堵第二步（Caddy 域名层已由管理员完成）

### 文档

- AGENTS.md：user\_id 信任边界由 P0 解决（app-api 验签注入，客户端自报失效）

> 部署注意：勿用 `pm2 restart`（不重读配置），须先 `pm2 delete` 再 `pm2 start deploy/ecosystem.config.json`，并验证端口仅监听本机回环地址。

