"""火山引擎播客API v4测试 - 正确的握手流程

根据 clawhub.ai doubao-podcast 指南：
1. 发送 StartConnection(event=1) → 收到 ConnectionStarted(event=50) + session_id
2. 发送 StartSession(event=100) + 播客参数 → 收到 SessionStarted(event=150)
3. 流式接收音频 (360/361/362)
4. 收到 PodcastEnd(363) + audio_url
5. 发送 FinishConnection(event=2)

Header: [0x11, 0x14, 0x10, 0x00]
Pre-connection:  header(4) + event_type(4) + payload_size(4) + payload
Post-connection: header(4) + event_type(4) + sid_len(4) + session_id + payload_size(4) + payload
"""

import asyncio
import json
import struct
import uuid

import websockets

# 凭证
APP_ID = "5574338866"
ACCESS_TOKEN = "R3JWd8zKAk3Un2iyBC-Gp50nhyvnUHXL"
SECRET_KEY = "cikJsA4zpa-T1TM8uiloFDE4j5kydows"

WS_URL = "wss://openspeech.bytedance.com/api/v3/sami/podcasttts"

SPEAKER_FEMALE = "zh_female_mizaitongxue_v2_saturn_bigtts"
SPEAKER_MALE = "zh_male_dayixiansheng_v2_saturn_bigtts"

HEADER = bytes([0x11, 0x14, 0x10, 0x00])


def pre_frame(event: int, payload: dict) -> bytes:
    """Pre-connection帧: header(4) + event(4) + payload_size(4) + payload"""
    p = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    e = struct.pack(">I", event)
    l = struct.pack(">I", len(p))
    return HEADER + e + l + p


def post_frame(event: int, sid: str, payload: dict) -> bytes:
    """Post-connection帧: header(4) + event(4) + sid_len(4) + sid + payload_size(4) + payload"""
    sb = sid.encode("utf-8")
    p = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    e = struct.pack(">I", event)
    sl = struct.pack(">I", len(sb))
    pl = struct.pack(">I", len(p))
    return HEADER + e + sl + sb + pl + p


def parse_frame(data: bytes) -> dict:
    """解析服务端响应帧"""
    buf = data
    if len(buf) < 4:
        return {"error": "too short"}

    mt = (buf[1] >> 4) & 0xF
    fl = buf[1] & 0xF
    ser = (buf[2] >> 4) & 0xF

    if mt == 0xF:  # 错误帧
        err_code = struct.unpack(">I", buf[4:8])[0] if len(buf) >= 8 else 0
        try:
            err_payload = json.loads(buf[8:].decode("utf-8"))
        except Exception:
            err_payload = buf[8:].hex()
        return {"type": "error", "error_code": err_code, "error_payload": err_payload}

    result = {"type": "response", "message_type": mt, "flags": fl, "serialization": ser}

    off = 4
    if len(buf) >= off + 4:
        evt = struct.unpack(">I", buf[off:off + 4])[0]
        result["event_code"] = evt
        off += 4

    if fl & 0x04 and len(buf) >= off + 4:  # has session_id
        sid_len = struct.unpack(">I", buf[off:off + 4])[0]
        off += 4
        if len(buf) >= off + sid_len:
            result["session_id"] = buf[off:off + sid_len].decode("utf-8")
            off += sid_len

    if len(buf) >= off + 4:
        payload_len = struct.unpack(">I", buf[off:off + 4])[0]
        off += 4
        if len(buf) >= off + payload_len:
            payload_data = buf[off:off + payload_len]
            if ser == 1:  # JSON
                try:
                    result["payload"] = json.loads(payload_data.decode("utf-8"))
                except Exception:
                    result["payload_raw"] = payload_data.hex()
            else:  # Raw audio
                result["audio_size"] = len(payload_data)
                result["audio_data"] = payload_data

    return result


async def test_podcast():
    """播客API完整握手测试"""

    headers = {
        "X-Api-App-Id": APP_ID,
        "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Secret-Key": SECRET_KEY,
        "X-Api-App-Key": "aGjiRDfUWi",
        "X-Api-Resource-Id": "volc.service_type.10050",
        "X-Api-Request-Id": str(uuid.uuid4()),
    }

    print("=== 火山引擎播客API v4测试（正确握手流程）===")

    audio_data = b""
    session_id = ""

    try:
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            print("✅ WebSocket连接成功")

            # Step 1: StartConnection (event=1)
            frame = pre_frame(1, {})
            print(f"\n发送 StartConnection: {len(frame)}B")
            await ws.send(frame)

            # 等待 ConnectionStarted (event=50)
            msg = await asyncio.wait_for(ws.recv(), timeout=30.0)
            parsed = parse_frame(msg)
            print(f"收到: event={parsed.get('event_code')}, sid={parsed.get('session_id')}")

            if parsed.get("type") == "error":
                print(f"❌ 错误: {parsed}")
                return

            session_id = parsed.get("session_id", "")
            if not session_id:
                print("❌ 未获取到session_id")
                return

            print(f"✅ session_id: {session_id}")

            # Step 2: StartSession (event=100) with podcast params
            podcast_payload = {
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

            frame = post_frame(100, session_id, podcast_payload)
            print(f"\n发送 StartSession: {len(frame)}B")
            await ws.send(frame)

            # Step 3: 接收流式音频
            print("\n等待音频流...")
            while True:
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=180.0)

                    if isinstance(msg, bytes):
                        parsed = parse_frame(msg)
                        evt = parsed.get("event_code")

                        if parsed.get("type") == "error":
                            print(f"❌ 错误帧: {parsed}")
                            break

                        if evt == 150:
                            print(f"  ✅ SessionStarted")
                        elif evt == 360:
                            p = parsed.get("payload", {})
                            print(f"  🎙️ RoundStart: {str(p)[:100]}")
                        elif evt == 361:
                            if "audio_data" in parsed:
                                audio_data += parsed["audio_data"]
                                print(f"  🔊 音频块: {parsed['audio_size']}B (总: {len(audio_data)}B)")
                        elif evt == 362:
                            print(f"  ✅ RoundEnd")
                        elif evt == 363:
                            p = parsed.get("payload", {})
                            print(f"  🏁 PodcastEnd")
                            if p and "meta_info" in p:
                                url = p["meta_info"].get("audio_url", "")
                                if url:
                                    print(f"  📥 audio_url: {url[:100]}...")
                        elif evt == 152:
                            print(f"  ✅ SessionFinished")
                            break
                        elif evt == 154:
                            print(f"  📊 Usage: {parsed.get('payload')}")
                        else:
                            print(f"  未知事件: {evt}, data: {str(parsed)[:200]}")

                    elif isinstance(msg, str):
                        print(f"  文本: {msg[:200]}")

                except asyncio.TimeoutError:
                    print("  ❌ 接收超时")
                    break

            # Step 5: FinishConnection (event=2)
            try:
                frame = pre_frame(2, {})
                await ws.send(frame)
                print("\n✅ 已发送 FinishConnection")
            except Exception:
                pass

    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    # 保存音频
    print(f"\n=== 结果 ===")
    print(f"音频: {len(audio_data)}B")

    if audio_data:
        output = f"docs/agent-outputs/podcast/test-v4-{uuid.uuid4().hex[:8]}.mp3"
        with open(output, "wb") as f:
            f.write(audio_data)
        print(f"✅ 已保存: {output}")
    else:
        print("❌ 无音频数据")


if __name__ == "__main__":
    asyncio.run(test_podcast())