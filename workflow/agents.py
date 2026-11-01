"""Workflow 的五个 Agent 节点：需求 / 设计 / 编码 / 审查 / 修复。

每个 Agent：
- 既可以作为 Workflow 引擎的节点函数直接调用（``__call__(state)``）；
- 也会通过 ``MessageBus`` 向上/下游广播消息（Agent 间通信的实证）；
- 具备**离线 / 在线双模式**：配置了 API Key 时调用 LLM（复用 HW1 的
  ``LLMClient``），否则退回到内置规则引擎，保证无网络也能端到端跑通。
"""
from __future__ import annotations

import ast
import json
import os
import re
from typing import Any, Dict, List, Optional

from codeagent.client import LLMClient
from codeagent.offline import OfflineEngine
from codeagent.tools import build_default_registry

from .core import BaseAgent, EventLog, Message, MessageBus, SharedState

# ---------------------------------------------------------------------------
# 关键词 -> 英文模块名映射（离线需求解析/命名用）
# ---------------------------------------------------------------------------
KEYWORD_MODULE_MAP = {
    "计算": "calculator", "管理": "manager", "存储": "storage", "用户": "user",
    "文件": "file_ops", "网络": "network", "数据": "data", "报表": "report",
    "通知": "notifier", "日志": "logger", "配置": "config", "安全": "security",
    "搜索": "search", "排序": "sorter", "统计": "stats", "导入": "importer",
    "导出": "exporter", "邮件": "mail", "任务": "task", "日程": "schedule",
    "图书": "book", "订单": "order", "商品": "product", "库存": "inventory",
    "支付": "payment", "成绩": "grade", "学生": "student", "课程": "course",
    "设备": "device", "监控": "monitor", "提醒": "reminder", "备份": "backup",
}

ACTION_WORDS = (
    "计算", "管理", "支持", "实现", "提供", "生成", "处理", "查询", "显示",
    "保存", "导入", "导出", "统计", "分析", "发送", "通知", "记录", "检索",
    "提醒", "备份", "恢复", "登录", "注册", "打印", "汇总", "排序", "筛选",
)

HIGH_PRIORITY_WORDS = ("必须", "核心", "关键", "刚需", "需要")
LOW_PRIORITY_WORDS = ("可选", "希望", "后续", "尽量", "建议")
CONSTRAINT_WORDS = (
    "不超过", "至少", "最多", "性能", "安全", "兼容", "响应", "秒", "分钟",
    "MB", "GB", "并发", "权限", "加密",
)


def _sentences(text: str) -> List[str]:
    """把需求文本切分为句子。"""
    parts = re.split(r"[。；;\n]+", text or "")
    return [p.strip(" ，,、") for p in parts if p.strip(" ，,、")]


def _module_name_for(feature: str, index: int) -> str:
    """根据功能描述推导英文模块名。"""
    for kw, name in KEYWORD_MODULE_MAP.items():
        if kw in feature:
            return name
    return f"module_{index}"


def _after_module_docstring(lines: List[str]) -> int:
    """返回可安全插入 import 的行号：模块 docstring 之后；无 docstring 则为 0。"""
    try:
        tree = ast.parse("\n".join(lines))
    except SyntaxError:
        return 0
    if tree.body:
        first = tree.body[0]
        if (
            isinstance(first, ast.Expr)
            and isinstance(first.value, ast.Constant)
            and isinstance(first.value.value, str)
        ):
            return first.end_lineno or 0
    return 0


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从 LLM 输出中提取第一个 JSON 对象。"""
    if not text:
        return None
    start = text.find("{")
    end = text.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        return json.loads(text[start:end + 1])
    except json.JSONDecodeError:
        return None


# ---------------------------------------------------------------------------
# Agent 基类（Workflow 版）
# ---------------------------------------------------------------------------
class WorkflowAgentBase(BaseAgent):
    """带 LLM/离线双模式的 Agent 基类。"""

    name = "agent"
    role = "Workflow Agent"
    output_kind = "artifact"

    def __init__(
        self,
        bus: Optional[MessageBus] = None,
        log: Optional[EventLog] = None,
        client: Optional[LLMClient] = None,
        tools=None,
    ) -> None:
        super().__init__(bus=bus, log=log)
        # CODEAGENT_OFFLINE=1 时强制离线（测试/快速演示用，避免等待网络与重试）
        # 仅在未显式传入 client 时生效，避免覆盖测试注入的假客户端
        if client is None:
            client = LLMClient()
            if os.environ.get("CODEAGENT_OFFLINE") == "1":
                client.api_key = ""  # 硬置空，避免回退到环境变量或默认 Key
        self.client = client
        self.tools = tools or build_default_registry()
        self.offline = OfflineEngine(self.tools)

    @property
    def mode(self) -> str:
        """当前运行模式：llm / offline。"""
        return "llm" if self.client.available else "offline"

    def _ask_llm(self, system: str, user: str) -> Optional[Dict[str, Any]]:
        """调用 LLM 并尝试解析 JSON；失败返回 None（由调用方降级到离线）。"""
        if not self.client.available:
            return None  # 未配置 API Key，直接走离线，无需重试等待
        try:
            resp = self.client.chat(
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ]
            )
            content = resp["choices"][0]["message"].get("content") or ""
            return _extract_json(content)
        except Exception as exc:  # noqa: BLE001
            if self.log is not None:
                self.log.error(self.name, f"LLM 调用失败，降级离线：{exc}")
            return None

    def __call__(self, state: SharedState) -> Dict[str, Any]:
        """引擎节点入口：执行业务逻辑并向总线广播产出（Agent 间通信）。"""
        result = self.execute(state)
        if self.bus is not None:
            self.bus.publish(self.send(self.output_kind, {"agent": self.name, "result": result}))
        return result

    def process(self, message: Message, state: Optional[SharedState] = None) -> Optional[Message]:
        """消息总线入口适配层。

        ``BaseAgent.receive`` 收到消息后会调用 ``process``，而本类把业务逻辑
        写在 ``execute(state)`` 上。这里负责翻译：把上游消息里的产物铺进共享
        状态，执行一次本 Agent，再把结果作为回复消息发回总线。

        没有这层适配，真实 Agent 一被订阅就会抛 ``NotImplementedError``，
        消息总线形同虚设。
        """
        state = SharedState() if state is None else state
        payload = message.payload if isinstance(message.payload, dict) else {}
        upstream = payload.get("result")
        if isinstance(upstream, dict):
            # 上游产物直接铺进黑板：requirement/design/code/review 等键名可以原样复用
            state.update(upstream, source=message.sender)
        else:
            state.update(
                {k: v for k, v in payload.items() if k != "agent"}, source=message.sender
            )
        result = self.execute(state)
        return self.send(self.output_kind, {"agent": self.name, "result": result})

    def execute(self, state: SharedState) -> Dict[str, Any]:
        raise NotImplementedError


# ---------------------------------------------------------------------------
# 1. 需求 Agent
# ---------------------------------------------------------------------------
class RequirementAgent(WorkflowAgentBase):
    """把自然语言需求解析为结构化需求（功能需求 + 约束 + 优先级）。"""

    name = "requirement_agent"
    role = "需求 Agent"
    output_kind = "requirement"

    def execute(self, state: SharedState) -> Dict[str, Any]:
        text = str(state.get("requirement", "")).strip()
        if not text:
            raise ValueError("state 中缺少 'requirement'（需求文本）")

        data = self._ask_llm(
            system=(
                "你是需求分析专家。把用户需求解析为 JSON："
                '{"features":[{"id":"F1","desc":"...","priority":"高|中|低"}],'
                '"constraints":["..."]}。只输出 JSON。'
            ),
            user=text,
        )
        if not (data and data.get("features")):
            data = self._parse_offline(text)
        else:
            data.setdefault("source", "llm")
        data["raw"] = text
        if self.log is not None:
            self.log.node_end(
                self.name,
                f"解析出 {len(data.get('features', []))} 项功能需求（{data.get('source')} 模式）",
            )
        return data

    def _parse_offline(self, text: str) -> Dict[str, Any]:
        features: List[Dict[str, str]] = []
        constraints: List[str] = []
        for sent in _sentences(text):
            if any(w in sent for w in CONSTRAINT_WORDS):
                constraints.append(sent)
                continue
            if any(w in sent for w in ACTION_WORDS) or not features:
                if any(w in sent for w in HIGH_PRIORITY_WORDS):
                    priority = "高"
                elif any(w in sent for w in LOW_PRIORITY_WORDS):
                    priority = "低"
                else:
                    priority = "中"
                features.append(
                    {"id": f"F{len(features) + 1}", "desc": sent, "priority": priority}
                )
        if not features:
            features.append({"id": "F1", "desc": text, "priority": "高"})
        return {
            "features": features,
            "constraints": constraints or ["无显式约束"],
            "source": "offline",
        }


# ---------------------------------------------------------------------------
# 2. 设计 Agent
# ---------------------------------------------------------------------------
class DesignAgent(WorkflowAgentBase):
    """把结构化需求转化为模块设计（模块划分 + 函数签名 + 数据流）。"""

    name = "design_agent"
    role = "设计 Agent"
    output_kind = "design"

    def execute(self, state: SharedState) -> Dict[str, Any]:
        reqs = state.get("requirements")
        if not reqs:
            raise ValueError("state 中缺少 'requirements'（上游需求产物）")

        data = self._ask_llm(
            system=(
                "你是软件架构师。根据需求 JSON 输出设计 JSON："
                '{"modules":[{"name":"...","responsibility":"...",'
                '"functions":[{"name":"...","signature":"...","doc":"..."}]}],'
                '"data_flow":["模块A","模块B"]}。只输出 JSON。'
            ),
            user=json.dumps(reqs, ensure_ascii=False),
        )
        if not (data and data.get("modules")):
            data = self._design_offline(reqs)
        else:
            data.setdefault("source", "llm")
        data.setdefault("data_flow", [m["name"] for m in data.get("modules", [])])
        if self.log is not None:
            self.log.node_end(
                self.name, f"产出 {len(data['modules'])} 个模块设计（{data.get('source')} 模式）"
            )
        return data

    def _design_offline(self, reqs: Dict[str, Any]) -> Dict[str, Any]:
        modules: List[Dict[str, Any]] = []
        used_names: set = set()
        for i, feat in enumerate(reqs.get("features", []), 1):
            base = _module_name_for(feat["desc"], i)
            name = base
            suffix = 2
            while name in used_names:  # 模块名去重
                name = f"{base}_{suffix}"
                suffix += 1
            used_names.add(name)
            modules.append(
                {
                    "name": name,
                    "responsibility": feat["desc"],
                    # 函数名跟着去重后的模块名走，避免 task / task_2 两个模块都定义 process_task
                    "functions": [
                        {
                            "name": f"process_{name}",
                            "signature": "data",
                            "doc": f"{feat['desc']}（对应需求 {feat['id']}，优先级：{feat['priority']}）",
                        },
                        {
                            "name": f"validate_{name}",
                            "signature": "data",
                            "doc": f"校验 {name} 模块的输入数据合法性。",
                        },
                    ],
                }
            )
        return {
            "modules": modules,
            "data_flow": [m["name"] for m in modules],
            "source": "offline",
        }


# ---------------------------------------------------------------------------
# 3. 编码 Agent
# ---------------------------------------------------------------------------
class CodingAgent(WorkflowAgentBase):
    """根据设计生成 Python 源码（离线：模板化代码生成；在线：LLM 生成）。

    生成的代码刻意保持"干净"（含 docstring、无裸 except、无硬编码密钥），
    以便演示 Mini SE 流程端到端跑通后审查可通过。
    """

    name = "coding_agent"
    role = "编码 Agent"
    output_kind = "code"

    def execute(self, state: SharedState) -> Dict[str, Any]:
        design = state.get("design")
        if not design:
            raise ValueError("state 中缺少 'design'（上游设计产物）")

        files: List[Dict[str, str]] = []
        llm_count = 0
        for mod in design.get("modules", []):
            code = self._generate_with_llm(mod)  # 有 Key 时由 LLM 生成
            if code:
                llm_count += 1
            else:
                code = self._generate_module(mod)  # 失败/无 Key 时模板兜底
            files.append({"name": f"{mod['name']}.py", "code": code})

        artifacts_dir = state.get("artifacts_dir")
        if artifacts_dir:
            gen_dir = os.path.join(str(artifacts_dir), "generated")
            os.makedirs(gen_dir, exist_ok=True)
            for f in files:
                with open(os.path.join(gen_dir, f["name"]), "w", encoding="utf-8") as fh:
                    fh.write(f["code"])

        if llm_count == len(files) and files:
            source = "llm"
        elif llm_count:
            source = "hybrid"
        else:
            source = "offline"

        if self.log is not None:
            self.log.node_end(
                self.name, f"生成 {len(files)} 个源码文件（AI 生成 {llm_count} 个，其余模板兜底）"
            )
        return {
            "files": files,
            "count": len(files),
            "source": source,
            "llm_files": llm_count,
        }

    def _generate_with_llm(self, mod: Dict[str, Any]) -> Optional[str]:
        """让 LLM 按模块设计生成源码；返回 None 表示不可用/不合格，由调用方兜底。"""
        if not self.client.available:
            return None
        try:
            resp = self.client.chat(
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "你是资深 Python 工程师。根据模块设计生成完整、可运行的 Python 源码。"
                            "要求：1) 每个函数都要有 docstring；2) 不要使用裸 except；"
                            "3) 不要硬编码密钥；4) 只输出代码，不要 Markdown 代码块。"
                        ),
                    },
                    {
                        "role": "user",
                        "content": (
                            "模块设计：\n"
                            + json.dumps(mod, ensure_ascii=False, indent=2)
                            + "\n\n请生成该模块的完整 .py 文件内容。"
                        ),
                    },
                ]
            )
            content = resp["choices"][0]["message"].get("content") or ""
            code = self._extract_code(self._strip_fence(content))
            ast.parse(code)  # 语法校验：不合格直接放弃，回退模板
            if "def " not in code:
                return None
            return code if code.endswith("\n") else code + "\n"
        except Exception as exc:  # noqa: BLE001 - 任何失败都回退模板，保证流程不中断
            if self.log is not None:
                self.log.error(
                    self.name, f"LLM 生成模块 {mod.get('name')} 失败，回退模板：{exc}"
                )
            return None

    @staticmethod
    def _strip_fence(text: str) -> str:
        """去掉 Markdown 代码块 ```python ... ``` 包装。"""
        t = (text or "").strip()
        if t.startswith("```"):
            lines = t.splitlines()
            if lines and lines[0].startswith("```"):
                lines = lines[1:]
            if lines and lines[-1].strip().startswith("```"):
                lines = lines[:-1]
            t = "\n".join(lines).strip()
        return t

    @staticmethod
    def _extract_code(text: str) -> str:
        """若模型在代码前后夹带说明文字，截取从第一条代码语句开始的部分。"""
        t = (text or "").strip()
        candidates = [i for i in (
            t.find("import "), t.find("from "), t.find("def "), t.find('"""'), t.find("class ")
        ) if i >= 0]
        return t[min(candidates):].strip() if candidates else t

    #: 需求动词 -> 生成的动作实现（离线模板据此产出真正的业务逻辑）
    ACTION_RULES = (
        ("create", ("创建", "新增", "添加", "录入", "注册", "add", "create")),
        ("delete", ("删除", "移除", "清除", "取消", "remove", "delete")),
        ("query", ("查询", "检索", "查找", "搜索", "筛选", "列表", "search", "query", "find", "list")),
        ("count", ("统计", "计数", "汇总", "count", "stat")),
    )

    @classmethod
    def _detect_actions(cls, mod: Dict[str, Any]) -> List[str]:
        """根据模块职责与函数说明推断动作实现类型（可能命中多个动词）。"""
        text = " ".join(
            [str(mod.get("responsibility", ""))]
            + [str(fn.get("doc", "")) for fn in mod.get("functions", [])]
        ).lower()
        found: List[str] = []
        for action, words in cls.ACTION_RULES:
            if any(w in text for w in words) and action not in found:
                found.append(action)
        return found or ["echo"]

    @classmethod
    def _generate_module(cls, mod: Dict[str, Any]) -> str:
        """离线模板生成：按需求动作产出有实际行为的模块，而不是空壳。"""
        name = mod["name"]
        actions = cls._detect_actions(mod)
        lines: List[str] = [
            f'"""模块 `{name}`：{mod.get("responsibility", "")}（动作：{"/".join(actions)}）"""',
            "",
            "from typing import Any, List",
            "",
            f"_store_{name}: List[Any] = []  # 模块内数据存储（进程内演示用）",
            "",
            "",
            "def validate_input(data: Any) -> bool:",
            '    """校验输入是否为非空数据。"""',
            "    return data is not None",
        ]
        for fn in mod.get("functions", []):
            fname = fn["name"]
            lines.extend(["", ""])
            lines.append(f'def {fname}({fn.get("signature", "data")}):')
            lines.append(f'    """{fn.get("doc", "")}"""')
            if fname.startswith("validate_"):
                # 校验函数只做校验，不再复制处理逻辑
                lines.append("    return validate_input(data)")
                continue
            lines.append("    if not validate_input(data):")
            lines.append('        raise ValueError("输入数据不合法")')
            lines.extend(cls._action_body(actions, name))
        lines.append("")
        return "\n".join(lines)

    @staticmethod
    def _action_body(actions: List[str], name: str) -> List[str]:
        """按动作集合生成函数体；命中多个动词时生成按 ``data["action"]`` 分发的实现。"""
        store = f"_store_{name}"

        if len(actions) > 1:
            primary = actions[0]
            lines = [
                f'    action = data.get("action", "{primary}") '
                f'if isinstance(data, dict) else "{primary}"'
            ]
            for act in actions:
                if act == "create":
                    lines += [
                        '    if action == "create":',
                        f"        {store}.append(data)",
                        f'        return {{"action": "create", "count": len({store})}}',
                    ]
                elif act == "delete":
                    lines += [
                        '    if action == "delete":',
                        '        key = data.get("id") if isinstance(data, dict) else data',
                        f"        before = len({store})",
                        f"        {store}[:] = [x for x in {store} "
                        'if (x.get("id") if isinstance(x, dict) else x) != key]',
                        f'        return {{"action": "delete", "removed": before - len({store})}}',
                    ]
                elif act == "query":
                    lines += [
                        '    if action == "query":',
                        f'        return {{"action": "query", "items": list({store})}}',
                    ]
                elif act == "count":
                    lines += [
                        '    if action == "count":',
                        f'        return {{"action": "count", "count": len({store})}}',
                    ]
            lines.append("    return data")
            return lines

        action = actions[0]
        if action == "create":
            return [
                f"    {store}.append(data)",
                f'    return {{"action": "create", "count": len({store})}}',
            ]
        if action == "delete":
            return [
                '    key = data.get("id") if isinstance(data, dict) else data',
                f"    before = len({store})",
                f"    {store}[:] = [x for x in {store} "
                'if (x.get("id") if isinstance(x, dict) else x) != key]',
                f'    return {{"action": "delete", "removed": before - len({store})}}',
            ]
        if action == "query":
            return [f'    return {{"action": "query", "items": list({store})}}']
        if action == "count":
            return [f'    return {{"action": "count", "count": len({store})}}']
        return ["    return data"]


# ---------------------------------------------------------------------------
# 4. 审查 Agent（复用 HW1 离线审查引擎）
# ---------------------------------------------------------------------------
class ReviewAgent(WorkflowAgentBase):
    """审查代码并输出结构化问题列表（复用 HW1 的 OfflineEngine.review_file）。"""

    name = "review_agent"
    role = "审查 Agent"
    output_kind = "review"

    ISSUE_PATTERNS = (
        ("missing_docstring", "docstring"),
        ("bare_except", "except"),
        ("hardcoded_secret", "密钥"),
        ("too_long", "过长"),
        ("todo_comment", "TODO"),
        ("eval_exec", "eval"),
    )

    def execute(self, state: SharedState) -> Dict[str, Any]:
        target = state.get("review_target")
        if not target:
            raise ValueError("state 中缺少 'review_target'（待审查路径）")

        report = self.offline.review_file(str(target))
        issues = self._parse_issues(report)
        if self.log is not None:
            self.log.node_end(self.name, f"发现 {len(issues)} 个问题")
        return {
            "target": str(target),
            "issues": issues,
            "passed": len(issues) == 0,
            "report": report,
            "source": "offline",
        }

    def _parse_issues(self, report: str) -> List[Dict[str, Any]]:
        issues: List[Dict[str, Any]] = []
        for line in report.splitlines():
            line = line.strip()
            if not line.startswith("- "):
                continue
            text = line[2:]
            m = re.match(r"第 (\d+) 行", text)
            issue_type = "other"
            for name, kw in self.ISSUE_PATTERNS:
                if kw in text:
                    issue_type = name
                    break
            issues.append(
                {
                    "type": issue_type,
                    "line": int(m.group(1)) if m else None,
                    "text": text,
                    "fixable": issue_type
                    in ("missing_docstring", "bare_except", "hardcoded_secret", "todo_comment"),
                }
            )
        return issues


# ---------------------------------------------------------------------------
# 5. 修复 Agent
# ---------------------------------------------------------------------------
class FixAgent(WorkflowAgentBase):
    """按审查结果自动修复可修问题（docstring / 裸 except / 硬编码密钥 / TODO）。"""

    name = "fix_agent"
    role = "修复 Agent"
    output_kind = "fix"

    def execute(self, state: SharedState) -> Dict[str, Any]:
        review = state.get("review")
        if not review:
            raise ValueError("state 中缺少 'review'（上游审查产物）")
        target = review["target"]

        with open(target, "r", encoding="utf-8") as fh:
            source = fh.read()

        fixed, fixed_count, unfixable, notes = self._apply_fixes(source, review["issues"])

        # 语法校验：修复后的代码必须仍然可解析，否则拒绝写入
        try:
            ast.parse(fixed)
        except SyntaxError as exc:
            raise RuntimeError(f"修复后代码语法错误，已放弃写入：{exc}") from exc

        with open(target, "w", encoding="utf-8") as fh:
            fh.write(fixed)

        # 循环里每轮都会覆盖 state["fix"]，因此把累计信息单独记到流程级键上
        state.set("fix_total", int(state.get("fix_total") or 0) + fixed_count, source=self.name)
        if notes:
            state.set(
                "fix_notes", list(state.get("fix_notes") or []) + notes, source=self.name
            )

        if self.log is not None:
            self.log.node_end(
                self.name, f"修复 {fixed_count} 处，剩余 {len(unfixable)} 处需人工处理"
            )
        return {
            "target": target,
            "fixed_count": fixed_count,
            "unfixable": unfixable,
            "notes": notes,
            "source": self.mode,
        }

    def _apply_fixes(self, source: str, issues: List[Dict[str, Any]]):
        lines = source.splitlines()
        fixed_count = 0
        unfixable: List[Dict[str, Any]] = []
        need_import_os = False
        env_vars: List[str] = []

        # 自后往前处理，避免行号偏移
        for issue in sorted(
            (i for i in issues if i.get("fixable")),
            key=lambda x: (x.get("line") or 0),
            reverse=True,
        ):
            line_no = issue.get("line")
            if not line_no or line_no > len(lines):
                unfixable.append(issue)
                continue
            idx = line_no - 1
            if issue["type"] == "missing_docstring":
                indent = re.match(r"\s*", lines[idx]).group(0)
                end = idx
                while end < len(lines) and not lines[end].rstrip().endswith(":"):
                    end += 1  # 找到函数签名结束行（以冒号结尾）
                if end >= len(lines):
                    unfixable.append(issue)
                    continue
                doc_indent = indent + "    "
                lines.insert(end + 1, f'{doc_indent}"""自动补充的文档字符串。"""')
                fixed_count += 1
            elif issue["type"] == "bare_except":
                lines[idx] = re.sub(r"except\s*:", "except Exception:", lines[idx])
                fixed_count += 1
            elif issue["type"] == "hardcoded_secret":
                m = re.search(r"([A-Za-z_][A-Za-z0-9_]*)\s*=\s*['\"][^'\"]+['\"]", lines[idx])
                if m:
                    var = m.group(1)
                    indent = re.match(r"\s*", lines[idx]).group(0)  # 保留原缩进
                    lines[idx] = f'{indent}{var} = os.environ.get("{var.upper()}", "")'
                    need_import_os = True
                    env_vars.append(var.upper())
                    fixed_count += 1
                else:
                    unfixable.append(issue)
            elif issue["type"] == "todo_comment":
                lines[idx] = re.sub(r"#\s*(TODO|FIXME|XXX).*", "# resolved", lines[idx])
                fixed_count += 1

        # import os 统一在所有修复完成后插入（避免迭代中行号偏移），
        # 且必须插在模块 docstring 之后，否则会把 docstring 变成普通字符串。
        if need_import_os and not re.search(r"^import os$", "\n".join(lines), re.M):
            at = _after_module_docstring(lines)
            if at and lines[at - 1].strip():
                lines.insert(at, "")  # docstring 与 import 之间留空行
                at += 1
            lines.insert(at, "import os")
            if at + 1 < len(lines) and lines[at + 1].strip():
                lines.insert(at + 1, "")  # import 与后续代码之间留空行

        for issue in issues:
            if not issue.get("fixable"):
                unfixable.append(issue)

        notes: List[str] = []
        if env_vars:
            notes.append(
                "已将硬编码值改为环境变量读取："
                + "、".join(sorted(set(env_vars)))
                + "（运行前需设置，否则取空串，行为与修复前不同）"
            )

        return "\n".join(lines) + "\n", fixed_count, unfixable, notes


__all__ = [
    "RequirementAgent",
    "DesignAgent",
    "CodingAgent",
    "ReviewAgent",
    "FixAgent",
]
