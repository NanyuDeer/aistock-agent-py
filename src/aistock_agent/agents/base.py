"""双模型工厂 — quick_think / deep_think

根据用途选择不同模型：
- quick_think：意图分类/路由，低延迟低成本
- deep_think：深度分析/晨报/事件，推理质量优先
"""

from langchain_openai import ChatOpenAI

from aistock_agent.config import settings


def get_quick_think() -> ChatOpenAI:
    """快速模型，用于意图分类和简单任务"""
    return ChatOpenAI(
        model=settings.quick_think_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.1,
    )


def get_deep_think() -> ChatOpenAI:
    """深度模型，用于复杂分析和推理"""
    return ChatOpenAI(
        model=settings.deep_think_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.3,
    )
