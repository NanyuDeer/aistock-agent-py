# tests/unit/test_chat_qa_metrics.py
"""CHAT QA 链路监控指标单元测试。"""
from aistock_agent.observability.metrics import MetricsCollector


def test_record_chat_qa_latency_qa_router():
    mc = MetricsCollector()
    mc.record_chat_qa_latency("qa_router", 100)
    mc.record_chat_qa_latency("qa_router", 200)
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["qa_router_latency_ms_avg"] == 150.0


def test_record_chat_qa_latency_synth_answer():
    mc = MetricsCollector()
    mc.record_chat_qa_latency("synth_answer", 500)
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["synth_latency_ms_avg"] == 500.0


def test_record_chat_qa_latency_e2e():
    mc = MetricsCollector()
    mc.record_chat_qa_latency("e2e", 1000)
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["e2e_latency_ms_avg"] == 1000.0


def test_record_chat_qa_latency_unknown_node_ignored():
    mc = MetricsCollector()
    mc.record_chat_qa_latency("unknown_node", 100)
    metrics = mc.get_metrics()
    # 未知节点不应影响已知节点的平均值
    assert metrics["chat_qa"]["qa_router_latency_ms_avg"] == 0.0


def test_record_skill_latency_by_name():
    mc = MetricsCollector()
    mc.record_skill_latency("report_lookup", 50)
    mc.record_skill_latency("report_lookup", 70)
    mc.record_skill_latency("stock_snapshot", 30)
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["skill_latency_ms_avg"]["report_lookup"] == 60.0
    assert metrics["chat_qa"]["skill_latency_ms_avg"]["stock_snapshot"] == 30.0


def test_record_skill_degraded_by_name():
    mc = MetricsCollector()
    mc.record_skill_degraded("stock_snapshot")
    mc.record_skill_degraded("stock_snapshot")
    mc.record_skill_degraded("stock_news")
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["skill_degraded_total"]["stock_snapshot"] == 2
    assert metrics["chat_qa"]["skill_degraded_total"]["stock_news"] == 1


def test_record_synth_degraded():
    mc = MetricsCollector()
    mc.record_synth_degraded()
    mc.record_synth_degraded()
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["synth_degraded_total"] == 2


def test_reset_clears_chat_qa_metrics():
    mc = MetricsCollector()
    mc.record_chat_qa_latency("qa_router", 100)
    mc.record_skill_latency("report_lookup", 50)
    mc.record_skill_degraded("stock_snapshot")
    mc.record_synth_degraded()
    mc.reset()
    metrics = mc.get_metrics()
    assert metrics["chat_qa"]["qa_router_latency_ms_avg"] == 0.0
    assert metrics["chat_qa"]["skill_latency_ms_avg"] == {}
    assert metrics["chat_qa"]["skill_degraded_total"] == {}
    assert metrics["chat_qa"]["synth_degraded_total"] == 0


def test_get_metrics_preserves_existing_llm_fields():
    """扩展后的 get_metrics 仍保留原有 LLM/工具字段。"""
    mc = MetricsCollector()
    mc.record_llm_start()
    mc.record_llm_tokens(prompt_tokens=10, completion_tokens=5, total_tokens=15)
    metrics = mc.get_metrics()
    assert metrics["llm_calls"] == 1
    assert metrics["prompt_tokens"] == 10
    assert metrics["total_tokens"] == 15
    assert "chat_qa" in metrics
