"""douyin_video Skill — 抖音视频读取（下载 → 语音识别 → 文本）。

用户提供抖音分享链接（或博主主页产出的链接清单中任一条），
本 skill 下载视频、FFmpeg 抽音频、硅基流动 SenseVoice 转写，
返回转写全文（facts）并落盘 transcript.md。分析流程不在本 skill 范围。
"""
from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import structlog

from aistock_agent.config import settings
from aistock_agent.schemas.chat_contract import ChatSource, Evidence, InsightGoal
from aistock_agent.skills.base import skill
from aistock_agent.skills.douyin_client import DouyinClient

logger = structlog.get_logger()

#: 转写产物默认落盘目录（相对服务 cwd；可通过 args.output_dir 覆盖）
DEFAULT_OUTPUT_DIR = Path("data/douyin_transcripts")


@skill
async def douyin_video(args: dict[str, Any], goal: InsightGoal) -> Evidence:
    link = args.get("link")
    if not isinstance(link, str) or not link.strip():
        raise ValueError("缺少 link 参数：请提供抖音分享链接")

    save_video = bool(args.get("save_video", False))
    output_dir = Path(str(args.get("output_dir") or DEFAULT_OUTPUT_DIR))
    output_dir.mkdir(parents=True, exist_ok=True)

    client = DouyinClient(
        api_key=settings.douyin_api_key,
        ffmpeg_binary=settings.ffmpeg_binary or "ffmpeg",
        ffprobe_binary=settings.ffprobe_binary or "ffprobe",
    )

    # 阻塞 IO（下载/ffmpeg/转写）放线程池，避免阻塞事件循环
    result = await asyncio.to_thread(client.extract_text, link.strip(), output_dir, save_video)

    now = datetime.now(UTC)
    video_info = result["video_info"]
    text = result["text"]
    facts = [
        f"视频《{video_info['title']}》（ID {video_info['video_id']}）转写完成：\n{text}"
    ]
    sources = [
        ChatSource(
            source_id=f"douyin:{video_info['video_id']}:{now.isoformat()}",
            kind="realtime_quote",
            title=video_info["title"],
            url=video_info["url"],
            snippet=text[:200],
            captured_at=now,
        )
    ]

    return Evidence(
        facts=facts,
        sources=sources,
        as_of=now,
        degraded=False,
        skill_name="douyin_video",
        raw={
            "video_info": video_info,
            "transcript_path": result["output_path"],
            "doctor": _doctor_summary(client),
        },
    )


def _doctor_summary(client: DouyinClient) -> dict:
    """附带依赖自检结果（失败不阻断，仅记录）。"""
    from aistock_agent.skills.douyin_client import run_doctor

    try:
        return run_doctor(api_key=client.api_key, settings_obj=settings)
    except Exception as exc:  # pragma: no cover - 防御性
        logger.warning("douyin_doctor_failed", err=str(exc))
        return {"ok": False, "checks": []}
