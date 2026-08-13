"""变体实验引擎 —— 生成/应用/恢复待迭代 agent 的提示词、工作流、数据源变体。

每轮实验流程（由 run_case.py 驱动）：
1. restore_baseline 恢复干净基线（git checkout -- 变体文件）
2. generate_variant 让 LLM 基于当前实现 + 标准答案 + 差距分析生成变体
3. apply_variant 写文件
4. 子进程回放（replay_runner）→ evaluator 评分
5. 实验记录落盘 data/experiments/{case_id}_r{round}.json
"""

import ast
import json
import os
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
  "target_symbol": "被修改的函数/常量名",
  "old_snippet": "目标区域中被替换的原文片段（必须与给定源码逐字符一致）",
  "new_snippet": "替换后的新片段",
  "instructions": "改动思路一句话"
}}
要求：
- target_symbol 必须存在于符号地图；old_snippet 必须从给定源码原样复制
- 只改与差距分析相关的部分；禁止引入无关重构
- 若需新增独立函数，target_symbol 用 "__new__" 且 new_snippet 为完整新函数
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
        extra_body={"thinking": {"type": "disabled"}, "reasoning_effort": "none"},
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
    目标区域补丁：读原文 → _apply_snippet_patch（失败不崩，返回空列表）。
    完整文件模式（legacy new_content）：直接覆盖写，保持既有调用兼容。
    """
    root = repo_root.resolve()
    written: list[Path] = []
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


def _target_regions(adapter: IterableAgentAdapter, repo_root: Path) -> str:
    """为 adapter 声明的每个文件生成符号地图 + 目标区域源代码（截断到 6000 字符）。"""
    root = repo_root.resolve()
    blocks: list[str] = []
    for rel in list(adapter.prompt_files) + list(adapter.workflow_files):
        path = (root / rel.lstrip("/")).resolve()
        if not path.is_relative_to(root) or not path.exists():
            blocks.append(f"{rel}:\n（文件不存在或不可读）")
            continue
        content = path.read_text(encoding="utf-8")
        symbols = _build_symbol_map(content)
        regions: list[str] = []
        for s in symbols[:8]:  # 首 8 个顶层符号（控制 token）
            src = _extract_symbol_source(content, str(s["name"]))
            if src and len(src) <= _MAX_VARIANT_TARGET_CHARS:
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
    variant_hash = _content_hash(variant.new_content)
    output = await _run_replay_subprocess(agent_id, case_id, variant_hash)
    if output.get("timed_out"):
        # 回放超时：不调用评估 LLM（无输出可评），记为超时失败轮。
        score = ScoreDetail(
            0.0,
            0.0,
            0.0,
            0.0,
            gap_analysis=(
                f"回放子进程超时（>{settings.iterate_round_timeout_seconds}s），本轮视为失败"
            ),
        )
    else:
        score = await evaluate_attribution(
            str(output.get("final_response", "")), ground_truth
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
    }
    path = get_data_dir() / "experiments" / f"{case_id}_r{round_no}.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    logger.info("iterate_experiment_recorded", case_id=case_id, round=round_no, score=score.total)
    return {**record, "score_detail_obj": score}


async def _run_replay_subprocess(
    agent_id: str, case_id: str, variant_hash: str
) -> dict[str, object]:
    # 继承父进程环境；PYTHONPATH 指向 src/（src 布局下 python -m aistock_agent.* 需 src 在路径）。
    # 修正：brief 原写 parent.parent（解析为 src/aistock_agent，无法导入 aistock_agent 包），
    # 改为 parent.parent.parent 即 src 目录。
    src_dir = str(Path(__file__).resolve().parent.parent.parent)
    env = {
        **os.environ,
        "REPLAY_CASE_ID": case_id,
        "REPLAY_AGENT": agent_id,
        "PYTHONPATH": src_dir,
    }
    try:
        result = subprocess.run(
            [
                sys.executable,
                "-m",
                "aistock_agent.iterate.replay_runner",
                agent_id,
                case_id,
                variant_hash,
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=settings.iterate_round_timeout_seconds,
        )
    except subprocess.TimeoutExpired:
        # 超时不崩整个闭环：返回 timed_out 标记，调用侧记为超时失败轮（评分 0）。
        # 之前裸抛 TimeoutExpired 导致 run_case 直接退出，多轮闭环无法继续。
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
    if result.returncode != 0:
        raise RuntimeError(f"replay subprocess failed: {result.stderr[-500:]}")
    lines = [ln for ln in result.stdout.strip().splitlines() if ln.strip()]
    try:
        # json.loads 返回 Any，mypy strict 的 no-any-return 要求显式 cast
        return cast("dict[str, object]", json.loads(lines[-1]))
    except (IndexError, json.JSONDecodeError):
        raise RuntimeError(f"replay subprocess bad output: {result.stdout[-500:]}") from None


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


def _content_hash(new_content: dict[str, str]) -> str:
    """变体内容的真实 sha256（json.dumps sorted keys），替代伪造的 git_commit。"""
    import hashlib

    payload = json.dumps(new_content, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _now_iso_date() -> str:
    """实验记录 created_at：ISO 日期（YYYY-MM-DD，本地时区），供报告按日过滤。"""
    return date.today().isoformat()
