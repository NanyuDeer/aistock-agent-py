"""变体实验引擎 —— 生成/应用/恢复待迭代 agent 的提示词、工作流、数据源变体。

每轮实验流程（由 run_case.py 驱动）：
1. restore_baseline 恢复干净基线（git checkout -- 变体文件）
2. generate_variant 让 LLM 基于当前实现 + 标准答案 + 差距分析生成变体
3. apply_variant 写文件
4. 子进程回放（replay_runner）→ evaluator 评分
5. 实验记录落盘 data/experiments/{case_id}_r{round}.json
"""

import ast
import asyncio
import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Literal, cast

import structlog
from langchain_core.messages import HumanMessage, SystemMessage

from aistock_agent.config import settings
from aistock_agent.iterate.adapters import IterableAgentAdapter
from aistock_agent.iterate.case_builder import get_data_dir
from aistock_agent.iterate.evaluator import ScoreDetail, evaluate_attribution
from aistock_agent.services import llm as llm_service

logger = structlog.get_logger()

VariantType = Literal["prompt_diff", "workflow_diff", "data_source_diff"]

_VALID_VARIANT_TYPES = {"prompt_diff", "workflow_diff", "data_source_diff"}

#: 变体生成时喂给 LLM 的单个目标区域源码上限（符号地图 + 目标块，防止爆上下文）
_MAX_VARIANT_TARGET_CHARS = 6000
#: 每个文件喂给 LLM 的目标区域符号上限（入口优先排序后取前 N 个）
_MAX_VARIANT_TARGET_SYMBOLS = 8
#: 入口候选符号名：主入口函数 / 主 prompt 常量。命中者优先喂入（C1 修复：
#: 真实仓库 run 常排在第 30+ 位（review 33 个符号中第 31、event 27 个中第 27），
#: 固定取前 8 个会漏掉入口，LLM 看不到 run/REVIEW_PROMPT 只能臆造 old_snippet → 补丁必失配）。
_ENTRY_SYMBOL_NAMES: frozenset[str] = frozenset(
    {
        "run",
        "run_review",
        "run_event",
        "REVIEW_PROMPT",
        "EVENT_PROMPT",
        "EVENT_UNDERSTANDING_PROMPT",
        "EVENT_TRANSMISSION_PROMPT",
        "EVENT_HISTORY_PROMPT",
        "EVENT_INVESTMENT_PROMPT",
        "EVENT_PODCAST_PROMPT",
    }
)
# 变体生成输出 token 上限：补丁模式输出 target_symbol/old_snippet/new_snippet，
# 默认 deep_think_max_tokens=4000 会截断，这里按输出体量放大。
_MAX_VARIANT_OUTPUT_TOKENS = 12000

_GENERATE_PROMPT = """你是迭代优化工程师。目标是改进待迭代 Agent 的归因质量。
待迭代文件符号地图（含目标区域源代码，超长已截断并标注）：
{files_with_content}
标准答案归因：{ground_truth}
最近评分：{score}（满分 1.0），差距分析：{gap_analysis}
请基于上述目标区域生成最小变体，输出严格 JSON：
{{
  "type": "prompt_diff|workflow_diff|data_source_diff",
  "files": ["相对仓库根路径的被改文件（必须来自符号地图所在文件）"],
  "target_symbol": "被修改的函数/常量名",
  "old_snippet": "目标区域中被替换的原文片段（必须与给定源码逐字符一致）",
  "new_snippet": "替换后的新片段",
  "instructions": "改动思路一句话"
}}
要求：
- target_symbol 必须存在于符号地图；old_snippet 必须从给定源码原样复制
- files 必须且只能包含 target_symbol 所在文件
- 只改与差距分析相关的部分；禁止引入无关重构
- 若需新增独立函数，target_symbol 用 "__new__" 且 new_snippet 为完整新函数；
  old_snippet 必须省略（空字符串）
只输出 JSON。"""


def _build_symbol_map(file_content: str) -> list[dict[str, int | str]]:
    """ast 提取顶层符号（def/async def/class/模块级赋值名）+ 起始行号。

    供 LLM 定位目标区域（不再要求输出完整文件，C1/C2 修复）。
    """
    symbols: list[dict[str, int | str]] = []
    try:
        tree = ast.parse(file_content)
    except SyntaxError:
        return symbols
    for node in tree.body:
        if isinstance(
            node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef
        ):
            symbols.append({"name": node.name, "line": node.lineno})
        elif isinstance(node, ast.Assign):
            for t in node.targets:
                if isinstance(t, ast.Name):
                    symbols.append({"name": t.id, "line": node.lineno})
    return symbols


def _is_entry_symbol(name: str) -> bool:
    """符号名是否命中入口候选集合（入口优先喂入，保证 LLM 可见主入口源码）。"""
    return name in _ENTRY_SYMBOL_NAMES


def _extract_symbol_source(content: str, symbol: str) -> str | None:
    """按符号返回其定义代码块（从起始行到下一个顶层符号前）。

    返回 None 表示符号不存在（LLM 必须重试，不允许凭空生成）。
    """
    symbols = _build_symbol_map(content)
    match = next((s for s in symbols if s["name"] == symbol), None)
    if match is None:
        return None
    lines = content.splitlines()
    start = int(match["line"]) - 1
    end = len(lines)
    for other in symbols:
        if int(other["line"]) > int(match["line"]):
            end = int(other["line"]) - 1
            break
    return "\n".join(lines[start:end])


def _apply_snippet_patch(original: str, old: str, new: str) -> str | None:
    """把 old 片段替换为 new 片段；先精确匹配，再空白归一化模糊匹配。

    找不到返回 None（调用方不崩闭环，标记该轮为失败轮）。
    """
    if old in original:
        return original.replace(old, new, 1)
    # 模糊：归一化空白后定位，回写原文行（仅替换匹配区间的行）
    norm_old = "".join(old.split())
    lines = original.splitlines()
    for i in range(len(lines)):
        window = "".join(lines[i : i + len(old.splitlines())]).split()
        if "".join(window) == norm_old:
            return "\n".join(lines[:i] + [new] + lines[i + len(old.splitlines()) :])
    return None


@dataclass
class VariantPlan:
    type: VariantType
    files: list[str]
    instructions: str
    new_content: dict[str, str] = field(default_factory=dict)
    target_symbol: str = ""
    old_snippet: str = ""
    new_snippet: str = ""


async def generate_variant(
    adapter: IterableAgentAdapter,
    case: dict[str, object],
    ground_truth: dict[str, object],
    current_score: ScoreDetail | None,
    gap_analysis: str,
    repo_root: Path,
) -> VariantPlan:
    """LLM 基于符号地图 + 目标区域生成变体（目标区域补丁，非完整文件）。

    LLM 注入沿用 evaluator 的已验证模式：模块级 import + get_deep_think()，
    避免 from-import 的绑定陷阱（模块内部状态变更时旧引用失效）。
    补丁输出体量小但 old_snippet 需逐字符复制原文，仍显式加大 max_tokens
    并关闭思考（生产走本地代理参数可能被剥离，大 token 兜底）。
    """
    prompt = _GENERATE_PROMPT.format(
        files_with_content=_target_regions(adapter, repo_root),
        ground_truth=json.dumps(ground_truth.get("attribution", {}), ensure_ascii=False),
        score=current_score.total if current_score else "N/A",
        gap_analysis=gap_analysis,
    )
    llm = llm_service.get_deep_think(
        max_tokens=_MAX_VARIANT_OUTPUT_TOKENS,
        # 只保留 thinking disabled（2026-08-13 实测：单独传 thinking disabled
        # 成功；加 reasoning_effort 会撞上游模型与 new-api 代理的合法值不一致
        # ——代理接受 minimal，deepseek 上游只接受 low/medium/high/xhigh/max，
        # 且行为不稳定（round 2 400 / round 3 成功），移除该参数彻底规避）。
        extra_body={"thinking": {"type": "disabled"}},
    )
    resp = await llm.ainvoke(
        [SystemMessage(content=prompt), HumanMessage(content=str(case["event_title"]))]
    )
    parsed = _parse_json(str(resp.content))
    target = str(parsed.get("target_symbol", ""))
    old_snippet = str(parsed.get("old_snippet", ""))
    new_snippet = str(parsed.get("new_snippet", ""))
    raw_type = str(parsed.get("type", "prompt_diff"))
    if raw_type not in _VALID_VARIANT_TYPES:
        raw_type = "prompt_diff"
    raw_files = parsed.get("files")
    if isinstance(raw_files, list):
        plan_files = [str(f) for f in raw_files]
    else:
        plan_files = list(adapter.prompt_files) + list(adapter.workflow_files)
    return VariantPlan(
        type=cast("VariantType", raw_type),
        files=plan_files,
        instructions=str(parsed.get("instructions", "")),
        new_content={},  # 目标区域补丁模式：不写 full new_content
        target_symbol=target,
        old_snippet=old_snippet,
        new_snippet=new_snippet,
    )


def apply_variant(variant: VariantPlan, repo_root: Path) -> list[Path]:
    """把变体写盘（目标区域补丁或完整文件），返回实际改动文件列表。

    路径安全：rel 经 resolve 后必须仍在 repo_root 内，否则抛 ValueError。
    __new__ 模式：新增独立函数，追加到第一个存在的目标文件末尾（I1 修复）。
    目标区域补丁：读原文 → _apply_snippet_patch（失败不崩，返回空列表）。
    完整文件模式（legacy new_content）：直接覆盖写，保持既有调用兼容。
    """
    root = repo_root.resolve()
    written: list[Path] = []
    if variant.target_symbol == "__new__":
        # I1 修复：LLM 按约定给 old_snippet=""（不落入补丁模式双守卫），
        # 把 new_snippet 完整新函数追加到 variant.files 中第一个存在的文件末尾。
        for rel in variant.files:
            path = (root / rel.lstrip("/")).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"variant path escapes repo root: {rel}")
            if not path.exists():
                logger.warning("iterate_variant_file_missing", file=str(path))
                continue
            original = path.read_text(encoding="utf-8")
            if not original.endswith("\n"):
                original += "\n"
            path.write_text(original + variant.new_snippet, encoding="utf-8")
            written.append(path)
            logger.info(
                "iterate_variant_applied",
                file=str(path),
                target=variant.target_symbol,
            )
            break  # 仅追加到第一个存在的文件
        return written
    if variant.old_snippet and variant.new_snippet:
        # 目标区域补丁模式：需读原文做 search/replace，目标文件必须已存在
        for rel in variant.files:
            path = (root / rel.lstrip("/")).resolve()
            if not path.is_relative_to(root):
                raise ValueError(f"variant path escapes repo root: {rel}")
            if not path.exists():
                logger.warning("iterate_variant_file_missing", file=str(path))
                continue
            original = path.read_text(encoding="utf-8")
            patched = _apply_snippet_patch(
                original, variant.old_snippet, variant.new_snippet
            )
            if patched is None:
                logger.warning(
                    "iterate_variant_patch_mismatch",
                    file=str(path),
                    target=variant.target_symbol,
                )
                continue  # 补丁不匹配 → 本轮不写盘（失败轮），不崩闭环
            path.write_text(patched, encoding="utf-8")
            written.append(path)
            logger.info(
                "iterate_variant_applied",
                file=str(path),
                target=variant.target_symbol,
            )
        return written
    for rel, content in variant.new_content.items():
        path = (root / rel.lstrip("/")).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"variant path escapes repo root: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
        logger.info("iterate_variant_applied", file=str(path))
    return written


def _truncate_region(src: str, limit: int, start_line: int) -> tuple[str, bool]:
    """按行截断超长目标区域：保留开头（含 def 签名）到截断点，标注未展示行号。

    返回 (截断后的源码, 是否发生截断)。start_line 为符号在文件中的起始行号（1 起）。
    """
    if len(src) <= limit:
        return src, False
    lines = src.splitlines()
    kept: list[str] = []
    size = 0
    for ln in lines:
        if size + len(ln) + 1 > limit:
            break
        kept.append(ln)
        size += len(ln) + 1
    if not kept:  # 首行自身超长（极端）：至少保留签名行，保证 LLM 看到入口定义
        kept = [lines[0]]
    hidden_start = start_line + len(kept)
    hidden_end = start_line + len(lines) - 1
    return (
        "\n".join(kept) + f"\n...（已截断，后续行号 {hidden_start}-{hidden_end} 未展示）",
        True,
    )


def _target_regions(adapter: IterableAgentAdapter, repo_root: Path) -> str:
    """为 adapter 声明的每个文件生成符号地图 + 目标区域源代码。

    符号选择入口优先：命中 _ENTRY_SYMBOL_NAMES 的符号排最前，其余按行号升序，
    取前 _MAX_VARIANT_TARGET_SYMBOLS 个（C1 修复：run 排第 30+ 位仍被喂入）；
    超长块按行截断并标注而非整块丢弃，保证 LLM 至少看到入口签名与开头逻辑。
    """
    root = repo_root.resolve()
    blocks: list[str] = []
    for rel in list(adapter.prompt_files) + list(adapter.workflow_files):
        path = (root / rel.lstrip("/")).resolve()
        if not path.is_relative_to(root) or not path.exists():
            blocks.append(f"{rel}:\n（文件不存在或不可读）")
            continue
        content = path.read_text(encoding="utf-8")
        symbols = _build_symbol_map(content)
        # 入口优先 + 行号兜底排序（稳定排序，其余符号保持原行号顺序）
        selected = sorted(
            symbols,
            key=lambda s: (0 if _is_entry_symbol(str(s["name"])) else 1, int(s["line"])),
        )[:_MAX_VARIANT_TARGET_SYMBOLS]
        regions: list[str] = []
        for s in selected:
            src = _extract_symbol_source(content, str(s["name"]))
            if src is None:
                continue
            src, _ = _truncate_region(src, _MAX_VARIANT_TARGET_CHARS, int(s["line"]))
            regions.append(f"### {s['name']} (line {s['line']})\n{src}")
        block = f"{rel}:\n符号地图: {[str(s['name']) for s in symbols]}\n\n"
        block += "\n\n".join(regions)
        blocks.append(block)
    return "\n\n---\n\n".join(blocks)


def restore_baseline(
    adapter: IterableAgentAdapter,
    repo_root: Path,
    extra_files: tuple[str, ...] = (),
) -> None:
    """git checkout -- 恢复 adapter 声明的提示词/工作流文件到基线。

    extra_files：上一轮 apply_variant 实际写过的相对路径（如 data_source_diff
    改动 tools/config 文件，不在 adapter 声明内），一并恢复，防止跨轮残留。
    非 git 目录（如测试 tmp_path）git checkout 失败仅告警，不阻塞。
    """
    files = list(adapter.prompt_files) + list(adapter.workflow_files) + list(extra_files)
    if not files:
        return
    result = subprocess.run(
        ["git", "checkout", "--", *files],
        cwd=repo_root,
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        logger.warning("iterate_restore_baseline_failed", stderr=result.stderr.strip())


async def run_experiment_round(
    agent_id: str,
    case: dict[str, object],
    round_no: int,
    variant: VariantPlan,
    ground_truth: dict[str, object],
) -> dict[str, object]:
    """应用变体 → 子进程回放 → 评分 → 落盘实验记录。

    返回 {score, score_detail, gap_analysis, agent_output, variant_hash}。
    """
    case_id = str(case["case_id"])
    variant_hash = _compute_variant_hash(variant)
    output = await _run_replay_subprocess(agent_id, case_id, variant_hash)
    # 2026-08-13：回放偶发失败（超时/子进程失败/agent 降级输出）自动重试一次——
    # LLM 波动容忍。变体轮 0 分事故根因：同变体两次回放一次成功一次超时/降级，
    # 失败轮直接 0 分浪费迭代预算。重试有界（仅 1 次），仍失败才记失败轮。
    if _needs_replay_retry(output):
        logger.warning(
            "iterate_replay_retry_once",
            agent_id=agent_id,
            case_id=case_id,
            timed_out=bool(output.get("timed_out")),
            subprocess_failed=bool(output.get("subprocess_failed")),
            response_len=len(str(output.get("final_response", ""))),
        )
        output = await _run_replay_subprocess(agent_id, case_id, variant_hash)
    if output.get("timed_out") or output.get("subprocess_failed"):
        # 回放超时/子进程失败：不调用评估 LLM（无输出可评），记为失败轮。
        score = ScoreDetail(
            0.0,
            0.0,
            0.0,
            0.0,
            gap_analysis=(
                f"回放子进程{'超时' if output.get('timed_out') else '失败'}"
                f"（>{settings.iterate_round_timeout_seconds}s），本轮视为失败"
            ),
        )
    else:
        structured = output.get("structured")
        score = await evaluate_attribution(
            str(output.get("final_response", "")),
            ground_truth,
            agent_structured=structured if isinstance(structured, dict) else None,
        )
    record: dict[str, object] = {
        "case_id": case_id,
        "round": round_no,
        "agent_id": agent_id,
        "variant": {
            "type": variant.type,
            "files": variant.files,
            "instructions": variant.instructions,
        },
        # C8/N2 修复：补丁规格落盘，负责人可复现合入（不再只有一句话 instructions）
        "patch": {
            "target_symbol": variant.target_symbol,
            "old_snippet": variant.old_snippet,
            "new_snippet": variant.new_snippet,
        },
        # C-5 修复：agent 输出全文落盘——评分可完全重算（REPRODUCIBLE），
        # 报告/复盘无需重跑回放即可查看实际输出。
        "agent_output": str(output.get("final_response", "")),
        "score": score.total,
        "score_detail": {
            "direction": score.direction,
            "drivers": score.drivers,
            "sectors": score.sectors,
        },
        "gap_analysis": score.gap_analysis,
        "duration_ms": 0,
        "variant_hash": variant_hash,
        "created_at": _now_iso_date(),
        "is_failure": bool(output.get("timed_out") or output.get("subprocess_failed")),
    }
    path = get_data_dir() / "experiments" / f"{case_id}_r{round_no}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_experiment_recorded", case_id=case_id, round=round_no, score=score.total)
    return {**record, "score_detail_obj": score}


#: 回放结果视为"可重试的偶发失败"的最小输出长度阈值（字符）。
#: agent 降级文本（如 review 的"收盘溯源生成暂时不可用，请稍后重试"）与
#: 空输出均 < 30 字符，正常归因输出远大于此；通用判断不依赖具体 agent 文本。
_REPLAY_RETRY_MIN_OUTPUT_LEN = 30


def _needs_replay_retry(output: dict[str, object]) -> bool:
    """回放结果是否值得重试一次：超时/子进程失败/输出降级或过短（LLM 波动）。"""
    if output.get("timed_out") or output.get("subprocess_failed"):
        return True
    response = str(output.get("final_response", "")).strip()
    return len(response) < _REPLAY_RETRY_MIN_OUTPUT_LEN


#: 回放子进程 env 前缀白名单（C-2，2026-08-14）：只传递运行必需的配置，
#: 其余环境变量（含未知密钥）不流入子进程，缩小泄漏面。
_SUBPROCESS_ENV_PREFIX_ALLOW: tuple[str, ...] = (
    "OPENAI_",
    "DEEP_THINK_",
    "QUICK_THINK_",
    "REDIS_",
    "TAVILY_",
    "ITERATE_",
    "REPLAY_",
    "AISTOCK_",
    "NODE_",
    "STOCK_TRACE_",
    "INSIGHT_",
    "DOUYIN_",
    "LANGSMITH_",
    "QQ_SMTP_",
    "HTTP_TIMEOUT_",
    "LOG_LEVEL_",
)
_SUBPROCESS_ENV_EXACT_ALLOW: frozenset[str] = frozenset(
    {
        "PYTHONPATH",
        "APP_ENV",
        "HTTP_TIMEOUT_SECONDS",
        "LOG_LEVEL",
        "INTERNAL_API_TOKEN",
        "HOST",
        "PORT",
        "CORS_ORIGINS",
        "SCHEDULER_ENABLED",
        "LLM_BASE_URL",
    }
)

_SECRET_VALUE_RE = re.compile(
    r"(?i)\b(api[_-]?key|token|password|auth|secret)\b[=:]\s*([^\s,;\"']+)"
)


def _mask_secrets(text: str) -> str:
    """日志/错误文本中的密钥值掩码（C-2：密钥掩码防泄漏）。"""
    return _SECRET_VALUE_RE.sub(lambda m: f"{m.group(1)}=***", text)


def _build_replay_env(src_dir: str) -> dict[str, str]:
    """回放子进程 env：白名单过滤 + 必需键注入（C-2，2026-08-14）。

    REPLAY_CASE_ID/REPLAY_AGENT 由调用方（_run_replay_subprocess 的参数）注入，
    不以 os.environ 为准（父进程可能残留上次回放的旧值）。
    """
    allowed = {
        k: v
        for k, v in os.environ.items()
        if k in _SUBPROCESS_ENV_EXACT_ALLOW
        or k.startswith(_SUBPROCESS_ENV_PREFIX_ALLOW)
    }
    allowed["PYTHONPATH"] = src_dir
    return allowed


async def _run_replay_subprocess(
    agent_id: str, case_id: str, variant_hash: str
) -> dict[str, object]:
    # 继承父进程环境；PYTHONPATH 指向 src/（src 布局下 python -m aistock_agent.* 需 src 在路径）。
    # 修正：brief 原写 parent.parent（解析为 src/aistock_agent，无法导入 aistock_agent 包），
    # 改为 parent.parent.parent 即 src 目录。
    src_dir = str(Path(__file__).resolve().parent.parent.parent)
    env = _build_replay_env(src_dir)
    # REPLAY_* 以函数参数为权威（C-2：不依赖父进程残留 env）
    env["REPLAY_CASE_ID"] = case_id
    env["REPLAY_AGENT"] = agent_id
    proc: asyncio.subprocess.Process | None = None
    try:
        # C-4 修复：回放改异步子进程（asyncio.create_subprocess_exec）——
        # 裁决书 C 论题：同步 subprocess.run(timeout=600s) 会阻塞主服务事件循环；
        # 17:00 asyncio job 内同步等待使整个调度器停摆。超时用 wait_for 包裹。
        proc = await asyncio.create_subprocess_exec(
            sys.executable,
            "-m",
            "aistock_agent.iterate.replay_runner",
            agent_id,
            case_id,
            variant_hash,
            env=env,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_b, stderr_b = await asyncio.wait_for(
            proc.communicate(),
            timeout=settings.iterate_round_timeout_seconds,
        )
        stdout = stdout_b.decode("utf-8", errors="replace") if stdout_b else ""
        stderr = stderr_b.decode("utf-8", errors="replace") if stderr_b else ""
    except TimeoutError:
        # 超时不崩整个闭环：终止子进程，返回 timed_out 标记，调用侧记为超时失败轮。
        if proc is not None and proc.returncode is None:
            proc.kill()
            await proc.wait()
        logger.warning(
            "iterate_replay_timed_out",
            agent_id=agent_id,
            case_id=case_id,
            timeout=settings.iterate_round_timeout_seconds,
        )
        return {
            "agent_id": agent_id,
            "case_id": case_id,
            "variant_hash": variant_hash,
            "final_response": "",
            "timed_out": True,
        }
    if proc.returncode != 0:
        # C11 修复：子进程失败不再抛 RuntimeError 崩整个闭环，返回失败标记，
        # 由 run_case 计为失败轮（评分 0 + gap 注明），连续失败达阈值再中止。
        logger.warning(
            "iterate_replay_subprocess_failed",
            agent_id=agent_id,
            case_id=case_id,
            stderr=stderr[-300:],
        )
        return {
            "agent_id": agent_id,
            "case_id": case_id,
            "variant_hash": variant_hash,
            "final_response": "",
            "subprocess_failed": True,
        }
    lines = [ln for ln in stdout.strip().splitlines() if ln.strip()]
    try:
        # json.loads 返回 Any，mypy strict 的 no-any-return 要求显式 cast
        return cast("dict[str, object]", json.loads(lines[-1]))
    except (IndexError, json.JSONDecodeError):
        raise RuntimeError(f"replay subprocess bad output: {stdout[-500:]}") from None


def _parse_json(text: str) -> dict[str, object]:
    import re

    match = re.search(r"\{.*\}", text, re.DOTALL)
    raw = match.group(0) if match else text
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except json.JSONDecodeError:
        logger.warning("iterate_variant_llm_invalid_json", snippet=raw[:200])
        return {}


def _content_hash(new_content: dict[str, object]) -> str:
    """变体内容的真实 sha256（json.dumps sorted keys），替代伪造的 git_commit。

    参数类型为 dict[str, object]：_compute_variant_hash 传入含嵌套 dict 的
    补丁规格（new_content + target_symbol/old_snippet/new_snippet），
    json.dumps 可序列化任意对象，无需限制为 dict[str, str]。
    """
    import hashlib

    payload = json.dumps(new_content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _compute_variant_hash(variant: VariantPlan) -> str:
    """计算变体的唯一 hash（T9 M3 修复）。

    修复前：_content_hash(variant.new_content) 在补丁模式下恒为 sha256("{}")，
    因为 new_content={}（目标区域补丁模式不写 full new_content）。
    修复后：hash 包含 new_content + target_symbol/old_snippet/new_snippet，
    不同补丁产生不同 hash，使 variant_hash 可参与去重/选择。
    """
    return _content_hash({
        "new_content": variant.new_content,
        "target_symbol": variant.target_symbol,
        "old_snippet": variant.old_snippet,
        "new_snippet": variant.new_snippet,
    })


def _now_iso_date() -> str:
    """实验记录 created_at：ISO 日期（YYYY-MM-DD，本地时区），供报告按日过滤。"""
    return date.today().isoformat()
