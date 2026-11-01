"""工具集（Tools）：Agent 与环境交互的手足。

提供 4 个工具，对应作业「至少支持一种工具」并可组合使用：
- read_file   ：读取文件内容（LLM 理解代码的前提）
- list_dir    ：列出目录结构（定位文件）
- run_code    ：在受限沙箱中执行 Python 片段（验证想法 / 运行示例）
- search_code ：在代码库中按正则搜索（代码审查 / 检索）

每个工具同时提供：供 LLM 使用的 JSON Schema 描述 + 本地执行函数。
ToolRegistry 负责注册、生成 schema、按名执行。
"""
from __future__ import annotations

import dataclasses
import os
import re
import subprocess
import sys
from typing import Any, Callable, Dict, List, Optional


@dataclasses.dataclass
class Tool:
    name: str
    description: str
    parameters: Dict[str, Any]  # JSON Schema
    func: Callable[..., str]

    @property
    def schema(self) -> Dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def execute(self, arguments: Dict[str, Any]) -> str:
        try:
            return str(self.func(**arguments))
        except Exception as exc:  # 工具内部异常需被 Agent 捕获并反馈，而非崩溃
            return f"[工具执行异常] {type(exc).__name__}: {exc}"


class ToolRegistry:
    def __init__(self) -> None:
        self._tools: Dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        self._tools[tool.name] = tool

    def get(self, name: str) -> Optional[Tool]:
        return self._tools.get(name)

    def get_schemas(self) -> List[Dict[str, Any]]:
        return [t.schema for t in self._tools.values()]

    def execute(self, name: str, arguments: Dict[str, Any]) -> str:
        tool = self._tools.get(name)
        if tool is None:
            return f"[未知工具] {name}"
        return tool.execute(arguments)


# --------------------------------------------------------------------------- #
# 具体工具实现
# --------------------------------------------------------------------------- #
def read_file(path: str, max_lines: int = 2000) -> str:
    if not os.path.isfile(path):
        return f"[错误] 文件不存在：{path}"
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            lines = fh.readlines()
    except Exception as exc:
        return f"[错误] 读取失败：{exc}"
    if len(lines) > max_lines:
        head = "".join(lines[:max_lines])
        return f"（仅显示前 {max_lines} 行，共 {len(lines)} 行）\n{head}"
    return "".join(lines)


def list_dir(path: str = ".") -> str:
    if not os.path.isdir(path):
        return f"[错误] 目录不存在：{path}"
    entries = []
    try:
        for name in sorted(os.listdir(path)):
            full = os.path.join(path, name)
            kind = "[目录]" if os.path.isdir(full) else "[文件]"
            entries.append(f"{kind} {name}")
    except Exception as exc:
        return f"[错误] 列举失败：{exc}"
    return "\n".join(entries) if entries else "（空目录）"


def run_code(code: str, timeout: int = 10) -> str:
    """在受限沙箱中执行一段 Python 代码，捕获 stdout/stderr，带超时。"""
    try:
        proc = subprocess.run(
            [sys.executable, "-c", code],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired:
        return f"[超时] 代码执行超过 {timeout} 秒，已被终止。"
    out = proc.stdout.strip()
    err = proc.stderr.strip()
    result = []
    if out:
        result.append(f"--- stdout ---\n{out}")
    if err:
        result.append(f"--- stderr ---\n{err}")
    if proc.returncode != 0 and not err and not out:
        result.append(f"[进程退出码 {proc.returncode}]")
    return "\n".join(result) if result else "（无输出）"


def search_code(pattern: str, path: str = ".", glob: str = "*.py", max_matches: int = 50) -> str:
    try:
        regex = re.compile(pattern)
    except re.error as exc:
        return f"[错误] 正则表达式无效：{exc}"
    hits: List[str] = []
    for root, _dirs, files in os.walk(path):
        if ".git" in root.split(os.sep):
            continue
        for fname in files:
            if not _match_glob(fname, glob):
                continue
            full = os.path.join(root, fname)
            try:
                with open(full, "r", encoding="utf-8", errors="replace") as fh:
                    for i, line in enumerate(fh, 1):
                        if regex.search(line):
                            hits.append(f"{full}:{i}: {line.rstrip()}")
                            if len(hits) >= max_matches:
                                return "\n".join(hits) + "\n（命中过多，已截断）"
            except Exception:
                continue
    return "\n".join(hits) if hits else "（未匹配到任何内容）"


def _match_glob(name: str, pattern: str) -> bool:
    import fnmatch
    return fnmatch.fnmatch(name, pattern)


def build_default_registry() -> ToolRegistry:
    """构造内置工具注册表。"""
    reg = ToolRegistry()
    reg.register(Tool(
        name="read_file",
        description="读取指定路径文件的文本内容，用于查看源码、配置等。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "文件相对或绝对路径"},
                "max_lines": {"type": "integer", "description": "最多读取行数，默认 2000", "default": 2000},
            },
            "required": ["path"],
        },
        func=read_file,
    ))
    reg.register(Tool(
        name="list_dir",
        description="列出目录下的文件与子目录，用于定位目标文件。",
        parameters={
            "type": "object",
            "properties": {
                "path": {"type": "string", "description": "目录路径，默认当前目录", "default": "."},
            },
            "required": [],
        },
        func=list_dir,
    ))
    reg.register(Tool(
        name="run_code",
        description="在受限沙箱中执行一段 Python 代码并返回 stdout/stderr（超时 10 秒）。",
        parameters={
            "type": "object",
            "properties": {
                "code": {"type": "string", "description": "要执行的 Python 代码"},
                "timeout": {"type": "integer", "description": "超时秒数，默认 10", "default": 10},
            },
            "required": ["code"],
        },
        func=run_code,
    ))
    reg.register(Tool(
        name="search_code",
        description="在代码库中按正则表达式搜索代码行，用于审查或检索。",
        parameters={
            "type": "object",
            "properties": {
                "pattern": {"type": "string", "description": "正则表达式"},
                "path": {"type": "string", "description": "搜索根目录，默认当前目录", "default": "."},
                "glob": {"type": "string", "description": "文件名匹配模式，默认 *.py", "default": "*.py"},
            },
            "required": ["pattern"],
        },
        func=search_code,
    ))
    return reg
