"""抖音视频客户端（移植自 douyin-video skill，已验证）。

职责：分享链接解析 → 无水印地址 → 下载 mp4 → FFmpeg 抽音频 →
硅基流动 SenseVoice 转写 → 落盘 transcript.md。
只做"视频 → 文本"，不包含分析。阻塞 IO（下载/ffmpeg/转写），
调用方（skill）须用 asyncio.to_thread 包装避免阻塞事件循环。
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from datetime import datetime
from pathlib import Path
from typing import Any

import requests

DEFAULT_API_BASE_URL = "https://api.siliconflow.cn/v1/audio/transcriptions"
DEFAULT_MODEL = "FunAudioLLM/SenseVoiceSmall"

# 模拟移动端访问，避免被识别为爬虫
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_2 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) EdgiOS/121.0.2277.107 "
        "Version/17.0 Mobile/15E148 Safari/604.1"
    )
}

#: 出站请求超时（秒）：解析 / 下载 / 转写。
#: requests 默认无超时，链接不可达时 to_thread 线程会长期挂起、耗尽线程池，
#: 必须显式传 timeout 防挂起。
REQUEST_TIMEOUT_PARSE = 30
REQUEST_TIMEOUT_DOWNLOAD = 120
REQUEST_TIMEOUT_TRANSCRIBE = 300


def _load_ffmpeg():
    try:
        import ffmpeg  # type: ignore[import-not-found]
    except ImportError as exc:  # pragma: no cover - 环境缺失分支
        raise RuntimeError(
            "缺少依赖 ffmpeg-python，请安装（pyproject dependencies）。"
        ) from exc
    return ffmpeg


def _describe_runtime_error(error: Exception) -> str:
    message = str(error)
    winerror = getattr(error, "winerror", None)
    if isinstance(error, PermissionError) or winerror == 5 or any(
        marker in message for marker in ("WinError 5", "拒绝访问", "Access is denied")
    ):
        return (
            "当前运行环境存在权限隔离（WinError 5），无法执行宿主依赖；"
            "请检查 FFmpeg 路径配置后重试。"
        )
    return message


def _check_binary(
    name: str, env_var: str, settings_obj: object | None = None
) -> dict[str, Any]:
    configured = (
        getattr(settings_obj, env_var.lower(), "")
        or os.getenv(env_var)
        or shutil.which(name)
        or name
    )
    try:
        result = subprocess.run(
            [configured, "-version"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
    except Exception as exc:
        return {"name": name, "ok": False, "detail": _describe_runtime_error(exc)}
    if result.returncode != 0:
        return {"name": name, "ok": False, "detail": f"命令返回退出码 {result.returncode}"}
    return {"name": name, "ok": True, "detail": str(configured)}


def run_doctor(
    api_key: str = "", settings_obj: object | None = None
) -> dict[str, Any]:
    """检查转写所需依赖：API_KEY / ffmpeg-python / FFmpeg / FFprobe。"""
    if settings_obj is None:
        from aistock_agent.config import settings as _settings

        settings_obj = _settings
    resolved_key = (
        api_key
        or os.getenv("DOUYIN_API_KEY")
        or getattr(settings_obj, "douyin_api_key", "")
    )
    checks: list[dict[str, Any]] = [
        {
            "name": "API_KEY",
            "ok": bool(resolved_key),
            "detail": "已配置" if resolved_key else "未配置",
        },
    ]
    try:
        _load_ffmpeg()
        checks.append({"name": "ffmpeg-python", "ok": True, "detail": "可导入"})
    except Exception as exc:
        checks.append(
            {"name": "ffmpeg-python", "ok": False, "detail": _describe_runtime_error(exc)}
        )

    ffmpeg_check = _check_binary("ffmpeg", "FFMPEG_BINARY", settings_obj)
    ffmpeg_check["name"] = "FFmpeg"
    checks.append(ffmpeg_check)

    ffprobe_check = _check_binary("ffprobe", "FFPROBE_BINARY", settings_obj)
    ffprobe_check["name"] = "FFprobe"
    checks.append(ffprobe_check)

    return {"ok": all(check["ok"] for check in checks), "checks": checks}


class DouyinClient:
    """抖音视频处理器：解析、下载、抽音频、转写。"""

    def __init__(
        self,
        api_key: str = "",
        api_base_url: str = DEFAULT_API_BASE_URL,
        model: str = DEFAULT_MODEL,
        ffmpeg_binary: str = "ffmpeg",
        ffprobe_binary: str = "ffprobe",
    ) -> None:
        self.api_key = api_key
        self.api_base_url = api_base_url
        self.model = model
        self.ffmpeg_binary = ffmpeg_binary
        self.ffprobe_binary = ffprobe_binary
        self.temp_dir = Path(tempfile.mkdtemp(prefix="douyin_"))

    # ── 链接解析 ──
    def parse_share_url(self, share_text: str) -> dict:
        urls = re.findall(
            r"http[s]?://(?:[a-zA-Z]|[0-9]|[$-_@.&+]|[!*\(\),]|(?:%[0-9a-fA-F][0-9a-fA-F]))+",
            share_text,
        )
        if not urls:
            raise ValueError("未找到有效的分享链接")

        share_response = requests.get(
            urls[0], headers=HEADERS, timeout=REQUEST_TIMEOUT_PARSE
        )
        video_id = share_response.url.split("?")[0].strip("/").split("/")[-1]
        share_url = f"https://www.iesdouyin.com/share/video/{video_id}"

        response = requests.get(
            share_url, headers=HEADERS, timeout=REQUEST_TIMEOUT_PARSE
        )
        response.raise_for_status()

        pattern = re.compile(r"window\._ROUTER_DATA\s*=\s*(.*?)</script>", flags=re.DOTALL)
        match = pattern.search(response.text)
        if not match or not match.group(1):
            raise ValueError("从HTML中解析视频信息失败")

        json_data = json.loads(match.group(1).strip())
        video_page_key = "video_(id)/page"
        note_page_key = "note_(id)/page"

        if video_page_key in json_data["loaderData"]:
            original = json_data["loaderData"][video_page_key]["videoInfoRes"]
        elif note_page_key in json_data["loaderData"]:
            original = json_data["loaderData"][note_page_key]["videoInfoRes"]
        else:
            raise ValueError("无法从JSON中解析视频或图集信息")

        data = original["item_list"][0]
        video_url = data["video"]["play_addr"]["url_list"][0].replace("playwm", "play")
        desc = data.get("desc", "").strip() or f"douyin_{video_id}"
        # 去掉抖音话题标签（#xxx），仅保留标题正文（测试契约：title 不含话题）
        desc = desc.split("#", 1)[0].strip() or f"douyin_{video_id}"
        desc = re.sub(r'[\\/:*?"<>|]', "_", desc)

        return {"url": video_url, "title": desc, "video_id": video_id}

    # ── 下载 ──
    def download_video(self, video_info: dict, output_dir: Path | None = None) -> Path:
        if output_dir is None:
            output_dir = self.temp_dir
        else:
            output_dir = Path(output_dir)
            output_dir.mkdir(parents=True, exist_ok=True)

        filepath = output_dir / f"{video_info['video_id']}.mp4"
        response = requests.get(
            video_info["url"],
            headers=HEADERS,
            stream=True,
            # stream 请求 timeout 覆盖连接阶段，已足够防挂起
            timeout=REQUEST_TIMEOUT_DOWNLOAD,
        )
        response.raise_for_status()

        with open(filepath, "wb") as f:
            for chunk in response.iter_content(chunk_size=8192):
                if chunk:
                    f.write(chunk)
        return filepath

    # ── 抽音频 ──
    def extract_audio(self, video_path: Path) -> Path:
        ffmpeg = _load_ffmpeg()
        audio_path = video_path.with_suffix(".mp3")
        ffmpeg.input(str(video_path)).output(
            str(audio_path), acodec="libmp3lame", q=0
        ).run(
            cmd=self.ffmpeg_binary,
            capture_stdout=True,
            capture_stderr=True,
            overwrite_output=True,
        )
        return audio_path

    # ── 转写 ──
    def transcribe_audio(self, audio_path: Path) -> str:
        if not self.api_key:
            raise ValueError("未配置 DOUYIN_API_KEY，无法转写")
        with open(audio_path, "rb") as f:
            files = {
                "file": (audio_path.name, f, "audio/mpeg"),
                "model": (None, self.model),
            }
            headers = {"Authorization": f"Bearer {self.api_key}"}
            response = requests.post(
                self.api_base_url,
                files=files,
                headers=headers,
                timeout=REQUEST_TIMEOUT_TRANSCRIBE,
            )
            response.raise_for_status()
            result = response.json()
        return result.get("text", response.text)

    # ── 一键：链接 → 文本 + transcript.md ──
    def extract_text(
        self, share_link: str, output_dir: Path, save_video: bool = False
    ) -> dict:
        video_info = self.parse_share_url(share_link)
        video_path = self.download_video(video_info)
        audio_path = self.extract_audio(video_path)
        try:
            text_content = self.transcribe_audio(audio_path)
        except Exception:
            # 转写失败：清理临时下载文件（mp4/mp3）后重新抛出
            self._cleanup(video_path, audio_path)
            raise

        video_folder = Path(output_dir) / video_info["video_id"]
        video_folder.mkdir(parents=True, exist_ok=True)
        transcript_path = video_folder / "transcript.md"
        transcript_path.write_text(
            f"# {video_info['title']}\n\n"
            f"| 属性 | 值 |\n|------|----|\n"
            f"| 视频ID | `{video_info['video_id']}` |\n"
            f"| 提取时间 | {datetime.now().strftime('%Y-%m-%d %H:%M:%S')} |\n"
            f"| 下载链接 | [点击下载]({video_info['url']}) |\n\n"
            f"---\n\n## 文案内容\n\n{text_content}",
            encoding="utf-8",
        )

        if save_video:
            shutil.copy2(video_path, video_folder / f"{video_info['video_id']}.mp4")

        # 清理临时下载文件（mp4/mp3），保留 transcript 产物
        self._cleanup(video_path, audio_path)

        return {
            "video_info": video_info,
            "text": text_content,
            "output_path": str(video_folder),
        }

    def _cleanup(self, *paths: Path) -> None:
        for path in paths:
            try:
                if path.exists():
                    path.unlink()
            except OSError:
                pass
