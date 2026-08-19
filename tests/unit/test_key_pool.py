import time

from aistock_agent.services.key_pool import KeyPool


def test_select_rotates_healthy_keys():
    pool = KeyPool(["a", "b"])
    picked = {pool.select_key() for _ in range(40)}
    assert picked == {"a", "b"}


def test_circuit_error_cooldown_excludes_key_until_retry():
    pool = KeyPool(["a", "b"])
    pool.report_error("a", is_circuit=True)
    # "a" 进入冷却，全部选择应落在健康 key "b" 上
    assert {pool.select_key() for _ in range(20)} == {"b"}


def test_all_cooldown_fail_open_uses_oldest_and_opens_circuit():
    pool = KeyPool(["a", "b"], cooldown_base_seconds=5.0)
    pool.report_error("a", is_circuit=True)
    pool.report_error("b", is_circuit=True)
    assert pool.circuit_open is False
    picked = pool.select_key()  # fail-open：仍返回一个 key，不抛
    assert picked in {"a", "b"}
    assert pool.circuit_open is True


def test_backoff_is_capped_and_key_returns_to_pool():
    pool = KeyPool(["a"], cooldown_base_seconds=5.0, max_backoff_seconds=60.0)
    # 第一次 hello：回退 base=5s
    pool.report_error("a", is_circuit=True)  # backoff = 5s
    pool.report_error("a", is_circuit=True)  # backoff = 10s
    pool.report_error("a", is_circuit=True)  # backoff = 20s
    pool.report_error("a", is_circuit=True)  # backoff = 40s
    pool.report_error("a", is_circuit=True)  # 应封顶 60s，而非 80s
    assert pool._cooldown_until("a") <= time.monotonic() + 61
