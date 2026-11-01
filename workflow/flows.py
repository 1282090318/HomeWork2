"""两条演示流程：

1. **Mini SE 流程**（需求 → 设计 → 代码 → 并行审查）：
   顺序执行三个 Agent，再并行审查生成的每个源码文件；
2. **代码审查修复流程**（编码 → 审查 → 修复）：
   循环执行「审查 → 修复」，直到没有可修复问题或达到最大迭代次数。

每个流程运行后都会把执行日志、状态快照、Mermaid 流程图
保存到 ``artifacts_dir``，便于追踪与复现。
"""
from __future__ import annotations

import json
import os
import shutil
from typing import Any, Dict, List, Optional

from .agents import (
    CodingAgent,
    DesignAgent,
    FixAgent,
    RequirementAgent,
    ReviewAgent,
)
from .core import EventLog, MessageBus, SharedState
from .engine import Node, Workflow

# 默认演示需求（examples/requirement.txt 的备选内置值）
DEFAULT_REQUIREMENT = (
    "开发一个个人任务管理小工具。"
    "必须支持任务的创建、删除和查询。"
    "需要支持按优先级统计任务数量。"
    "响应时间不超过 1 秒。"
    "可选：支持任务提醒。"
)

# 演示用“问题代码”（与 HW1 示例一致：缺 docstring / 裸 except / 硬编码密钥）
DEMO_BUGGY_CODE = '''def add(a, b):
    return a + b


def load_config():
    password = "123456"
    api_key = "sk-abcdef123456"
    return {"password": password, "api_key": api_key}


def safe_div(a, b):
    try:
        return a / b
    except:
        return None


def main():
    # TODO: 补充命令行参数解析
    print(add(1, 2))
'''


def _make_agents(bus: MessageBus, log: EventLog):
    return {
        "requirement": RequirementAgent(bus=bus, log=log),
        "design": DesignAgent(bus=bus, log=log),
        "coding": CodingAgent(bus=bus, log=log),
        "review": ReviewAgent(bus=bus, log=log),
        "fix": FixAgent(bus=bus, log=log),
    }


def _save_artifacts(
    artifacts_dir: str,
    flow_name: str,
    state: SharedState,
    log: EventLog,
    bus: MessageBus,
    wf: Workflow,
) -> None:
    os.makedirs(artifacts_dir, exist_ok=True)
    with open(os.path.join(artifacts_dir, f"{flow_name}_log.txt"), "w", encoding="utf-8") as fh:
        fh.write(log.to_text() + "\n")
    snapshot = {k: v for k, v in state.snapshot().items() if k != "workflow"}
    with open(os.path.join(artifacts_dir, f"{flow_name}_state.json"), "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2, default=str)
    with open(os.path.join(artifacts_dir, f"{flow_name}_flow.md"), "w", encoding="utf-8") as fh:
        fh.write(f"# {flow_name} 流程图\n\n```mermaid\n{wf.to_mermaid()}\n```\n")
    messages = [m.to_dict() for m in bus.history]
    with open(os.path.join(artifacts_dir, f"{flow_name}_messages.json"), "w", encoding="utf-8") as fh:
        json.dump(messages, fh, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# 流程一：Mini SE（需求 → 设计 → 代码 → 并行审查）
# ---------------------------------------------------------------------------
def build_mini_se_flow(agents: Dict[str, Any]) -> Workflow:
    wf = Workflow("mini-se")
    wf.add("需求分析", agents["requirement"], output_key="requirements")
    wf.add("架构设计", agents["design"], output_key="design")
    wf.add("编码实现", agents["coding"], output_key="code")
    return wf


def run_mini_se(
    requirement: str,
    artifacts_dir: str = "output/mini-se",
    with_review: bool = True,
) -> Dict[str, Any]:
    """运行 Mini SE 流程，返回最终状态与日志。"""
    bus, log = MessageBus(), EventLog()
    agents = _make_agents(bus, log)
    state = SharedState({"requirement": requirement, "artifacts_dir": artifacts_dir})

    wf = build_mini_se_flow(agents)
    wf.run(state, log)

    if with_review:
        _parallel_review(state, log, agents, artifacts_dir)

    _save_artifacts(artifacts_dir, "mini_se", state, log, bus, wf)
    return {"state": state, "log": log, "bus": bus}


def _parallel_review(
    state: SharedState,
    log: EventLog,
    agents: Dict[str, Any],
    artifacts_dir: str,
) -> None:
    """并行审查生成目录下的每个 .py 文件（演示并行执行）。"""
    gen_dir = os.path.join(artifacts_dir, "generated")
    if not os.path.isdir(gen_dir):
        return
    files = [f for f in os.listdir(gen_dir) if f.endswith(".py")]
    if not files:
        return

    def review_one(path: str):
        def _fn(st: SharedState) -> Dict[str, Any]:
            return agents["review"](SharedState({"review_target": path}))

        return _fn

    wf = Workflow("parallel-review")
    wf.add_parallel(
        "并行审查",
        [Node(f"审查 {f}", review_one(os.path.join(gen_dir, f))) for f in files],
        output_key="reviews",  # 由引擎聚合并行结果，避免多线程竞争
    )
    wf.run(state, log)


# ---------------------------------------------------------------------------
# 流程二：代码审查修复（审查 → 修复，循环直到无可修复问题）
# ---------------------------------------------------------------------------
def build_review_fix_flow(agents: Dict[str, Any]) -> Workflow:
    """构造「审查 → 修复」循环流程。"""
    body = Workflow("review-fix-body")
    body.add("代码审查", agents["review"], output_key="review")
    body.add("自动修复", agents["fix"], output_key="fix")

    wf = Workflow("review-fix")
    wf.add_loop(
        "审查-修复循环",
        condition=lambda s: s.get("review") is None
        or any(i.get("fixable") for i in s.get("review", {}).get("issues", [])),
        body=body,
        max_iters=3,
    )
    return wf


def run_review_fix(
    target_path: Optional[str] = None,
    artifacts_dir: str = "output/review-fix",
) -> Dict[str, Any]:
    """运行审查修复流程。默认使用内置问题代码（复制到 artifacts 后再修）。"""
    bus, log = MessageBus(), EventLog()
    agents = _make_agents(bus, log)

    os.makedirs(artifacts_dir, exist_ok=True)
    if target_path:
        dst = os.path.join(artifacts_dir, os.path.basename(target_path))
        shutil.copyfile(target_path, dst)  # 复制后再修，避免改动原始文件
    else:
        dst = os.path.join(artifacts_dir, "demo_buggy.py")
        with open(dst, "w", encoding="utf-8") as fh:
            fh.write(DEMO_BUGGY_CODE)

    state = SharedState({"review_target": dst})
    wf = build_review_fix_flow(agents)
    wf.run(state, log)

    _save_artifacts(artifacts_dir, "review_fix", state, log, bus, wf)
    return {"state": state, "log": log, "bus": bus}


def run_demo(artifacts_root: str = "output") -> Dict[str, Any]:
    """一键运行两条演示流程。"""
    return {
        "mini_se": run_mini_se(DEFAULT_REQUIREMENT, os.path.join(artifacts_root, "mini-se")),
        "review_fix": run_review_fix(None, os.path.join(artifacts_root, "review-fix")),
    }
