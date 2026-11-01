"""CodeAgent —— 一个基于 LLM 的代码助手 Agent。

支持模式：
- 在线模式：配置 OPENAI_API_KEY（或任意 OpenAI 兼容接口）后，由 LLM 自主推理并调用工具。
- 离线模式：未配置 API Key 时，自动降级到内置「规则引擎」，仍可使用
  代码读取 / 执行 / 搜索等工具完成审查与解释任务，方便无网络环境演示。
"""
from .client import LLMClient, LLMError
from .tools import Tool, ToolRegistry
from .agent import CodeAgent, Memory
from .offline import OfflineEngine

__version__ = "1.0.0"
__all__ = ["LLMClient", "LLMError", "Tool", "ToolRegistry", "CodeAgent", "Memory", "OfflineEngine"]
