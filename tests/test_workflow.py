"""HW2-Workflow 单元测试。

覆盖：
- 引擎控制流：顺序 / 条件分支 / 循环 / 并行；
- 基础设施：消息总线、共享状态、事件日志；
- 端到端：Mini SE 流程（离线）与审查修复流程（离线）。
"""
from __future__ import annotations

import ast
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# 测试固定走离线模式：不依赖网络与 API Key，保证快速、可复现
os.environ.setdefault("CODEAGENT_OFFLINE", "1")

from workflow.core import EventLog, Message, MessageBus, SharedState
from workflow.engine import Branch, Loop, Node, Parallel, Workflow
from workflow.agents import (
    CodingAgent,
    DesignAgent,
    FixAgent,
    RequirementAgent,
    ReviewAgent,
)
from workflow.flows import run_mini_se, run_review_fix


# ---------------------------------------------------------------------------
# 引擎控制流
# ---------------------------------------------------------------------------
class TestEngineControlFlow(unittest.TestCase):
    def test_sequential(self):
        """顺序执行：节点按序运行，结果写入共享状态。"""
        wf = Workflow("seq")
        order = []
        wf.add("A", lambda s: order.append("A") or "a", output_key="a")
        wf.add("B", lambda s: order.append("B") or "b", output_key="b")
        state = wf.run(SharedState(), EventLog())
        self.assertEqual(order, ["A", "B"])
        self.assertEqual(state.get("a"), "a")
        self.assertEqual(state.get("b"), "b")

    def test_branch(self):
        """条件分支：按状态选择 then / else。"""
        wf = Workflow("branch")
        wf.add_branch(
            "判断",
            condition=lambda s: s.get("flag"),
            then_node=Node("then", lambda s: "T", output_key="picked"),
            else_node=Node("else", lambda s: "F", output_key="picked"),
        )
        state = wf.run(SharedState({"flag": False}), EventLog())
        self.assertEqual(state.get("picked"), "F")

    def test_branch_skip(self):
        """条件为假且无 else：跳过不报错。"""
        wf = Workflow("branch-skip")
        wf.add_branch(
            "判断",
            condition=lambda s: False,
            then_node=Node("then", lambda s: "T"),
        )
        state = wf.run(SharedState(), EventLog())
        self.assertIsNone(state.get("picked"))

    def test_loop(self):
        """循环：条件满足时迭代，达到 max_iters 停止。"""
        body = Workflow("body")
        body.add("递增", lambda s: s.set("n", s.get("n", 0) + 1), )
        wf = Workflow("loop")
        wf.add_loop("循环", condition=lambda s: s.get("n", 0) < 5, body=body, max_iters=3)
        state = wf.run(SharedState(), EventLog())
        self.assertEqual(state.get("n"), 3)  # 最多 3 次

    def test_loop_exit_by_condition(self):
        """循环：条件变假时提前退出。"""
        body = Workflow("body")
        body.add("递增", lambda s: s.set("n", s.get("n", 0) + 1))
        wf = Workflow("loop")
        wf.add_loop("循环", condition=lambda s: s.get("n", 0) < 2, body=body, max_iters=10)
        state = wf.run(SharedState(), EventLog())
        self.assertEqual(state.get("n"), 2)

    def test_parallel(self):
        """并行：所有节点都执行，结果完整聚合。"""
        def work(name):
            return lambda s: name

        wf = Workflow("par")
        wf.add_parallel(
            "并行",
            [Node(f"N{i}", work(i), ) for i in range(5)],
            output_key="results",
        )
        state = wf.run(SharedState(), EventLog())
        self.assertEqual(sorted(state.get("results")), [0, 1, 2, 3, 4])

    def test_retry_and_error_policy(self):
        """重试与错误策略：continue 模式下错误被记录不中断。"""
        calls = {"n": 0}

        def flaky(s):
            calls["n"] += 1
            if calls["n"] < 2:
                raise ValueError("第一次失败")
            return "ok"

        wf = Workflow("retry", error_policy="continue")
        wf.add("flaky", flaky, retries=1, output_key="r")
        state = wf.run(SharedState(), EventLog())
        self.assertEqual(state.get("r"), "ok")
        self.assertEqual(calls["n"], 2)

        wf2 = Workflow("fail", error_policy="continue")
        wf2.add("bad", lambda s: 1 / 0)
        state2 = wf2.run(SharedState(), EventLog())
        self.assertTrue(any(e["node"] == "bad" for e in state2.get("errors", [])))


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------
class TestInfrastructure(unittest.TestCase):
    def test_message_bus(self):
        """消息总线：订阅-发布-记录历史。"""
        bus = MessageBus()
        received = []

        class Echo(RequirementAgent):
            name = "echo"

            def process(self, message, state):
                received.append(message)
                return self.send("reply", {"got": message.kind})

        echo = Echo(bus=bus)
        echo.subscribe_to("requirement")
        bus.publish(Message(sender="up", receiver="echo", kind="requirement", payload={}))
        self.assertEqual(len(received), 1)
        self.assertEqual(len(bus.history), 1)

    def test_shared_state(self):
        """共享状态：读写、快照、变更历史。"""
        st = SharedState({"a": 1})
        st.set("b", 2, source="t")
        self.assertEqual(st.snapshot(), {"a": 1, "b": 2})
        self.assertEqual(st.changes[-1]["source"], "t")

    def test_event_log(self):
        """事件日志：记录并可导出文本。"""
        log = EventLog()
        log.node_start("X")
        log.node_end("X")
        text = log.to_text()
        self.assertIn("X", text)
        self.assertEqual(len(log), 2)

    def test_mermaid(self):
        """流程图导出为 Mermaid 文本。"""
        wf = Workflow("g")
        wf.add("A", lambda s: None)
        wf.add_branch("B", lambda s: True, then_node=Node("t", lambda s: None))
        mm = wf.to_mermaid()
        self.assertIn("flowchart TD", mm)
        self.assertIn('S0["A"]', mm)


# ---------------------------------------------------------------------------
# 回归测试：真实 Agent 的消息投递、修复副作用、流程边界
# ---------------------------------------------------------------------------
class TestRegressionFixes(unittest.TestCase):
    def test_message_bus_delivers_to_real_agent(self):
        """真实 Agent（不只是测试替身）订阅后能收到消息并回复。"""
        bus, log = MessageBus(), EventLog()
        agent = RequirementAgent(bus=bus, log=log)
        agent.subscribe_to("requirement")

        replies = bus.publish(
            Message(
                sender="upstream",
                receiver="requirement_agent",
                kind="requirement",
                payload={"result": {"requirement": "开发一个任务管理工具，必须支持任务的创建。"}},
            )
        )
        self.assertEqual(len(replies), 1)
        self.assertEqual(replies[0].kind, "requirement")
        self.assertTrue(replies[0].payload["result"]["features"])

    def test_fix_keeps_module_docstring(self):
        """修复模块级硬编码值时，不能破坏模块 docstring。"""
        source = (
            '"""模块文档：示例。"""\n'
            "\n"
            'db_secret = "s3cr3t"\n'
            "\n"
            "def load():\n"
            '    """读取配置。"""\n'
            "    return db_secret\n"
        )
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "mod.py")
            with open(path, "w", encoding="utf-8") as fh:
                fh.write(source)

            state = SharedState(
                {
                    "review": {
                        "target": path,
                        "issues": [{"type": "hardcoded_secret", "line": 3, "fixable": True}],
                    }
                }
            )
            result = FixAgent().execute(state)
            self.assertEqual(result["fixed_count"], 1)
            self.assertTrue(result["notes"])  # 语义变更要留痕

            with open(path, encoding="utf-8") as fh:
                fixed = fh.read()
            self.assertGreater(fixed.index("import os"), fixed.index('"""'))  # import 在 docstring 之后
            self.assertEqual(ast.get_docstring(ast.parse(fixed)), "模块文档：示例。")

    def test_loop_stops_at_max_iters(self):
        """条件恒为真时，循环必须在 max_iters 处停止。"""
        body = Workflow("body")
        body.add("inc", lambda s: s.set("n", (s.get("n") or 0) + 1))

        wf = Workflow("loop")
        wf.add_loop("恒真循环", lambda s: True, body, max_iters=2)
        state = wf.run(SharedState({"n": 0}), EventLog())

        self.assertEqual(state.get("n"), 2)
        self.assertEqual(state.get("恒真循环#iter"), 2)

    def test_parallel_failure_is_aggregated(self):
        """并行节点失败：错误要记入状态，且整体结果为失败。"""
        wf = Workflow("par", error_policy="continue")
        wf.add_parallel(
            "并行",
            [Node("ok", lambda s: 1), Node("bad", lambda s: 1 / 0)],
        )
        state = wf.run(SharedState(), EventLog())
        self.assertIn("bad", [e["node"] for e in state.get("errors", [])])

    def test_generated_code_has_real_action(self):
        """离线模板生成的函数要真的有行为，而不是统一的 return data。"""
        mod = {
            "name": "task",
            "responsibility": "必须支持任务的创建",
            "functions": [
                {"name": "process_task", "signature": "data", "doc": "创建任务。"},
                {"name": "validate_task", "signature": "data", "doc": "校验 task 输入。"},
            ],
        }
        code = CodingAgent._generate_module(mod)
        ast.parse(code)
        namespace: dict = {}
        exec(compile(code, "<generated>", "exec"), namespace)  # noqa: S102 - 测试自生成代码

        out = namespace["process_task"]({"id": 1})
        self.assertEqual(out["action"], "create")
        self.assertEqual(out["count"], 1)
        self.assertIs(namespace["validate_task"]({"id": 1}), True)

    def test_mermaid_expands_loop_body(self):
        """流程图要展开循环体，不能只剩一个循环节点。"""
        from workflow.flows import build_review_fix_flow

        agents = {"review": ReviewAgent(), "fix": FixAgent()}
        mermaid = build_review_fix_flow(agents).to_mermaid()
        self.assertIn("审查-修复循环", mermaid)
        self.assertIn("代码审查", mermaid)
        self.assertIn("自动修复", mermaid)
        self.assertIn("-.", mermaid)  # 回边


# ---------------------------------------------------------------------------
# 端到端（离线模式）
# ---------------------------------------------------------------------------
class TestEndToEnd(unittest.TestCase):
    def test_mini_se_flow(self):
        """Mini SE 流程：需求→设计→代码→并行审查，产出可解析的源码。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "mini-se")
            result = run_mini_se("开发一个任务管理工具。必须支持任务的创建和查询。", out)
            state = result["state"]

            self.assertTrue(state.get("requirements")["features"])
            self.assertTrue(state.get("design")["modules"])
            code = state.get("code")
            self.assertGreater(code["count"], 0)

            # 生成的每个文件都应是语法正确的 Python
            for f in code["files"]:
                ast.parse(f["code"])

            # 并行审查覆盖了全部生成文件
            reviews = state.get("reviews")
            self.assertEqual(len(reviews), code["count"])

            # 产物落盘
            self.assertTrue(os.path.isdir(os.path.join(out, "generated")))
            self.assertTrue(os.path.isfile(os.path.join(out, "mini_se_log.txt")))

    def test_review_fix_flow(self):
        """审查修复流程：循环修复后代码无剩余可修复问题且语法正确。"""
        with tempfile.TemporaryDirectory() as tmp:
            out = os.path.join(tmp, "review-fix")
            result = run_review_fix(None, out)
            state = result["state"]

            review = state.get("review")
            self.assertTrue(review["passed"])  # 最终审查通过
            self.assertEqual(review["issues"], [])

            # 修复后的文件语法正确，且不再包含裸 except / 硬编码密钥
            with open(review["target"], encoding="utf-8") as fh:
                fixed_src = fh.read()
            ast.parse(fixed_src)
            self.assertNotIn("except:", fixed_src)
            self.assertNotIn('"123456"', fixed_src)

    def test_agent_offline_fallback(self):
        """无 API Key 时 Agent 自动降级离线并正常产出。"""
        with tempfile.TemporaryDirectory() as tmp:
            agent = RequirementAgent()
            self.assertEqual(agent.mode, "offline")
            result = agent(SharedState({"requirement": "实现一个计算器，支持加减法。"}))
            self.assertTrue(result["features"])
            self.assertEqual(result["source"], "offline")


class TestCodingAgentLLM(unittest.TestCase):
    """编码 Agent 的 LLM 生成路径（使用假客户端，不联网）。"""

    class _FakeClient:
        available = True

        def __init__(self, content: str) -> None:
            self.content = content

        def chat(self, messages, tools=None, tool_choice="auto"):
            return {"choices": [{"message": {"content": self.content}}]}

    def test_llm_generates_valid_code(self):
        """LLM 返回带代码块的合法代码：应剥离围栏并通过语法校验。"""
        raw = '```python\ndef add(a, b):\n    """求和。"""\n    return a + b\n```'
        agent = CodingAgent(client=self._FakeClient(raw))
        code = agent._generate_with_llm({"name": "calc", "responsibility": "计算"})
        self.assertIsNotNone(code)
        ast.parse(code)
        self.assertIn("def add", code)

    def test_invalid_output_falls_back(self):
        """LLM 返回非代码内容：应返回 None，由调用方回退模板。"""
        agent = CodingAgent(client=self._FakeClient("抱歉，我无法生成。"))
        self.assertIsNone(agent._generate_with_llm({"name": "m"}))


if __name__ == "__main__":
    unittest.main(verbosity=2)
