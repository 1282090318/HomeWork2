"""命令行入口（CLI）。

子命令：

- ``mini-se``     运行 Mini SE 流程（需求 → 设计 → 代码 → 并行审查）
- ``review-fix``  运行代码审查修复流程（审查 → 修复 循环）
- ``demo``        一键运行两条演示流程
- ``graph``       打印两条流程的 Mermaid 流程图（供文档使用）
"""
from __future__ import annotations

import argparse
import os
import sys
from typing import List, Optional

from .agents import (
    CodingAgent,
    DesignAgent,
    FixAgent,
    RequirementAgent,
    ReviewAgent,
)
from .core import EventLog, MessageBus
from .flows import (
    DEFAULT_REQUIREMENT,
    build_mini_se_flow,
    build_review_fix_flow,
    run_mini_se,
    run_review_fix,
)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="HW2-Workflow",
        description="Simple Workflow MVP —— 多 Agent 协作的 Workflow 编排（HW2）",
    )
    p.add_argument(
        "--offline",
        action="store_true",
        help="强制离线模式（不调用 LLM，测试/快速演示用）",
    )
    sub = p.add_subparsers(dest="command")

    p_se = sub.add_parser("mini-se", help="Mini SE 流程：需求 → 设计 → 代码 → 并行审查")
    p_se.add_argument("--requirement", default=None, help="需求文本")
    p_se.add_argument("--file", default=None, help="需求文本文件路径")
    p_se.add_argument("--out", default="output/mini-se", help="产物输出目录")

    p_rf = sub.add_parser("review-fix", help="审查修复流程：审查 → 修复（循环）")
    p_rf.add_argument("target", nargs="?", default=None, help="待审查的 .py 文件（缺省用内置问题代码）")
    p_rf.add_argument("--out", default="output/review-fix", help="产物输出目录")

    sub.add_parser("demo", help="一键运行两条演示流程")
    sub.add_parser("graph", help="打印 Mermaid 流程图")
    return p


def _print_log(log: EventLog) -> None:
    # live 模式下事件已实时打印，这里跳过避免重复刷屏
    if EventLog.live:
        return
    print(log.to_text())


def _summarize_mini_se(state) -> None:
    print("\n===== Mini SE 流程结果 =====")
    reqs = state.get("requirements") or {}
    print(f"[需求] {len(reqs.get('features', []))} 项功能需求（{reqs.get('source')} 模式）")
    design = state.get("design") or {}
    modules = design.get("modules", [])
    print(f"[设计] {len(modules)} 个模块：{', '.join(m['name'] for m in modules)}")
    code = state.get("code") or {}
    print(f"[编码] 生成 {code.get('count', 0)} 个源码文件：{', '.join(f['name'] for f in code.get('files', []))}")
    reviews = state.get("reviews") or []
    total_issues = sum(len(r.get("issues", [])) for r in reviews)
    print(f"[并行审查] 审查 {len(reviews)} 个文件，共发现 {total_issues} 个问题")
    for r in reviews:
        mark = "通过" if r.get("passed") else f"{len(r['issues'])} 个问题"
        print(f"    - {os.path.basename(r['target'])}: {mark}")


def _summarize_review_fix(state) -> None:
    print("\n===== 审查修复流程结果 =====")
    review = state.get("review") or {}
    issues = review.get("issues", [])
    fix = state.get("fix") or {}
    print(f"[最终审查] 剩余 {len(issues)} 个问题（目标：{review.get('target')}）")
    for it in issues:
        tag = "可自动修复" if it.get("fixable") else "需人工处理"
        print(f"    - 第 {it.get('line')} 行 [{it.get('type')}] {tag}")
    print(f"[自动修复] 最后一轮修复 {fix.get('fixed_count', 0)} 处")
    unfixable = fix.get("unfixable") or []
    if unfixable:
        print(f"[需人工] {len(unfixable)} 个问题无法自动修复（如函数过长需人工拆分）")


def cmd_mini_se(args) -> int:
    requirement = args.requirement
    if args.file:
        with open(args.file, "r", encoding="utf-8") as fh:
            requirement = fh.read().strip()
    if not requirement:
        requirement = DEFAULT_REQUIREMENT
    result = run_mini_se(requirement, artifacts_dir=args.out)
    _print_log(result["log"])
    _summarize_mini_se(result["state"])
    print(f"\n产物已保存到：{os.path.abspath(args.out)}")
    return 0


def cmd_review_fix(args) -> int:
    result = run_review_fix(args.target, artifacts_dir=args.out)
    _print_log(result["log"])
    _summarize_review_fix(result["state"])
    print(f"\n产物已保存到：{os.path.abspath(args.out)}")
    return 0


def cmd_demo(_args) -> int:
    # 两条流程分开跑，各自打印阶段标题：避免"全跑完才输出"导致误以为卡死
    print("=== 流程一：Mini SE（需求 → 设计 → 编码 → 并行审查）===", flush=True)
    mini = run_mini_se(DEFAULT_REQUIREMENT, os.path.join("output", "mini-se"))
    _print_log(mini["log"])
    _summarize_mini_se(mini["state"])

    print("\n=== 流程二：代码审查修复（审查 → 修复 循环）===", flush=True)
    review_fix = run_review_fix(None, os.path.join("output", "review-fix"))
    _print_log(review_fix["log"])
    _summarize_review_fix(review_fix["state"])
    return 0


def cmd_graph(_args) -> int:
    bus, log = MessageBus(), EventLog()
    agents = {
        "requirement": RequirementAgent(bus=bus, log=log),
        "design": DesignAgent(bus=bus, log=log),
        "coding": CodingAgent(bus=bus, log=log),
        "review": ReviewAgent(bus=bus, log=log),
        "fix": FixAgent(bus=bus, log=log),
    }
    print("# Mini SE 流程\n")
    print(build_mini_se_flow(agents).to_mermaid())
    print("\n# 代码审查修复流程\n")
    print(build_review_fix_flow(agents).to_mermaid())
    return 0


def main(argv: Optional[List[str]] = None) -> int:
    # 强制 UTF-8 输出，避免 Windows 控制台（GBK）下编码报错
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass
    parser = build_parser()
    args = parser.parse_args(argv)
    if getattr(args, "offline", False):
        os.environ["CODEAGENT_OFFLINE"] = "1"
    # 实时回显事件日志：长流程（尤其是联网时）不会看起来像卡死
    EventLog.live = True
    command = args.command or "demo"
    handlers = {
        "mini-se": cmd_mini_se,
        "review-fix": cmd_review_fix,
        "demo": cmd_demo,
        "graph": cmd_graph,
    }
    return handlers[command](args)
