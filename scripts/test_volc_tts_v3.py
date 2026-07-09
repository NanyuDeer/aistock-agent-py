"""火山引擎播客API v3测试 - 简单文本帧 + 正确发音人

根据官方文档，sendText使用 Message type=0b001, flags=0b0000
"""

import asyncio
import json
import struct
import uuid

import websockets

# 旧版控制台凭证
APP_ID = "5574338866"
ACCESS_TOKEN = "R3JWd8zKAk3Un2iyBC-Gp50nhyvnUHXL"
SECRET_KEY = "cikJsA4zpa-T1TM8uiloFDE4j5kydows"

# WebSocket URL
WS_URL = "wss://openspeech.bytedance.com/api/v3/sami/podcasttts"

# 正确的发音人名称
SPEAKER_FEMALE = "zh_female_mizaitongxue_v2_saturn_bigtts"
SPEAKER_MALE = "zh_male_dayixiansheng_v2_saturn_bigtts"


def build_send_text_frame(payload: dict) -> bytes:
    """构建sendText二进制帧

    根据官方文档二进制帧表格：
    - Byte 0: Protocol version=0b0001, Header size=0b0001 (4 bytes)
    - Byte 1: Message type=0b001, flags=0b0000 (sendText)
    - Byte 2: Serialization=0b0001 (JSON), Compression=0b0000
    - Byte 3: Reserved=0x00
    - [4~7]: Optional field (depends on flags, flags=0 so NO optional field)
    - Payload: JSON

    但实际上根据StartSession示例，payload前面有payload_size字段。
    尝试两种格式：无payload_size 和 有payload_size
    """
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    payload_size = len(payload_bytes)

    # Header (4 bytes)
    header = struct.pack(">BBBB", 0x11, 0x10, 0x10, 0x00)

    # Payload size (4 bytes, big-endian)
    payload_size_bytes = struct.pack(">I", payload_size)

    return header + payload_size_bytes + payload_bytes


def parse_response(data: bytes) -> dict:
    """解析服务端响应"""
    if len(data) < 4:
        return {"error": "frame too short"}

    byte1 = data[1]
    byte2 = data[2]
    message_type = (byte1 >> 4) & 0x0F
    message_flags = byte1 & 0x0F
    serialization = (byte2 >> 4) & 0x0F
    is_error = message_type == 0b1111

    result = {
        "message_type": f"0b{message_type:04b}",
        "message_flags": f"0b{message_flags:04b}",
        "serialization": "JSON" if serialization == 1 else "Raw",
        "is_error": is_error,
    }

    if is_error:
        # Error frame: [4~7] = error code, then payload
        if len(data) >= 8:
            error_code = struct.unpack(">I", data[4:8])[0]
            result["error_code"] = error_code
        try:
            error_payload = json.loads(data[8:].decode("utf-8"))
            result["error_payload"] = error_payload
        except Exception:
            result["error_raw"] = data[8:].hex() if len(data) > 8 else ""
        return result

    # Normal response
    offset = 4
    has_event = bool(message_flags & 0x04)

    if has_event:
        if len(data) >= offset + 4:
            event_code = struct.unpack(">I", data[offset:offset + 4])[0]
            result["event_code"] = event_code
            offset += 4

        if len(data) >= offset + 4:
            session_id_len = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            if len(data) >= offset + session_id_len:
                result["session_id"] = data[offset:offset + session_id_len].decode("utf-8")
                offset += session_id_len

        if len(data) >= offset + 4:
            payload_len = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            payload_data = data[offset:offset + payload_len]

            if serialization == 1:  # JSON
                try:
                    result["payload"] = json.loads(payload_data.decode("utf-8"))
                except Exception:
                    result["payload_raw"] = payload_data.hex()
            else:  # Raw audio
                result["audio_size"] = len(payload_data)
                result["audio_data"] = payload_data
    else:
        # No event number
        if serialization == 1:
            try:
                result["payload"] = json.loads(data[offset:].decode("utf-8"))
            except Exception:
                result["payload_raw"] = data[offset:].hex()
        else:
            result["audio_size"] = len(data) - offset
            result["audio_data"] = data[offset:]

    return result


async def test_podcast():
    """测试播客API（简单文本帧）"""

    # 构建鉴权Header
    headers = {
        "X-Api-App-Id": APP_ID,
        "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Secret-Key": SECRET_KEY,
        "X-Api-App-Key": "aGjiRDfUWi",
        "X-Api-Resource-Id": "volc.service_type.10050",
        "X-Api-Request-Id": str(uuid.uuid4()),
    }

    # Payload（action=0 长文本模式，最简单的测试）
    payload_action0 = {
        "input_id": str(uuid.uuid4()),
        "action": 0,
        "input_text": "分析下当前的大模型发展",
        "use_head_music": False,
        "audio_config": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 0,
        },
        "speaker_info": {
            "random_order": True,
            "speakers": [SPEAKER_MALE, SPEAKER_FEMALE],
        },
    }

    # Payload（action=4 prompt模式，最简单）
    payload_action4 = {
        "input_id": str(uuid.uuid4()),
        "action": 4,
        "prompt_text": "火山引擎",
        "use_head_music": False,
        "audio_config": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 0,
        },
    }

    # Payload（action=3 对话模式）
    payload_action3 = {
        "input_id": str(uuid.uuid4()),
        "action": 3,
        "use_head_music": False,
        "audio_config": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 0,
        },
        "nlp_texts": [
            {
                "speaker": SPEAKER_MALE,
                "text": "今天呢我们要聊的呢是火山引擎在这个 FORCE 原动力大会上面的一些比较重磅的发布。",
            },
            {
                "speaker": SPEAKER_FEMALE,
                "text": "来看看都有哪些亮点哈。",
            },
        ],
    }

    # 测试 action=0
    for name, payload in [("action=0 长文本", payload_action0), ("action=4 prompt", payload_action4), ("action=3 对话", payload_action3)]:
        print(f"\n{'='*80}")
        print(f"测试: {name}")
        print(f"Payload: {json.dumps(payload, indent=2, ensure_ascii=False)[:300]}")

        audio_data = b""

        try:
            async with websockets.connect(
                WS_URL,
                additional_headers=headers,
                ping_interval=30,
                ping_timeout=10,
            ) as ws:
                print("✅ WebSocket连接成功")

                frame = build_send_text_frame(payload)
                print(f"发送请求: {len(frame)} 字节, Header: {frame[:4].hex()}")
                await ws.send(frame)

                print("等待响应（最多120秒）...")
                while True:
                    try:
                        message = await asyncio.wait_for(ws.recv(), timeout=120.0)

                        if isinstance(message, bytes):
                            parsed = parse_response(message)

                            if parsed.get("is_error"):
                                print(f"❌ 错误: {json.dumps(parsed, indent=2, ensure_ascii=False)}")
                                break

                            event_code = parsed.get("event_code")
                            print(f"  帧: {len(message)}B, event={event_code}, type={parsed.get('message_type')}")

                            if event_code == 150:
                                print(f"  ✅ SessionStarted")
                            elif event_code == 360:
                                print(f"  🎙️ RoundStart: {parsed.get('payload')}")
                            elif event_code == 361:
                                if "audio_data" in parsed:
                                    audio_data += parsed["audio_data"]
                                    print(f"  🔊 音频: {parsed['audio_size']}B (总: {len(audio_data)}B)")
                            elif event_code == 362:
                                print(f"  ✅ RoundEnd: {parsed.get('payload')}")
                            elif event_code == 363:
                                print(f"  🏁 PodcastEnd: {parsed.get('payload')}")
                            elif event_code == 152:
                                print(f"  ✅ SessionFinished")
                                break
                            elif event_code == 154:
                                print(f"  📊 Usage: {parsed.get('payload')}")
                            else:
                                print(f"  未知: {parsed}")
                        elif isinstance(message, str):
                            print(f"  文本: {message[:200]}")

                    except asyncio.TimeoutError:
                        print("  ❌ 超时")
                        break

        except Exception as e:
            print(f"❌ 错误: {e}")
            import traceback
            traceback.print_exc()

        if audio_data:
            output_path = f"docs/agent-outputs/podcast/test-v3-{uuid.uuid4().hex[:8]}.mp3"
            with open(output_path, "wb") as f:
                f.write(audio_data)
            print(f"\n✅ 音频已保存: {output_path} ({len(audio_data)} 字节)")
        else:
            print(f"\n❌ 无音频数据")

        # 只测试第一个成功的
        if audio_data:
            break


if __name__ == "__main__":
    asyncio.run(test_podcast())