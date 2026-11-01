"""HW2：Simple Workflow MVP —— 多 Agent 协作的 Workflow 编排框架。

公开 API：

- 核心：``Message`` / ``MessageBus`` / ``SharedState`` / ``EventLog`` / ``BaseAgent``
- 引擎：``Workflow`` / ``Node`` / ``Branch`` / ``Loop`` / ``Parallel``
- Agent：``RequirementAgent`` / ``DesignAgent`` / ``CodingAgent`` /
  ``ReviewAgent`` / ``FixAgent``
- 流程：``run_mini_se`` / ``run_review_fix`` / ``run_demo``
"""
from .core import BaseAgent, EventLog, Message, MessageBus, SharedState
from .engine import Branch, Loop, Node, Parallel, StepResult, Workflow
from .agents import (
    CodingAgent,
    DesignAgent,
    FixAgent,
    RequirementAgent,
    ReviewAgent,
    WorkflowAgentBase,
)
from .flows import (
    build_mini_se_flow,
    build_review_fix_flow,
    run_demo,
    run_mini_se,
    run_review_fix,
)

__version__ = "1.0.0"

__all__ = [
    "Message", "MessageBus", "SharedState", "EventLog", "BaseAgent",
    "Workflow", "Node", "Branch", "Loop", "Parallel", "StepResult",
    "RequirementAgent", "DesignAgent", "CodingAgent", "ReviewAgent", "FixAgent",
    "WorkflowAgentBase",
    "run_mini_se", "run_review_fix", "run_demo",
    "build_mini_se_flow", "build_review_fix_flow",
]
