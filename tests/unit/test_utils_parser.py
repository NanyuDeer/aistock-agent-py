"""utils.parser 测试 — LLM 意图分类输出解析（从 supervisor._parse_intent 抽出）"""

from aistock_agent.utils.parser import parse_intent


def test_morning_intent():
    result = parse_intent("MORNING", "生成今日晨报")
    assert result == {"intent": "morning", "symbol": None, "tag_code": None}


def test_event_intent():
    result = parse_intent("event", "分析事件")
    assert result["intent"] == "event"


def test_sector_intent_with_tag_code():
    result = parse_intent("SECTOR", "分析白酒板块 BK0475")
    assert result["intent"] == "sector"
    assert result["tag_code"] == "BK0475"


def test_stock_intent_with_symbol():
    result = parse_intent("stock", "分析 600519")
    assert result["intent"] == "stock"
    assert result["symbol"] == "600519"


def test_general_fallback():
    """无法匹配任何关键词时回退 general"""
    result = parse_intent("无法识别的内容", "你好")
    assert result == {"intent": "general", "symbol": None, "tag_code": None}


def test_symbol_extraction_six_digits():
    """从用户消息提取 6 位股票代码"""
    result = parse_intent("stock", "看看 000001 的走势")
    assert result["symbol"] == "000001"


def test_tag_code_case_normalized():
    """BK 代码小写也归一化为大写"""
    result = parse_intent("sector", "分析 bk0475")
    assert result["tag_code"] == "BK0475"


def test_intent_priority_morning_first():
    """if-elif 顺序决定优先级：morning 优先于 stock"""
    result = parse_intent("morning stock", "分析")
    assert result["intent"] == "morning"


def test_llm_output_case_insensitive():
    """LLM 输出大小写不敏感（.lower() 处理）"""
    result = parse_intent("StOcK", "分析 600519")
    assert result["intent"] == "stock"
