"""Application services — wire ``domain`` + ``transport`` + ``core.repos`` together.

A service owns its dependencies as constructor arguments and exposes a small
API to the ``ui`` layer. Services depend on ``domain`` and ``transport``;
``domain`` and ``transport`` do not depend on services.
"""

from telos.services.agent_runtime import (
    AgentConfigSeedError,
    AgentRuntime,
    RecordatioUnavailableError,
)
from telos.services.cost_rollup import CostRollupService
from telos.services.email_notifications import EmailNotificationService
from telos.services.goal_costs import housekeeper_costs, make_goal_gen_cost_recorder
from telos.services.improvement import AgentImprovementState, ImprovementService
from telos.services.routine_execution import RoutineExecutor
from telos.services.scheduler import BackgroundScheduler, SchedulerShutdownError

__all__ = [
    "AgentConfigSeedError",
    "AgentImprovementState",
    "AgentRuntime",
    "BackgroundScheduler",
    "CostRollupService",
    "EmailNotificationService",
    "ImprovementService",
    "RecordatioUnavailableError",
    "RoutineExecutor",
    "SchedulerShutdownError",
    "housekeeper_costs",
    "make_goal_gen_cost_recorder",
]
