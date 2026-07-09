"""火山引擎播客服务 — 双人对话播客生成

WebSocket 调用火山引擎播客 API，根据文本生成双人播客音频。
官方文档：https://www.volcengine.com/docs/6561/1668014

协议要点（v3 SAMI 二进制协议）：
1. 建连 → 发送 StartConnection(event=1) → 收到 ConnectionStarted(event=50) + session_id
2. 发送 StartSession(event=100) + 播客参数 → 收到 SessionStarted(event=150)
3. 流式接收音频 (360 RoundStart / 361 RoundResp / 362 RoundEnd)
4. 收到 PodcastEnd(363) → 可能含 audio_url
5. 发送 FinishConnection(event=2)

二进制帧格式：
- Pre-connection:  header(4) + event_type(4) + payload_size(4) + payload
- Post-connection: header(4) + event_type(4) + sid_len(4) + session_id + payload_size(4) + payload
- Header: [0x11, 0x14, 0x10, 0x00]
"""

import asyncio
import json
import struct
import uuid
from pathlib import Path
from typing import Any

import websockets
from websockets.client import WebSocketClientProtocol

from aistock_agent.config import settings
from aistock_agent.observability.logging import get_logger

logger = get_logger(__name__)

# 固定 Header（Protocol v1, 4-byte header, JSON, no compression）
_HEADER = bytes([0x11, 0x14, 0x10, 0x00])

# 事件码
_EVT_START_CONNECTION = 1
_EVT_FINISH_CONNECTION = 2
_EVT_CONNECTION_STARTED = 50
_EVT_START_SESSION = 100
_EVT_SESSION_STARTED = 150
_EVT_SESSION_FINISHED = 152
_EVT_USAGE_RESPONSE = 154
_EVT_ROUND_START = 360
_EVT_ROUND_RESPONSE = 361
_EVT_ROUND_END = 362
_EVT_PODCAST_END = 363

# 默认发音人（黑猫侦探社咪仔系列，官方推荐配对）
DEFAULT_SPEAKER_FEMALE = "zh_female_mizaitongxue_v2_saturn_bigtts"
DEFAULT_SPEAKER_MALE = "zh_male_dayixiansheng_v2_saturn_bigtts"


def _pre_frame(event: int, payload: dict) -> bytes:
    """构建 Pre-connection 帧: header(4) + event(4) + payload_size(4) + payload"""
    p = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    e = struct.pack(">I", event)
    l = struct.pack(">I", len(p))
    return _HEADER + e + l + p


def _post_frame(event: int, sid: str, payload: dict) -> bytes:
    """构建 Post-connection 帧: header(4) + event(4) + sid_len(4) + sid + payload_size(4) + payload"""
    sb = sid.encode("utf-8")
    p = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    e = struct.pack(">I", event)
    sl = struct.pack(">I", len(sb))
    pl = struct.pack(">I", len(p))
    return _HEADER + e + sl + sb + pl + p


def _parse_frame(data: bytes) -> dict:
    """解析服务端响应帧"""
    if len(data) < 4:
        return {"type": "error", "error": "frame too short"}

    mt = (data[1] >> 4) & 0xF
    fl = data[1] & 0xF
    ser = (data[2] >> 4) & 0xF

    # 错误帧
    if mt == 0xF:
        err_code = struct.unpack(">I", data[4:8])[0] if len(data) >= 8 else 0
        try:
            err_payload = json.loads(data[8:].decode("utf-8"))
        except Exception:
            err_payload = data[8:].hex()
        return {"type": "error", "error_code": err_code, "error_payload": err_payload}

    result: dict[str, Any] = {"type": "response", "message_type": mt, "flags": fl, "serialization": ser}

    off = 4
    # 事件码
    if len(data) >= off + 4:
        result["event_code"] = struct.unpack(">I", data[off:off + 4])[0]
        off += 4

    # session_id（flags & 0x04 时存在）
    if fl & 0x04 and len(data) >= off + 4:
        sid_len = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        if len(data) >= off + sid_len:
            result["session_id"] = data[off:off + sid_len].decode("utf-8")
            off += sid_len

    # payload
    if len(data) >= off + 4:
        payload_len = struct.unpack(">I", data[off:off + 4])[0]
        off += 4
        if len(data) >= off + payload_len:
            payload_data = data[off:off + payload_len]
            if ser == 1:  # JSON
                try:
                    result["payload"] = json.loads(payload_data.decode("utf-8"))
                except Exception:
                    result["payload_raw"] = payload_data.hex()
            else:  # Raw audio
                result["audio_size"] = len(payload_data)
                result["audio_data"] = payload_data

    return result


class VolcenginePodcastService:
    """火山引擎播客服务

    官方文档：https://www.volcengine.com/docs/6561/1668014
    """

    def __init__(self) -> None:
        self.api_url = settings.volc_tts_podcast_url
        self.api_key = settings.volc_tts_api_key
        self.app_id = settings.volc_tts_app_id
        self.access_token = settings.volc_tts_access_token
        self.secret_key = settings.volc_tts_secret_key
        self.host_voice = settings.volc_tts_host_voice
        self.analyst_voice = settings.volc_tts_analyst_voice

    def _build_headers(self) -> dict[str, str]:
        """构建 WebSocket 鉴权 Header"""
        headers = {
            "X-Api-Request-Id": str(uuid.uuid4()),
            "X-Api-Resource-Id": "volc.service_type.10050",
            "X-Api-App-Key": "aGjiRDfUWi",
        }

        if self.api_key:
            headers["X-Api-Key"] = self.api_key
            logger.debug("volc_tts_auth_method", method="api_key")
        else:
            headers["X-Api-App-Id"] = self.app_id
            headers["X-Api-Access-Key"] = self.access_token
            if self.secret_key:
                headers["X-Api-Secret-Key"] = self.secret_key
            logger.debug("volc_tts_auth_method", method="app_id_access_token")

        return headers

    def _build_payload_action0(self, input_text: str) -> dict[str, Any]:
        """构建 action=0 长文本总结模式 Payload"""
        return {
            "input_id": str(uuid.uuid4()),
            "action": 0,
            "input_text": input_text,
            "use_head_music": False,
            "audio_config": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": 0,
            },
            "speaker_info": {
                "random_order": True,
                "speakers": [self.analyst_voice, self.host_voice],
            },
        }

    def _build_payload_action3(self, dialogue_json: str) -> dict[str, Any]:
        """构建 action=3 对话文本模式 Payload

        Args:
            dialogue_json: JSON 格式的对话列表
                [{"role": "host", "content": "..."}, {"role": "analyst", "content": "..."}]
        """
        dialogue_list = json.loads(dialogue_json)
        nlp_texts = []
        for item in dialogue_list:
            speaker = self.host_voice if item["role"] == "host" else self.analyst_voice
            nlp_texts.append({"speaker": speaker, "text": item["content"]})

        return {
            "input_id": str(uuid.uuid4()),
            "action": 3,
            "nlp_texts": nlp_texts,
            "use_head_music": False,
            "audio_config": {
                "format": "mp3",
                "sample_rate": 24000,
                "speech_rate": 0,
            },
        }

    async def generate_podcast(
        self,
        input_text: str,
        output_path: str | None = None,
        action: int = 0,
    ) -> str:
        """生成双人播客音频

        Args:
            input_text: 输入文本
                - action=0: 长文本，API 自动总结生成对话
                - action=3: JSON 格式对话列表 [{"role":"host","content":"..."},...]
            output_path: 输出音频文件路径
            action: 生成类型（0=长文本总结, 3=对话文本）

        Returns:
            音频文件路径

        Raises:
            ValueError: 缺少火山引擎配置
            RuntimeError: 音频生成失败
        """
        if not self.api_key and not (self.app_id and self.access_token):
            raise ValueError(
                "缺少火山引擎 TTS 配置，请在 .env 中设置 VOLC_TTS_API_KEY "
                "或 VOLC_TTS_APP_ID + VOLC_TTS_ACCESS_TOKEN"
            )

        # 默认输出路径
        if output_path is None:
            from datetime import datetime
            timestamp = datetime.now().strftime("%Y-%m-%d-%H%M")
            output_path = f"docs/agent-outputs/podcast/{timestamp}.mp3"

        output_file = Path(output_path)
        output_file.parent.mkdir(parents=True, exist_ok=True)

        logger.info("volc_tts_podcast_start", input_length=len(input_text), output_path=str(output_file), action=action)

        # 构建请求 payload
        if action == 3:
            payload = self._build_payload_action3(input_text)
        else:
            payload = self._build_payload_action0(input_text)

        try:
            headers = self._build_headers()
            async with websockets.connect(
                self.api_url,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                # Step 1: StartConnection → 获取 session_id
                await ws.send(_pre_frame(_EVT_START_CONNECTION, {}))
                conn_msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
                conn_parsed = _parse_frame(conn_msg)

                if conn_parsed.get("type") == "error":
                    raise RuntimeError(f"StartConnection 失败: {conn_parsed}")

                session_id = conn_parsed.get("session_id", "")
                if not session_id:
                    raise RuntimeError(f"未获取到 session_id: {conn_parsed}")

                logger.info("volc_tts_connection_started", session_id=session_id)

                # Step 2: StartSession → 开始播客生成
                await ws.send(_post_frame(_EVT_START_SESSION, session_id, payload))
                logger.info("volc_tts_session_started", action=action)

                # Step 3: 流式接收音频
                audio_data = b""
                audio_url = None

                while True:
                    try:
                        msg = await asyncio.wait_for(ws.recv(), timeout=300.0)
                    except asyncio.TimeoutError:
                        logger.warning("volc_tts_receive_timeout", total_audio=len(audio_data))
                        break
                    except websockets.exceptions.ConnectionClosed:
                        logger.info("volc_tts_connection_closed")
                        break

                    if not isinstance(msg, bytes):
                        continue

                    parsed = _parse_frame(msg)
                    event = parsed.get("event_code")

                    # 错误帧
                    if parsed.get("type") == "error":
                        logger.error("volc_tts_error_frame", error=parsed)
                        break

                    if event == _EVT_SESSION_STARTED:
                        logger.info("volc_tts_session_started_ok")

                    elif event == _EVT_ROUND_START:
                        p = parsed.get("payload", {})
                        logger.debug("volc_tts_round_start", round_id=p.get("round_id"), speaker=p.get("speaker"))

                    elif event == _EVT_ROUND_RESPONSE:
                        if "audio_data" in parsed:
                            audio_data += parsed["audio_data"]

                    elif event == _EVT_ROUND_END:
                        logger.debug("volc_tts_round_end", total_audio=len(audio_data))

                    elif event == _EVT_PODCAST_END:
                        p = parsed.get("payload", {})
                        if p and "meta_info" in p:
                            audio_url = p["meta_info"].get("audio_url")
                        logger.info("volc_tts_podcast_end", total_audio=len(audio_data), has_url=bool(audio_url))

                    elif event == _EVT_SESSION_FINISHED:
                        logger.info("volc_tts_session_finished")
                        break

                    elif event == _EVT_USAGE_RESPONSE:
                        logger.debug("volc_tts_usage", usage=parsed.get("payload"))

                # Step 4: FinishConnection
                try:
                    await ws.send(_pre_frame(_EVT_FINISH_CONNECTION, {}))
                except Exception:
                    pass

                # 如果有 audio_url 但没收到音频流，尝试下载
                if not audio_data and audio_url:
                    import aiohttp
                    async with aiohttp.ClientSession() as session:
                        async with session.get(audio_url) as resp:
                            audio_data = await resp.read()
                    logger.info("volc_tts_audio_downloaded", size=len(audio_data))

                # 保存音频
                if audio_data:
                    output_file.write_bytes(audio_data)
                    logger.info("volc_tts_podcast_success", audio_size=len(audio_data), output_path=str(output_file))
                    return str(output_file)
                else:
                    raise RuntimeError("播客生成失败：未收到音频数据")

        except Exception as e:
            logger.error("volc_tts_podcast_failed", error=str(e), exc_info=True)
            raise RuntimeError(f"播客生成失败: {e}") from e


# 单例
_podcast_service: VolcenginePodcastService | None = None


def get_podcast_service() -> VolcenginePodcastService:
    """获取火山引擎播客服务单例"""
    global _podcast_service
    if _podcast_service is None:
        _podcast_service = VolcenginePodcastService()
    return _podcast_service
