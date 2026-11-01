"""离线规则引擎（Offline Fallback）。

当未配置 API Key 时，CodeAgent 自动降级到本引擎：基于 Python `ast` 做静态分析，
实现作业要求的「代码审查 Agent」与「代码解释 Agent」两个方向，并复用工具完成
搜索 / 执行。保证零依赖、零网络也能完整演示。

适用场景：无 API Key 的本地演示、CI 中的离线检查、LLM 不可用时的兜底。
"""
from __future__ import annotations

import ast
import os
import re
from typing import List, Optional, Tuple

from .tools import ToolRegistry


class OfflineEngine:
    def __init__(self, tools: ToolRegistry) -> None:
        self.tools = tools

    # ----------------------------- 入口分发 ----------------------------- #
    def dispatch(self, text: str) -> str:
        t = text.strip()
        if not t or t in ("help", "帮助", "?"):
            return self.help()
        if t.startswith("review ") or "审查" in t or "review" in t.lower():
            path = self._extract_path(t, ("review", "审查"))
            return self.review_file(path or ".")
        if t.startswith("explain ") or "解释" in t or "explain" in t.lower():
            path = self._extract_path(t, ("explain", "解释"))
            return self.explain_file(path or ".")
        if t.startswith("search ") or t.startswith("搜索 "):
            prefix = "search " if t.startswith("search ") else "搜索 "
            parts = t[len(prefix):].split(maxsplit=1)
            pattern = parts[0] if parts else ""
            path = parts[1] if len(parts) > 1 else "."
            return self.tools.execute("search_code", {"pattern": pattern, "path": path})
        if t.startswith("run ") or t.startswith("运行 "):
            prefix = "run " if t.startswith("run ") else "运行 "
            code = t[len(prefix):]
            return self.tools.execute("run_code", {"code": code})
        return ("未能识别的指令。可用：\n" + self.help()).strip()

    @staticmethod
    def _extract_path(text: str, keywords: Tuple[str, ...]) -> Optional[str]:
        # 取最后一个关键字之后的内容作为路径
        for kw in keywords:
            idx = text.lower().rfind(kw.lower())
            if idx != -1:
                rest = text[idx + len(kw):].strip(" :：")
                if rest:
                    return rest
        return None

    @staticmethod
    def help() -> str:
        return (
            "离线模式命令示例（中英文均可）：\n"
            "  review | 审查 <文件路径>        审查代码质量并给出改进建议\n"
            "  explain | 解释 <文件路径>       解释代码结构与逻辑\n"
            "  search | 搜索 <正则> [目录]     在代码中搜索\n"
            "  run | 运行 <Python 代码>        在沙箱中运行一段代码\n"
            "  help | 帮助                     显示本帮助"
        )

    # ----------------------------- 代码审查 ----------------------------- #
    def review_file(self, path: str) -> str:
        if os.path.isdir(path):
            py_files = [os.path.join(path, f) for f in os.listdir(path)
                        if f.endswith(".py") and os.path.isfile(os.path.join(path, f))]
            if not py_files:
                return f"目录 {path} 下未找到 .py 文件。"
            lines = [self.review_file(f) for f in py_files[:10]]
            return "\n\n".join(lines)
        if not path.endswith(".py") or not os.path.isfile(path):
            return f"[跳过] {path} 不是可审查的 Python 文件。"

        source = self.tools.execute("read_file", {"path": path})
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"【代码审查】{path}\n[警告] 语法错误，无法静态分析：{exc}"

        issues: List[str] = []
        functions: List[ast.FunctionDef] = []
        classes: List[ast.ClassDef] = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                functions.append(node)
            elif isinstance(node, ast.ClassDef):
                classes.append(node)

        # 1. 函数规模与文档
        for fn in functions:
            body = [n for n in fn.body if not isinstance(n, ast.Expr)]
            loc = (body[-1].end_lineno - fn.lineno) if body else 0
            if loc > 50:
                issues.append(f"第 {fn.lineno} 行 函数 `{fn.name}` 过长（约 {loc} 行），建议拆分。")
            if not ast.get_docstring(fn):
                issues.append(f"第 {fn.lineno} 行 函数 `{fn.name}` 缺少 docstring。")

        # 2. 类缺少文档
        for cls in classes:
            if not ast.get_docstring(cls):
                issues.append(f"第 {cls.lineno} 行 类 `{cls.name}` 缺少 docstring。")

        # 3. 危险模式：裸 except、eval/exec、硬编码密钥
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler) and node.type is None:
                issues.append(f"第 {node.lineno} 行 存在裸 `except:`，应捕获具体异常。")
            if isinstance(node, (ast.Call,)):
                fn_name = getattr(getattr(node.func, "attr", None), "__str__", None)
                if isinstance(node.func, ast.Name) and node.func.id in ("eval", "exec"):
                    issues.append(f"第 {node.lineno} 行 使用了 `eval/exec`，存在安全风险。")

        # 硬编码密钥（基于源码文本行匹配）
        for i, line in enumerate(source.splitlines(), 1):
            if re.search(r"(password|passwd|secret|api[_-]?key|token)\s*=\s*['\"][^'\"]+['\"]", line, re.I):
                issues.append(f"第 {i} 行 疑似硬编码密钥/密码，应改为环境变量或配置读取。")
            if re.search(r"#\s*(TODO|FIXME|XXX)", line, re.I):
                issues.append(f"第 {i} 行 存在 TODO/FIXME 待办注释。")

        report = [f"【代码审查报告】{path}"]
        report.append(f"统计：{len(classes)} 个类，{len(functions)} 个函数。")
        if issues:
            report.append("发现以下可改进点：")
            report.extend(f"  - {it}" for it in issues)
            report.append("\n建议：补充文档字符串、缩小函数规模、捕获具体异常、"
                          "将敏感信息移出源码，可显著提升代码质量。")
        else:
            report.append("未检测到明显问题，代码风格良好。")
        return "\n".join(report)

    # ----------------------------- 代码解释 ----------------------------- #
    def explain_file(self, path: str) -> str:
        if not path.endswith(".py") or not os.path.isfile(path):
            return f"[跳过] {path} 不是可解释的 Python 文件。"
        source = self.tools.execute("read_file", {"path": path})
        try:
            tree = ast.parse(source)
        except SyntaxError as exc:
            return f"【代码解释】{path}\n[警告] 语法错误，无法解析：{exc}"

        title = os.path.basename(path)
        lines: List[str] = [f"【代码解释】{title}", ""]

        module_doc = ast.get_docstring(tree)
        if module_doc:
            lines.append(f"模块说明：{module_doc}")
            lines.append("")

        imports: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Import):
                imports.extend(alias.name for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                names = ", ".join(a.name for a in node.names)
                imports.append(f"{names} (from {mod})")
        if imports:
            lines.append("导入：")
            lines.extend(f"  - {imp}" for imp in imports)
            lines.append("")

        top_consts: List[str] = []
        for node in tree.body:
            if isinstance(node, ast.Assign):
                for tgt in node.targets:
                    if isinstance(tgt, ast.Name):
                        try:
                            val = ast.literal_eval(node.value)
                            top_consts.append(f"{tgt.id} = {val!r}")
                        except Exception:
                            top_consts.append(tgt.id)
        if top_consts:
            lines.append("顶层常量：")
            lines.extend(f"  - {c}" for c in top_consts)
            lines.append("")

        for node in tree.body:
            if isinstance(node, ast.ClassDef):
                lines.append(f"类 `{node.name}`" + (f"（继承 {[b.id for b in node.bases]}）" if node.bases else ""))
                for item in node.body:
                    if isinstance(item, ast.FunctionDef):
                        lines.append(f"    - 方法 `{item.name}({_args(item)})`")
                lines.append("")
            elif isinstance(node, ast.FunctionDef):
                lines.append(f"函数 `{node.name}({_args(node)})`")
                doc = ast.get_docstring(node)
                if doc:
                    lines.append(f"    说明：{doc.splitlines()[0]}")
                lines.append("")

        return "\n".join(lines)


def _args(fn: ast.FunctionDef) -> str:
    parts = []
    for a in fn.args.args:
        parts.append(a.arg)
    if fn.args.vararg:
        parts.append("*" + fn.args.vararg.arg)
    if fn.args.kwarg:
        parts.append("**" + fn.args.kwarg.arg)
    return ", ".join(parts)
