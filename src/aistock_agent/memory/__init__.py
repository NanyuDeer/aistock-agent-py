"""持久化记忆模块 — LangGraph checkpointer + 会话存储 + 用户偏好

子模块：
- checkpointer：多轮对话状态恢复（MemorySaver 默认，sqlite/redis 可选降级）
- session_store：会话消息历史（Redis，key=session:{id}:messages）
- preferences：用户自选股偏好（Redis，key=user:{id}:favorites）
"""
