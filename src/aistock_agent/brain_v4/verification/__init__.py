"""到期验证包：append-only 验证记录与错误标签。

对外只暴露只读判定结果；验证器绝不修改原 judgment，也绝不用验证结果反向
改写研究账本。
"""

from .error_tags import ErrorTag, classify_error
from .verifier import (
    ObservedOutcome,
    VerificationRecord,
    Verifier,
    verify_judgment,
)

__all__ = [
    "ErrorTag",
    "ObservedOutcome",
    "VerificationRecord",
    "Verifier",
    "classify_error",
    "verify_judgment",
]
