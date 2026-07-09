"""晨报 Redis 缓存提取脚本

用法:
    # 方式1：直接运行（需先设置 PYTHONPATH 包含 src）
    $env:PYTHONPATH = "src"; python scripts/extract_morning_cache.py

    # 方式2：Windows 批处理（已设置好 PYTHONPATH）
    scripts\extract_morning_cache.bat

    # 提取指定日期的晨报
    $env:PYTHONPATH = "src"; python scripts/extract_morning_cache.py --date 2026-07-09

功能:
    1. 连接 Redis，扫描 briefing:morning:* 缓存 key
    2. 将缓存内容提取并落盘到 docs/agent-outputs/morning/YYYY-MM-DD-briefing.md
    3. 不触发 LLM 重新生成，纯缓存读取
    4. 支持 --date 参数提取指定日期，默认提取所有缓存报告
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import datetime
from pathlib import Path

from aistock_agent.config import settings
from aistock_agent.services.redis_pool import RedisPool

OUTPUT_DIR = Path(__file__).resolve().parent.parent / "docs" / "agent-outputs" / "morning"

# 缓存 key 前缀，与 services/cache.py 中一致
CACHE_KEY_PREFIX = "briefing:morning:"


async def extract_briefing(date_str: str | None = None) -> int:
    """从 Redis 提取晨报缓存并落盘，返回退出码。

    Args:
        date_str: 指定日期（YYYY-MM-DD），为 None 时提取所有缓存的晨报。
    """
    start_at = datetime.now()
    print(f"[extract-morning] 开始提取晨报缓存: {start_at.isoformat()}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    # 初始化 Redis 连接池
    await RedisPool.init(settings.redis_url, max_connections=settings.redis_max_connections)

    try:
        client = await RedisPool.get_client()

        if date_str:
            # 提取指定日期
            cache_key = f"{CACHE_KEY_PREFIX}{date_str}"
            keys = [cache_key]
            print(f"[extract-morning] 指定日期: {date_str}")
        else:
            # 扫描所有 briefing:morning:* key
            keys = []
            async for key in client.scan_iter(match=f"{CACHE_KEY_PREFIX}*", count=100):
                if isinstance(key, bytes):
                    key = key.decode()
                keys.append(key)
            print(f"[extract-morning] 扫描到 {len(keys)} 个缓存 key")

        if not keys:
            print("[extract-morning] 未找到任何晨报缓存（可能缓存已过期或未生成）")
            return 0

        extracted = 0
        skipped = 0

        for key in keys:
            # 从 key 提取日期: briefing:morning:2026-07-09 → 2026-07-09
            key_str = key if isinstance(key, str) else key.decode()
            date_part = key_str.removeprefix(CACHE_KEY_PREFIX)

            cached = await client.get(key_str)
            if not cached:
                print(f"[extract-morning] 跳过 {date_part}: 缓存值为空")
                skipped += 1
                continue

            content = cached.decode() if isinstance(cached, bytes) else str(cached)
            if not content.strip():
                print(f"[extract-morning] 跳过 {date_part}: 内容为空白")
                skipped += 1
                continue

            filename = f"{date_part}-briefing.md"
            output_path = OUTPUT_DIR / filename

            # 检查文件是否已存在且内容相同，避免重复写入
            if output_path.exists():
                existing = output_path.read_text(encoding="utf-8")
                # 去除 frontmatter 比较纯内容
                existing_body = _strip_frontmatter(existing)
                if existing_body.strip() == content.strip():
                    print(f"[extract-morning] 跳过 {date_part}: 文件已存在且内容一致")
                    skipped += 1
                    continue

            # 写入文件，带提取元数据头
            header = _build_header(date_part, start_at)
            output_path.write_text(header + content, encoding="utf-8")
            print(f"[extract-morning] 已提取: {output_path} ({len(content)} 字符)")
            extracted += 1

        end_at = datetime.now()
        duration = (end_at - start_at).total_seconds()
        print(
            f"[extract-morning] 完成: 提取 {extracted} 篇, 跳过 {skipped} 篇, "
            f"耗时 {duration:.2f}s"
        )
        return 0 if extracted > 0 or skipped > 0 else 1

    except Exception as exc:
        print(f"[extract-morning] 提取失败: {exc}", file=sys.stderr)
        return 1
    finally:
        await RedisPool.close()


def _build_header(date_str: str, extracted_at: datetime) -> str:
    """构建文件头元数据。

    Args:
        date_str: 晨报日期（YYYY-MM-DD）。
        extracted_at: 提取时间。
    """
    return f"""---
date: {date_str}
extracted_at: {extracted_at.isoformat()}
source: redis_cache
cache_key: {CACHE_KEY_PREFIX}{date_str}
agent: morning_agent
---

"""


def _strip_frontmatter(text: str) -> str:
    """去除 YAML frontmatter，返回正文部分。

    Args:
        text: 带 frontmatter 的完整文本。
    """
    if not text.startswith("---"):
        return text
    # 找第二个 "---" 作为 frontmatter 结束
    end = text.find("\n---", 3)
    if end == -1:
        return text
    return text[end + 4 :]


def main() -> None:
    parser = argparse.ArgumentParser(description="从 Redis 缓存提取晨报到 docs/agent-outputs/morning/")
    parser.add_argument(
        "--date",
        type=str,
        default=None,
        help="指定日期（YYYY-MM-DD），不指定则提取所有缓存报告",
    )
    args = parser.parse_args()

    # 简单校验日期格式
    if args.date:
        try:
            datetime.strptime(args.date, "%Y-%m-%d")
        except ValueError:
            print(f"[extract-morning] 日期格式无效: {args.date}，应为 YYYY-MM-DD", file=sys.stderr)
            sys.exit(2)

    raise SystemExit(asyncio.run(extract_briefing(args.date)))


if __name__ == "__main__":
    main()
