"""
Multi-Agent Supervision Experiment — v1 package.

Public API:
- TaskRunner: drives one run
- RunConfig.from_condition: instantiate config per condition
- ConditionEnum: A1-B1 / A1-B2 / A2-A / ... / A2-F
- load_task: resolve task_id to TaskInterface
- make_inspect_solver: Inspect framework wrapper
"""

from .config import RunConfig
from .data_models import (
    ConditionEnum, AttackerVariant, RunRecord,
)
from .task_runner import TaskRunner, make_inspect_solver
from .tasks import load_task, TaskInterface

__all__ = [
    "TaskRunner",
    "RunConfig",
    "ConditionEnum",
    "AttackerVariant",
    "RunRecord",
    "load_task",
    "TaskInterface",
    "make_inspect_solver",
]
