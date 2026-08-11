"""变体实验引擎 —— 生成/应用/恢复待迭代 agent 的提示词、工作流、数据源变体。

每轮实验流程（由 run_case.py 驱动）：
1. restore_baseline 恢复干净基线（git checkout -- 变体文件）
2. generate_variant 让 LLM 基于当前实现 + 标准答案 + 差距分析生成变体
3. apply_variant 写文件
4. 子进程回放（replay_runner）→ evaluator 评分
5. 实验记录落盘 data/experiments/{case_id}_r{round}.json
"""

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

_GENERATE_PROMPT = """你是迭代优化工程师。目标是改进待迭代 Agent 的归因质量。
当前实现文件：{files}
标准答案归因：{ground_truth}
最近评分：{score}（满分 1.0），差距分析：{gap_analysis}
请生成一个最小、可验证的变体方案，输出严格 JSON：
{{
  "type": "prompt_diff|workflow_diff|data_source_diff",
  "files": ["相对仓库根路径的文件"],
  "instructions": "改动思路一句话",
  "new_content": {{"相对仓库根路径": "该文件的完整新内容"}}
}}
要求：只改与差距分析相关的部分；禁止引入无关重构；new_content 必须包含被改文件的完整内容。
只输出 JSON。"""


@dataclass
class VariantPlan:
    type: VariantType
    files: list[str]
    instructions: str
    new_content: dict[str, str] = field(default_factory=dict)


async def generate_variant(
    adapter: IterableAgentAdapter,
    case: dict[str, object],
    ground_truth: dict[str, object],
    current_score: ScoreDetail | None,
    gap_analysis: str,
) -> VariantPlan:
    """LLM 基于当前状态生成变体方案。"""
    files = list(adapter.prompt_files) + list(adapter.workflow_files)
    prompt = _GENERATE_PROMPT.format(
        files=json.dumps(files, ensure_ascii=False),
        ground_truth=json.dumps(ground_truth.get("attribution", {}), ensure_ascii=False),
        score=current_score.total if current_score else "N/A",
        gap_analysis=gap_analysis,
    )
    # LLM 注入沿用 evaluator 的已验证模式：模块级 import + get_deep_think()，
    # 避免 from-import 的绑定陷阱（模块内部状态变更时旧引用失效）。
    llm = llm_service.get_deep_think()
    resp = await llm.ainvoke(
        [SystemMessage(content=prompt), HumanMessage(content=str(case["event_title"]))]
    )
    parsed = _parse_json(str(resp.content))
    raw_type = str(parsed.get("type", "prompt_diff"))
    if raw_type not in _VALID_VARIANT_TYPES:
        raw_type = "prompt_diff"
    raw_files = parsed.get("files")
    if isinstance(raw_files, list):
        plan_files = [str(f) for f in raw_files]
    else:
        plan_files = files
    raw_new_content = parsed.get("new_content")
    if isinstance(raw_new_content, dict):
        plan_new_content = {str(k): str(v) for k, v in raw_new_content.items()}
    else:
        plan_new_content = {}
    return VariantPlan(
        type=cast("VariantType", raw_type),
        files=plan_files,
        instructions=str(parsed.get("instructions", "")),
        new_content=plan_new_content,
    )


def apply_variant(variant: VariantPlan, repo_root: Path) -> list[Path]:
    """把变体写盘（覆盖对应文件），返回实际改动文件列表。

    路径安全：rel 经过 lstrip("/") 后仍可能含 "../../" 逃逸出仓库根，
    必须做包含性校验（resolve 后仍在 repo_root 内），否则变体可越权改写
    沙盒外文件。
    """
    root = repo_root.resolve()
    written: list[Path] = []
    for rel, content in variant.new_content.items():
        path = (root / rel.lstrip("/")).resolve()
        if not path.is_relative_to(root):
            raise ValueError(f"variant path escapes repo root: {rel}")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(path)
        logger.info("iterate_variant_applied", file=str(path))
    return written


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
