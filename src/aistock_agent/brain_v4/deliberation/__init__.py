"""V4 案卷制议事层。

导出协作路由、议程、席位、同伴摘要、信念更新、收敛门禁、单写提交与裁判。
图装配在 `graph.py`，延迟导入以避免包级循环依赖。
"""

from .agenda import build_agenda, merge_submissions
from .beliefs import (
    BeliefUpdateResult,
    BeliefValidation,
    belief_update_from_peer_brief,
    make_hold_update,
    validate_belief_update,
)
from .committer import CommitResult, RevisionCommitter
from .convergence import (
    ConvergenceDecision,
    InvalidObjectionError,
    evaluate_convergence,
    validate_objection,
)
from .judge import CouncilJudge, EvidenceNotFrozenError
from .peer_briefs import build_peer_briefs
from .roles import RoleContext, build_role_context, run_role_node, select_roles
from .router import CollaborationRoute, route_collaboration

__all__ = [
    "BeliefUpdateResult",
    "BeliefValidation",
    "CollaborationRoute",
    "CommitResult",
    "ConvergenceDecision",
    "CouncilJudge",
    "EvidenceNotFrozenError",
    "InvalidObjectionError",
    "RevisionCommitter",
    "RoleContext",
    "belief_update_from_peer_brief",
    "build_agenda",
    "build_peer_briefs",
    "build_role_context",
    "evaluate_convergence",
    "make_hold_update",
    "merge_submissions",
    "route_collaboration",
    "run_role_node",
    "select_roles",
    "validate_belief_update",
    "validate_objection",
]
