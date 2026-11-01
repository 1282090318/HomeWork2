"""Agent 核心：上下文记忆 + 推理-工具循环。

Agent 循环（作业要求的输入 → 推理 → 工具调用 → 输出）：
  1. 将用户输入写入 Memory；
  2. 调用 LLM（携带工具 schema）；
  3. 若 LLM 返回 tool_calls，则依次执行工具并将结果回写 Memory，回到步骤 2；
  4. 若 LLM 返回最终文本，则结束本轮并返回。

通过 max_iter 限制循环次数，避免无限调用；所有消息保存在 Memory 中，
形成「上下文记忆」，支持多轮对话。
"""
from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from .client import LLMClient
from .offline import OfflineEngine
from .prompts import DEFAULT_SYSTEM
from .tools import ToolRegistry


class Memory:
    """维护对话上下文（含 system / user / assistant / tool 消息）。"""

    def __init__(self, max_messages: int = 40) -> None:
        self.messages: List[Dict[str, Any]] = []
        self.max = max_messages

    def add(
        self,
        role: str,
        content: Optional[str],
        name: Optional[str] = None,
        tool_call_id: Optional[str] = None,
        tool_calls: Optional[List[Dict[str, Any]]] = None,
    ) -> None:
        msg: Dict[str, Any] = {"role": role, "content": content}
        if name is not None:
            msg["name"] = name
        if tool_call_id is not None:
            msg["tool_call_id"] = tool_call_id
        if tool_calls is not None:
            msg["tool_calls"] = tool_calls
        self.messages.append(msg)
        # 简单截断：保留 system 之外的最后 max-1 条
        if len(self.messages) > self.max:
            sys_msgs = [m for m in self.messages if m["role"] == "system"]
            rest = self.messages[-(self.max - len(sys_msgs)):]
            self.messages = sys_msgs + rest

    def reset(self, system: str) -> None:
        self.messages = [{"role": "system", "content": system}]


class CodeAgent:
    def __init__(
        self,
        client: LLMClient,
        tools: ToolRegistry,
        system_prompt: str = DEFAULT_SYSTEM,
        max_iter: int = 8,
        offline: Optional[OfflineEngine] = None,
    ) -> None:
        self.client = client
        self.tools = tools
        self.system = system_prompt
        self.max_iter = max_iter
        self.offline = offline
        self.memory = Memory()
        self.memory.add("system", self.system)

    # ------------------------------ 主循环 ------------------------------ #
    def run(self, user_input: str) -> str:
        """执行一轮对话，返回 Agent 的最终文本回复。"""
        # 离线降级：无 API Key 时走规则引擎的分发
        if not self.client.available:
            if self.offline:
                return self.offline.dispatch(user_input)
            return "[警告] 未配置 API Key，且未提供离线引擎。"

        self.memory.add("user", user_input)
        tool_schemas = self.tools.get_schemas()

        for _ in range(self.max_iter):
            resp = self.client.chat(self.memory.messages, tools=tool_schemas)
            choice = resp["choices"][0]["message"]
            content = choice.get("content")
            tool_calls = choice.get("tool_calls")

            if tool_calls:
                # 将 assistant 的「工具调用意图」写入上下文
                self.memory.add(
                    "assistant",
                    content or "",
                    tool_calls=[
                        {
                            "id": tc.get("id"),
                            "type": "function",
                            "function": {
                                "name": tc["function"]["name"],
                                "arguments": tc["function"].get("arguments", "{}"),
                            },
                        }
                        for tc in tool_calls
                    ],
                )
                for tc in tool_calls:
                    fn = tc["function"]
                    try:
                        args = json.loads(fn.get("arguments") or "{}")
                    except json.JSONDecodeError:
                        args = {}
                    result = self.tools.execute(fn["name"], args)
                    self.memory.add(
                        "tool", result, name=fn["name"], tool_call_id=tc.get("id")
                    )
                # 携带工具结果继续推理
                continue
            else:
                self.memory.add("assistant", content or "")
                return content or "（无内容）"

        return "[警告] 已达到最大迭代次数（%d）仍未产出最终回复，请简化任务或检查工具。" % self.max_iter

    def chat_loop(self) -> None:
        """交互式 REPL（命令行对话）。"""
        print("CodeAgent 已启动，输入 `exit` 退出。")
        if not self.client.available and self.offline:
            from .prompts import OFFLINE_HINT
            print(OFFLINE_HINT.strip())
        while True:
            try:
                user = input("\n你> ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\n再见。")
                break
            if not user:
                continue
            if user.lower() in ("exit", "quit", "q", "退出"):
                print("再见。")
                break
            try:
                answer = self.run(user)
            except Exception as exc:  # 顶层兜底，避免一次错误终止整个会话
                answer = f"发生错误：{exc}"
            print("\nCodeAgent> " + answer)
