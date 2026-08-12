# 待提交修改记录（changelog-pending）

## 2026-08-12 — 统一事件抓取中台遗留 Minor 修复

- `src/aistock_agent/api/routes.py`：`event_scrape_list` / `event_scrape_by_symbol` 改用 `date = _validate_scrape_date(date)` 接收返回值（消除丢弃返回值歧义）
- `src/aistock_agent/services/event_scrape_sources.py`：`_event_shanghai_date` 非 Z 分支先判断 `dt.tzinfo is not None`，带显式偏移（如 +00:00）时 `astimezone(Asia/Shanghai)` 换算墙钟，否则 `replace(tzinfo=Asia/Shanghai)`（Final review Minor-1）
- `src/aistock_agent/agents/workers/morning.py`：`_event_records_to_major_events` docstring 去除行号引用 ":690-728"，改为注入路径过滤语义描述（Final review Minor-2）
- `tests/unit/test_scheduler_event_scrape.py`：交易日/盘中/异常用例补 `scheduler.logger` patch，断言 `event_scrape_job_done` 成功日志恰好一次、`event_scrape_job_failed` 失败日志不出现（Task 3 I1 回归保护）
- `tests/unit/test_event_scrape_query.py`：补 `test_scrape_list_degrades_on_node_error`（node 异常 → 路由 200 空列表，Task 6）
- `tests/unit/test_event_scrape_sources.py`：补 `test_collect_eastmoney_judgements_explicit_utc_offset_converted`（带 +00:00 偏移用例，Final review Minor-1）
- `tests/unit/test_event_scraper_conduction.py`：docstring 去除行号引用 "event_scraper.py:86-87"（Final review Minor-2）
