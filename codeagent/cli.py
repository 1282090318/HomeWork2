"""命令行入口（CLI）：构建组件并按子命令路由。

子命令：
  chat                启动交互式对话（在线/离线自动识别）
  review  <文件/目录> 审查代码质量（可审查单个 .py 或整个目录）
  explain <文件>      解释代码结构与逻辑
  search  <正则> [路径] 在代码中搜索
  run     <Python代码> 在沙箱中运行代码（不传代码则进入多行输入）
  test                运行内置自检示例
"""
from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from .agent import CodeAgent
from .client import LLMClient
from .offline import OfflineEngine
from .tools import build_default_registry


def build_components(args: argparse.Namespace):
    client = LLMClient(
        api_key=args.api_key,
        base_url=args.base_url,
        model=args.model,
    )
    tools = build_default_registry()
    offline = OfflineEngine(tools)
    agent = CodeAgent(client, tools, offline=offline)
    return agent, client, tools, offline


def _online_or_offline_review(agent: CodeAgent, offline: OfflineEngine, path: str) -> str:
    if agent.client.available:
        return agent.run(
            f"请审查文件（或目录）`{path}`。先使用 read_file 工具读取相关文件内容，"
            f"然后从代码质量、潜在 Bug、可维护性角度给出问题清单和改进建议。"
        )
    return offline.review_file(path)


def _online_or_offline_explain(agent: CodeAgent, offline: OfflineEngine, path: str) -> str:
    if agent.client.available:
        return agent.run(
            f"请解释文件 `{path}`。先使用 read_file 工具读取文件，"
            f"然后说明其模块结构、主要类/函数的职责与整体逻辑。"
        )
    return offline.explain_file(path)


def cmd_review(agent, offline, args):
    print(_online_or_offline_review(agent, offline, args.target))


def cmd_explain(agent, offline, args):
    print(_online_or_offline_explain(agent, offline, args.target))


def cmd_search(agent, tools, args):
    print(tools.execute("search_code", {"pattern": args.pattern, "path": args.path}))


def cmd_run(agent, tools, args):
    code = args.code
    if not code:
        print("请输入要执行的 Python 代码（空行结束）：")
        lines: List[str] = []
        while True:
            line = sys.stdin.readline()
            if line == "\n":
                break
            if not line:
                break
            lines.append(line.rstrip("\n"))
        code = "\n".join(lines)
    print(tools.execute("run_code", {"code": code}))


def cmd_test(agent, offline, args):
    sample = (
        "def add(a,b):\n"
        "    return a+b\n"
        "result = add(2,3)\n"
        "print('add(2,3) =', result)\n"
    )
    print("== 1) 运行示例代码 ==")
    print(agent.tools.execute("run_code", {"code": sample}))
    print("\n== 2) 离线代码审查演示 ==")
    with open("_demo_review.py", "w", encoding="utf-8") as fh:
        fh.write("import os\n\ndef foo(x):\n    pass\n")
    print(offline.review_file("_demo_review.py"))
    try:
        import os as _os
        _os.remove("_demo_review.py")
    except Exception:
        pass


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="CodeAgent",
        description="CodeAgent —— 一个基于 LLM 的代码助手 Agent（支持离线模式）。",
    )
    p.add_argument("--api-key", default=None, help="OpenAI API Key（或设环境变量 OPENAI_API_KEY）")
    p.add_argument(
        "--base-url",
        default=None,
        help="API 基址（默认取 OPENAI_BASE_URL，再缺省为客户端内置默认值）",
    )
    p.add_argument(
        "--model",
        default=None,
        help="模型名（默认取 OPENAI_MODEL，再缺省为客户端内置默认值）",
    )

    sub = p.add_subparsers(dest="command")
    sub.add_parser("chat", help="交互式对话")

    pr = sub.add_parser("review", help="审查代码")
    pr.add_argument("target", help="文件或目录路径")

    pe = sub.add_parser("explain", help="解释代码")
    pe.add_argument("target", help="文件路径")

    ps = sub.add_parser("search", help="搜索代码")
    ps.add_argument("pattern", help="正则")
    ps.add_argument("path", nargs="?", default=".", help="搜索路径，默认当前目录")

    prun = sub.add_parser("run", help="运行代码")
    prun.add_argument("code", nargs="?", default=None, help="要运行的 Python 代码")

    sub.add_parser("test", help="运行内置自检示例")
    return p


def main(argv: Optional[List[str]] = None) -> int:
    # 强制 UTF-8 输出，避免 Windows 控制台（GBK）下编码报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    command = args.command or "chat"

    agent, client, tools, offline = build_components(args)

    if command == "chat":
        agent.chat_loop()
    elif command == "review":
        cmd_review(agent, offline, args)
    elif command == "explain":
        cmd_explain(agent, offline, args)
    elif command == "search":
        cmd_search(agent, tools, args)
    elif command == "run":
        cmd_run(agent, tools, args)
    elif command == "test":
        cmd_test(agent, offline, args)
    else:
        parser.print_help()
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
