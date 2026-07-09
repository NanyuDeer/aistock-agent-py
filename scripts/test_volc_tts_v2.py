"""火山引擎播客API v2测试 - 正确的二进制协议

根据官方文档 https://docs.volcengine.com/docs/6561/1668014
正确实现Full-client request格式的二进制协议
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

# 正确的发音人名称（官方文档推荐配对）
SPEAKER_FEMALE = "zh_female_mizaitongxue_v2_saturn_bigtts"  # 黑猫侦探社咪仔-女
SPEAKER_MALE = "zh_male_dayixiansheng_v2_saturn_bigtts"      # 黑猫侦探社咪仔-男


def build_full_client_request(session_id: str, event_code: int, payload: dict) -> bytes:
    """构建Full-client request二进制帧

    格式（参考官方文档2.4节 StartSession）：
    - Byte 0: 0x11 (Protocol version=1, Header size=1 -> 4 bytes)
    - Byte 1: 0x94 (Message type=0b1001 Full-client request, flags=0b0100 with event number)
    - Byte 2: 0x10 (JSON serialization, no compression)
    - Byte 3: 0x00 (reserved)
    - Bytes 4-7: Event code (4 bytes, big-endian)
    - Bytes 8-11: Session ID length (4 bytes, big-endian)
    - Bytes 12+: Session ID (UTF-8 string)
    - Then: Payload length (4 bytes, big-endian) + Payload (UTF-8 JSON)
    """
    # Header (4 bytes)
    header = struct.pack(">BBBB", 0x11, 0x94, 0x10, 0x00)

    # Event code (4 bytes, big-endian)
    event_bytes = struct.pack(">I", event_code)

    # Session ID
    session_id_bytes = session_id.encode("utf-8")
    session_id_length = struct.pack(">I", len(session_id_bytes))

    # Payload
    payload_bytes = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    payload_length = struct.pack(">I", len(payload_bytes))

    return header + event_bytes + session_id_length + session_id_bytes + payload_length + payload_bytes


def build_finish_connection(session_id: str) -> bytes:
    """构建FinishConnection二进制帧

    格式：
    - Byte 0: 0x11
    - Byte 1: 0x14 (Message type=0b0001, flags=0b0100 with event number)
    - Byte 2: 0x10 (JSON, no compression)
    - Byte 3: 0x00
    - Bytes 4-7: Event code = 2 (FinishConnection)
    - Bytes 8-11: Session ID length
    - Bytes 12+: Session ID
    - Then: Payload length (4 bytes) + Payload (empty JSON)
    """
    header = struct.pack(">BBBB", 0x11, 0x14, 0x10, 0x00)
    event_bytes = struct.pack(">I", 2)  # FinishConnection event code = 2
    session_id_bytes = session_id.encode("utf-8")
    session_id_length = struct.pack(">I", len(session_id_bytes))
    payload_bytes = b"{}"
    payload_length = struct.pack(">I", len(payload_bytes))

    return header + event_bytes + session_id_length + session_id_bytes + payload_length + payload_bytes


def parse_response_frame(data: bytes) -> dict:
    """解析服务端响应二进制帧"""
    if len(data) < 4:
        return {"error": "frame too short", "raw": data.hex()}

    byte0 = data[0]
    byte1 = data[1]
    byte2 = data[2]
    byte3 = data[3]

    protocol_version = (byte0 >> 4) & 0x0F
    header_size = byte0 & 0x0F
    message_type = (byte1 >> 4) & 0x0F
    message_flags = byte1 & 0x0F
    serialization = (byte2 >> 4) & 0x0F
    compression = byte2 & 0x0F

    result = {
        "protocol_version": protocol_version,
        "header_size": header_size * 4,
        "message_type": f"0b{message_type:04b}",
        "message_flags": f"0b{message_flags:04b}",
        "serialization": "JSON" if serialization == 1 else "Raw",
        "compression": "gzip" if compression == 1 else "none",
    }

    # Check for error frame (Message type = 0b1111)
    if message_type == 0b1111:
        error_code = struct.unpack(">I", data[4:8])[0] if len(data) >= 8 else 0
        result["error_code"] = error_code
        try:
            error_payload = json.loads(data[8:].decode("utf-8"))
            result["error_payload"] = error_payload
        except Exception:
            result["error_raw"] = data[8:].hex()
        return result

    # Parse event code if flags indicate it
    offset = 4  # After header
    if message_flags & 0x04:  # has event number
        if len(data) >= offset + 4:
            event_code = struct.unpack(">I", data[offset:offset + 4])[0]
            result["event_code"] = event_code
            offset += 4

        # Parse session ID
        if len(data) >= offset + 4:
            session_id_length = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            if len(data) >= offset + session_id_length:
                session_id = data[offset:offset + session_id_length].decode("utf-8")
                result["session_id"] = session_id
                offset += session_id_length

        # Parse payload
        if len(data) >= offset + 4:
            payload_length = struct.unpack(">I", data[offset:offset + 4])[0]
            offset += 4
            if len(data) >= offset + payload_length:
                payload_data = data[offset:offset + payload_length]
                if serialization == 1:  # JSON
                    try:
                        result["payload"] = json.loads(payload_data.decode("utf-8"))
                    except Exception:
                        result["payload_raw"] = payload_data.hex()
                else:  # Raw (audio data)
                    result["payload_size"] = len(payload_data)
                    result["payload_type"] = "audio"
    else:
        # No event number, just payload
        if serialization == 1:  # JSON
            try:
                result["payload"] = json.loads(data[offset:].decode("utf-8"))
            except Exception:
                result["payload_raw"] = data[offset:].hex()
        else:
            result["payload_size"] = len(data) - offset
            result["payload_type"] = "audio"

    return result


async def test_podcast():
    """测试播客API（正确的二进制协议）"""

    session_id = str(uuid.uuid4())[:16]  # 短session ID

    # 构建鉴权Header
    headers = {
        "X-Api-App-Id": APP_ID,
        "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Secret-Key": SECRET_KEY,
        "X-Api-App-Key": "aGjiRDfUWi",
        "X-Api-Resource-Id": "volc.service_type.10050",
        "X-Api-Request-Id": str(uuid.uuid4()),
    }

    # 构建Payload（action=3, 使用正确的发音人名称）
    payload = {
        "input_id": str(uuid.uuid4()),
        "action": 3,
        "use_head_music": False,
        "use_tail_music": False,
        "audio_config": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 0,
        },
        "speaker_info": {
            "random_order": True,
            "speakers": [SPEAKER_MALE, SPEAKER_FEMALE],
        },
        "nlp_texts": [
            {
                "speaker": SPEAKER_MALE,
                "text": "各位投资者早上好，欢迎收听今日股市播报。",
            },
            {
                "speaker": SPEAKER_FEMALE,
                "text": "今天AI算力板块领涨，多只个股涨停，市场情绪积极。",
            },
        ],
    }

    print(f"=== 火山引擎播客API v2测试 ===")
    print(f"Session ID: {session_id}")
    print(f"App ID: {APP_ID}")
    print(f"Speaker Male: {SPEAKER_MALE}")
    print(f"Speaker Female: {SPEAKER_FEMALE}")
    print(f"Payload: {json.dumps(payload, indent=2, ensure_ascii=False)}")
    print()

    audio_data = b""

    try:
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            print("✅ WebSocket连接成功")

            # 发送StartSession请求（Event code = 1）
            request_frame = build_full_client_request(session_id, 1, payload)
            print(f"发送StartSession请求: {len(request_frame)} 字节")
            print(f"  Header: {request_frame[:4].hex()}")
            print(f"  Event: {request_frame[4:8].hex()}")
            await ws.send(request_frame)

            # 接收响应
            print("\n等待响应...")
            while True:
                try:
                    message = await asyncio.wait_for(ws.recv(), timeout=120.0)

                    if isinstance(message, bytes):
                        parsed = parse_response_frame(message)
                        event_code = parsed.get("event_code")
                        msg_type = parsed.get("message_type")

                        print(f"\n收到帧: {len(message)} 字节")
                        print(f"  Message type: {msg_type}")
                        print(f"  Event code: {event_code}")

                        # 错误帧
                        if msg_type == "0b1111":
                            print(f"  ❌ 错误: {json.dumps(parsed, indent=2, ensure_ascii=False)}")
                            break

                        # 事件处理
                        if event_code == 150:
                            print(f"  ✅ SessionStarted - 会话已开始")
                            print(f"  Session ID: {parsed.get('session_id')}")

                        elif event_code == 154:
                            print(f"  📊 UsageResponse: {parsed.get('payload')}")

                        elif event_code == 360:
                            print(f"  🎙️ PodcastRoundStart: {parsed.get('payload')}")

                        elif event_code == 361:
                            # 音频数据
                            if parsed.get("payload_type") == "audio":
                                audio_size = parsed.get("payload_size", 0)
                                audio_data += message[-audio_size:] if audio_size > 0 else b""
                                print(f"  🔊 音频数据: {audio_size} 字节")

                        elif event_code == 362:
                            print(f"  ✅ PodcastRoundEnd: {parsed.get('payload')}")

                        elif event_code == 363:
                            print(f"  🏁 PodcastEnd: {parsed.get('payload')}")
                            # 如果有audio_url，下载
                            payload_data = parsed.get("payload", {})
                            if payload_data and "meta_info" in payload_data:
                                audio_url = payload_data["meta_info"].get("audio_url")
                                if audio_url:
                                    print(f"  📥 音频URL: {audio_url}")

                        elif event_code == 152:
                            print(f"  ✅ SessionFinished - 会话已结束")
                            break

                        elif event_code == 52:
                            print(f"  ✅ ConnectionFinished")
                            break

                        else:
                            print(f"  未知事件: {parsed}")

                    elif isinstance(message, str):
                        print(f"收到文本: {message[:200]}")

                except asyncio.TimeoutError:
                    print("❌ 接收超时")
                    break

            # 发送FinishConnection
            try:
                finish_frame = build_finish_connection(session_id)
                await ws.send(finish_frame)
                print("\n已发送FinishConnection")
            except Exception:
                pass

    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ 连接关闭: {e}")
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    # 保存音频
    print(f"\n=== 结果 ===")
    print(f"音频数据: {len(audio_data)} 字节")

    if audio_data:
        output_path = f"docs/agent-outputs/podcast/test-v2-{uuid.uuid4().hex[:8]}.mp3"
        with open(output_path, "wb") as f:
            f.write(audio_data)
        print(f"✅ 音频已保存: {output_path}")
    else:
        print("❌ 未收到音频数据")


if __name__ == "__main__":
    asyncio.run(test_podcast())