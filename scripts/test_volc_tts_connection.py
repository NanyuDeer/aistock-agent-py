"""火山引擎播客API连接测试

简化测试，直接验证WebSocket连接和API响应
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

# 模型实例ID
MODEL_ID = "Doubao_scene_SLM_Doubao_Podcast_model2000000845879864642"


async def test_connection():
    """测试WebSocket连接"""

    # 构建请求Header
    headers = {
        "X-Api-App-Id": APP_ID,
        "X-Api-Access-Key": ACCESS_TOKEN,
        "X-Api-Secret-Key": SECRET_KEY,
        "X-Api-App-Key": "aGjiRDfUWi",  # 固定值
        "X-Api-Resource-Id": "volc.service_type.10050",  # 播客语音合成资源ID
        "X-Api-Request-Id": str(uuid.uuid4()),
    }

    # 构建请求Payload（最简测试）
    payload = {
        "action": 3,  # 根据对话文本生成播客
        "input_id": str(uuid.uuid4()),
        "nlp_texts": [
            {
                "speaker": "preset_female_1",
                "text": "测试语音生成",
            }
        ],
        "use_head_music": False,
        "use_tail_music": False,
        "audio_config": {
            "format": "mp3",
            "sample_rate": 24000,
            "speech_rate": 0,
        },
    }

    print(f"=== 测试1: 基础连接（无模型实例ID）===")
    print(f"App ID: {APP_ID}")
    print(f"Headers: {json.dumps(headers, indent=2)}")
    print(f"Payload: {json.dumps(payload, indent=2)}")

    try:
        # 建立WebSocket连接
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            print("✅ WebSocket连接成功")

            # 构建二进制帧（Header + Payload size + Payload）
            payload_bytes = json.dumps(payload).encode("utf-8")
            payload_size = len(payload_bytes)
            header = struct.pack(">BBBB", 0x11, 0x10, 0x10, 0x00)
            payload_size_bytes = struct.pack(">I", payload_size)
            message = header + payload_size_bytes + payload_bytes

            print(f"发送请求: {len(message)} 字节")
            await ws.send(message)

            # 接收响应
            print("等待响应...")
            message = await asyncio.wait_for(ws.recv(), timeout=60.0)

            if isinstance(message, bytes):
                print(f"收到二进制帧: {len(message)} 字节")
                print(f"前16字节: {message[:16].hex()}")

                # 尝试解析JSON响应
                frame_type = message[2] >> 4
                if frame_type == 0x1:
                    try:
                        json_data = json.loads(message[8:].decode("utf-8"))
                        print(f"JSON响应: {json.dumps(json_data, indent=2)}")
                    except:
                        print(f"无法解析JSON: {message[8:].hex()}")
            elif isinstance(message, str):
                print(f"收到文本消息: {message}")

    except asyncio.TimeoutError:
        print("❌ 接收超时（60秒）")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ 连接关闭: {e}")
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()

    print("\n" + "="*80 + "\n")

    # 测试2: 添加模型实例ID
    print(f"=== 测试2: 添加模型实例ID ===")
    payload_with_model = payload.copy()
    payload_with_model["model_id"] = MODEL_ID  # 添加模型实例ID
    print(f"Payload: {json.dumps(payload_with_model, indent=2)}")

    try:
        async with websockets.connect(
            WS_URL,
            additional_headers=headers,
            ping_interval=30,
            ping_timeout=10,
        ) as ws:
            print("✅ WebSocket连接成功")

            payload_bytes = json.dumps(payload_with_model).encode("utf-8")
            payload_size = len(payload_bytes)
            header = struct.pack(">BBBB", 0x11, 0x10, 0x10, 0x00)
            payload_size_bytes = struct.pack(">I", payload_size)
            message = header + payload_size_bytes + payload_bytes

            print(f"发送请求: {len(message)} 字节")
            await ws.send(message)

            print("等待响应...")
            message = await asyncio.wait_for(ws.recv(), timeout=60.0)

            if isinstance(message, bytes):
                print(f"收到二进制帧: {len(message)} 字节")
                print(f"前16字节: {message[:16].hex()}")

                frame_type = message[2] >> 4
                if frame_type == 0x1:
                    try:
                        json_data = json.loads(message[8:].decode("utf-8"))
                        print(f"JSON响应: {json.dumps(json_data, indent=2)}")
                    except:
                        print(f"无法解析JSON: {message[8:].hex()}")
            elif isinstance(message, str):
                print(f"收到文本消息: {message}")

    except asyncio.TimeoutError:
        print("❌ 接收超时（60秒）")
    except websockets.exceptions.ConnectionClosed as e:
        print(f"❌ 连接关闭: {e}")
    except Exception as e:
        print(f"❌ 错误: {e}")
        import traceback
        traceback.print_exc()


if __name__ == "__main__":
    asyncio.run(test_connection())