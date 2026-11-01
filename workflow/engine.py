"""Workflow 引擎：顺序执行、条件分支、循环、并行，以及重试与错误恢复。

引擎不关心 Agent 的内部实现，只负责**编排**：

- ``Node``      ：叶子节点，包装一次 Agent 调用；
- ``Branch``    ：条件分支（if/else）；
- ``Loop``      ：循环（while cond and iter < max_iters）；
- ``Parallel``  ：并行执行（线程池，多个无依赖节点同时跑）；
- ``Workflow``  ：由上述节点组成，``run`` 时驱动整个流程。

错误处理策略（``error_policy``）：

- ``raise``    ：任一节点失败立即中止（默认，快速失败）；
- ``continue`` ：记录错误到 ``state["errors"]`` 后继续（容错执行）。

每个节点支持 ``retries`` 次重试（间隔线性退避）。
"""
from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any, Callable, List, Optional

from .core import EventLog, SharedState

StateFn = Callable[[SharedState], Any]


def _node_label(node: Any) -> str:
    """节点在 Mermaid 图中的显示名。"""
    return getattr(node, "name", type(node).__name__)


# ---------------------------------------------------------------------------
# 执行结果
# ---------------------------------------------------------------------------
@dataclass
class StepResult:
    """单个节点的执行结果。"""

    node: str
    ok: bool
    output: Any = None
    error: Optional[str] = None
    attempts: int = 1
    duration_ms: float = 0.0


# ---------------------------------------------------------------------------
# 叶子节点
# ---------------------------------------------------------------------------
class Node:
    """包装一次可调用（通常是某个 Agent 的处理函数）。"""

    def __init__(
        self,
        name: str,
        fn: StateFn,
        output_key: Optional[str] = None,
        retries: int = 0,
    ) -> None:
        self.name = name
        self.fn = fn
        self.output_key = output_key
        self.retries = max(0, retries)

    def run(self, state: SharedState, log: EventLog, policy: str = "raise") -> StepResult:
        log.node_start(self.name)
        started = time.perf_counter()
        attempts = 0
        last_err: Optional[Exception] = None

        while attempts <= self.retries:
            attempts += 1
            try:
                output = self.fn(state)
                if self.output_key:
                    state.set(self.output_key, output, source=self.name)
                duration = (time.perf_counter() - started) * 1000
                log.node_end(self.name, f"成功（{duration:.0f}ms，第 {attempts} 次尝试）")
                return StepResult(self.name, True, output, attempts=attempts, duration_ms=duration)
            except Exception as exc:  # noqa: BLE001 - 引擎需要兜底所有异常
                last_err = exc
                if attempts <= self.retries:
                    time.sleep(0.2 * attempts)  # 线性退避

        duration = (time.perf_counter() - started) * 1000
        log.error(self.name, f"失败（重试 {self.retries} 次后）：{last_err}")
        state.append_error(self.name, str(last_err))
        if policy == "raise":
            raise RuntimeError(f"节点 [{self.name}] 执行失败：{last_err}") from last_err
        return StepResult(self.name, False, None, error=str(last_err), attempts=attempts, duration_ms=duration)


# ---------------------------------------------------------------------------
# 结构节点：分支 / 循环 / 并行
# ---------------------------------------------------------------------------
class Branch:
    """条件分支：``condition(state)`` 为真走 ``then_node``，否则走 ``else_node``。"""

    def __init__(
        self,
        name: str,
        condition: Callable[[SharedState], bool],
        then_node: Any,
        else_node: Optional[Any] = None,
    ) -> None:
        self.name = name
        self.condition = condition
        self.then_node = then_node
        self.else_node = else_node

    def run(self, state: SharedState, log: EventLog, policy: str = "raise") -> StepResult:
        taken = self.then_node if self.condition(state) else self.else_node
        label = "then" if taken is self.then_node else "else"
        log.node_start(self.name)
        if taken is None:
            log.node_end(self.name, f"条件为假且无 else 分支，跳过（{label}）")
            return StepResult(self.name, True, output=None)
        # 先执行子节点再记 node_end，保证父节点的结束时间不早于子节点
        result = taken.run(state, log, policy)
        log.node_end(
            self.name,
            f"走 {label} 分支：{getattr(taken, 'name', taken)}"
            f"（{'成功' if result.ok else '失败'}）",
        )
        return StepResult(
            self.name, result.ok, output=result.output, error=result.error, attempts=result.attempts
        )


class Loop:
    """循环：每次迭代前评估 ``condition(state)``，最多 ``max_iters`` 次。"""

    def __init__(
        self,
        name: str,
        condition: Callable[[SharedState], bool],
        body: "Workflow",
        max_iters: int = 3,
    ) -> None:
        self.name = name
        self.condition = condition
        self.body = body
        self.max_iters = max_iters

    def run(self, state: SharedState, log: EventLog, policy: str = "raise") -> StepResult:
        log.node_start(self.name)
        iterations = 0
        while iterations < self.max_iters and self.condition(state):
            iterations += 1
            # 迭代进度用独立事件类型记录，避免和节点结束事件混在一起造成时序错乱
            log.record("iteration", self.name, f"第 {iterations}/{self.max_iters} 次迭代")
            state.set(f"{self.name}#iter", iterations, source=self.name)
            self.body.run(state, log, policy)
        log.node_end(self.name, f"循环结束，共 {iterations} 次迭代")
        return StepResult(self.name, True, output=iterations)


class Parallel:
    """并行执行一组无依赖关系的节点（线程池）。

    ``output_key``：把各节点的输出聚合为列表写入共享状态
    （由引擎统一收集，避免多线程读写同一状态键的竞态）。
    """

    def __init__(self, name: str, nodes: List[Node], output_key: Optional[str] = None) -> None:
        self.name = name
        self.nodes = nodes
        self.output_key = output_key

    def run(self, state: SharedState, log: EventLog, policy: str = "raise") -> StepResult:
        log.node_start(self.name)
        results: List[StepResult] = []
        with ThreadPoolExecutor(max_workers=max(1, len(self.nodes))) as pool:
            futures = [pool.submit(n.run, state, log, policy) for n in self.nodes]
            for fut in futures:
                results.append(fut.result())
        ok = all(r.ok for r in results)
        if self.output_key:
            state.set(self.output_key, [r.output for r in results], source=self.name)
        log.node_end(self.name, f"{len(results)} 个节点并行完成，全部成功={ok}")
        return StepResult(self.name, ok, output=results)


# ---------------------------------------------------------------------------
# Workflow
# ---------------------------------------------------------------------------
class Workflow:
    """由节点组成的有向流程。

    用法::

        wf = Workflow("mini-se")
        wf.add("需求分析", requirement_fn, output_key="requirements")
        wf.add("架构设计", design_fn, output_key="design")
        wf.add("编码实现", coding_fn, output_key="code")
        final_state = wf.run(state, log)
    """

    def __init__(self, name: str, error_policy: str = "raise") -> None:
        self.name = name
        self.error_policy = error_policy
        self.steps: List[Any] = []

    # -- 构建 API -----------------------------------------------------------
    def add(
        self,
        name: str,
        fn: StateFn,
        output_key: Optional[str] = None,
        retries: int = 0,
    ) -> Node:
        node = Node(name, fn, output_key=output_key, retries=retries)
        self.steps.append(node)
        return node

    def add_branch(
        self,
        name: str,
        condition: Callable[[SharedState], bool],
        then_node: Any,
        else_node: Optional[Any] = None,
    ) -> Branch:
        branch = Branch(name, condition, then_node, else_node)
        self.steps.append(branch)
        return branch

    def add_loop(
        self,
        name: str,
        condition: Callable[[SharedState], bool],
        body: "Workflow",
        max_iters: int = 3,
    ) -> Loop:
        loop = Loop(name, condition, body, max_iters=max_iters)
        self.steps.append(loop)
        return loop

    def add_parallel(
        self, name: str, nodes: List[Node], output_key: Optional[str] = None
    ) -> Parallel:
        par = Parallel(name, nodes, output_key=output_key)
        self.steps.append(par)
        return par

    # -- 执行 ---------------------------------------------------------------
    def run(
        self,
        state: Optional[SharedState] = None,
        log: Optional[EventLog] = None,
        policy: Optional[str] = None,
    ) -> SharedState:
        """执行整个流程。

        ``policy``：错误策略覆盖项（``Loop`` 会把外层策略透传给循环体）；
        缺省用本流程自己的 ``error_policy``。

        注意：消息总线由 Agent 自己持有（``BaseAgent.bus``），引擎只负责编排，
        因此这里不再接受一个用不到的 ``bus`` 参数。
        """
        state = SharedState() if state is None else state
        log = EventLog() if log is None else log
        effective_policy = policy or self.error_policy
        log.record("node_start", f"Workflow[{self.name}]", "流程启动")
        for step in self.steps:
            step.run(state, log, effective_policy)
        log.record("node_end", f"Workflow[{self.name}]", "流程结束")
        state.set("workflow", self.name, source="engine")
        return state

    # -- 可视化（Mermaid 流程图，供文档使用）---------------------------------
    def to_mermaid(self) -> str:
        """导出 Mermaid 流程图。

        会递归展开循环体、分支与并行子节点——否则 ``graph`` 输出里整条
        审查-修复流程只会剩下一个孤零零的循环节点。
        """
        lines = ["flowchart TD"]
        self._emit_steps(self.steps, "S", lines)
        return "\n".join(lines)

    def _emit_steps(self, steps: List[Any], prefix: str, lines: List[str]) -> List[str]:
        """递归展开一层节点，返回该层的节点 id 序列（用于连线）。"""
        ids: List[str] = []
        for i, step in enumerate(steps):
            sid = f"{prefix}{i}"
            if isinstance(step, Branch):
                lines.append(f'    {sid}{{"{step.name}?"}}')
                then_id = f"{sid}_then"
                lines.append(f'    {then_id}["{_node_label(step.then_node)}"]')
                lines.append(f"    {sid} -- 真 --> {then_id}")
                if step.else_node is not None:
                    else_id = f"{sid}_else"
                    lines.append(f'    {else_id}["{_node_label(step.else_node)}"]')
                    lines.append(f"    {sid} -- 假 --> {else_id}")
            elif isinstance(step, Loop):
                lines.append(f'    {sid}[["{step.name}"]]')
                body_ids = self._emit_steps(step.body.steps, f"{sid}_", lines)
                if body_ids:
                    lines.append(f"    {sid} --> {body_ids[0]}")
                    lines.append(f"    {body_ids[-1]} -. 条件仍成立 .-> {sid}")
            elif isinstance(step, Parallel):
                lines.append(f'    {sid}[("{step.name}")]')
                for j, sub in enumerate(step.nodes):
                    sub_id = f"{sid}_p{j}"
                    lines.append(f'    {sub_id}["{_node_label(sub)}"]')
                    lines.append(f"    {sid} --> {sub_id}")
            else:
                lines.append(f'    {sid}["{_node_label(step)}"]')
            ids.append(sid)
            if len(ids) > 1:
                lines.append(f"    {ids[-2]} --> {sid}")
        return ids
