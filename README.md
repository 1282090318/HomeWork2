# HW2-Workflow：Simple Workflow MVP（多 Agent 协作的 Workflow 编排）

> SE2026 课程实践 · 作业二（Homework 2）
> 目标：搭建一个 **2-3 个 Agent 协作的简单 Workflow**，理解 Workflow 编排、Agent 间通信、状态管理等核心概念。

本项目实现了一个**零第三方依赖**（纯 Python 标准库）的多 Agent Workflow 框架，并基于它构建了两条可端到端运行的演示流程：

1. **Mini SE 流程**：需求 Agent → 设计 Agent → 编码 Agent →（并行）审查
2. **代码审查修复流程**：审查 Agent ⇄ 修复 Agent（循环，直到无可修复问题）

支持**离线 / 在线双模式**：配置 OpenAI 兼容 API Key 时由 LLM 驱动各 Agent；无 Key 时自动降级到内置规则引擎，**无网络也能完整演示**。

---

## 一、功能特性（对照作业要求）

### 必须实现 ✅

| 要求 | 实现 |
|------|------|
| 至少 2 个 Agent 协作完成完整流程 | Mini SE 流程 3 个 Agent（需求/设计/编码）+ 审查修复流程 2 个 Agent（审查/修复），均端到端跑通 |
| Agent 间消息传递或状态共享 | 两者都有：`MessageBus` 发布/订阅消息传递 + `SharedState` 黑板式状态共享 |
| 基本流程控制（顺序执行） | `Workflow` 顺序编排，上游产出写入状态、下游自动接续 |
| MVP 可运行 | `python main.py demo` 一键运行，22 个单元测试全部通过 |
| LLM 接入 | 需求/设计/编码 Agent 均接入 OpenAI 兼容接口（本项目已实测智谱 GLM） |

### 推荐实现 ✅

| 要求 | 实现 |
|------|------|
| 条件分支 | `Branch` 节点（`if/else`） |
| 循环 | `Loop` 节点（`while 条件 and iter < max_iters`），审查-修复循环即由它驱动 |
| 并行执行 | `Parallel` 节点（线程池），Mini SE 流程并行审查多个生成文件 |
| 可视化编排界面（可选） | `python main.py graph` 导出 Mermaid 流程图，可直接贴入支持 Mermaid 的文档 |
| 执行日志与状态追踪 | `EventLog` 记录每个节点的开始/结束/错误，运行后落盘 `*_log.txt`、`*_state.json` |
| 错误处理与恢复 | 节点级重试（线性退避）、`raise/continue` 两种错误策略、修复后语法校验（不合法则拒绝写入） |
| LLM 生成兜底 | 编码 Agent 由 LLM 生成代码，生成结果先做 `ast.parse` 语法校验，不合格自动回退模板，保证流程不中断 |

### 从项目 1 复用 ✅

`codeagent/` 目录**原样复用 HW1 的 CodeAgent 项目**：

- `LLMClient`（OpenAI 兼容客户端，含指数退避重试）→ 作为各 Agent 的在线模式后端；
- `OfflineEngine`（基于 `ast` 的规则审查引擎）→ 作为审查 Agent 的离线后端；
- `ToolRegistry`（文件读取/列目录/代码执行/搜索）→ 供 Agent 调用。

---

## 二、项目结构

```
HW2-Workflow/
├─ main.py                     # 入口
├─ requirements.txt            # 依赖说明（零第三方依赖）
├─ workflow/                   # HW2 核心：Workflow 框架
│  ├─ core.py                  # Message / MessageBus / SharedState / EventLog / BaseAgent
│  ├─ engine.py                # Workflow 引擎：Node/Branch/Loop/Parallel + 重试与错误策略
│  ├─ agents.py                # 5 个 Agent：需求/设计/编码/审查/修复（离线+在线双模式）
│  ├─ flows.py                 # 两条演示流程 + 产物落盘
│  └─ cli.py                   # 命令行入口
├─ codeagent/                  # 复用自 HW1（LLM 客户端 + 离线规则引擎 + 工具集）
├─ examples/
│  ├─ requirement.txt          # 示例需求
│  └─ sample.py                # 示例"问题代码"（供 review-fix 演示）
├─ tests/
│  └─ test_workflow.py         # 22 个单元测试（控制流/基础设施/端到端/回归）
├─ .gitignore                  # 忽略 output/、__pycache__、本地密钥等
└─ output/                     # 运行产物（日志/状态快照/流程图/生成代码），不入库
```

---

## 三、快速开始

环境要求：**Python ≥ 3.9**，无需安装任何第三方包。

### 1. 一键演示（推荐先跑这个）

```bash
cd HW2-Workflow
python main.py demo
```

依次运行两条流程并打印执行日志与结果摘要，产物保存到 `output/`。

> 运行过程中每个节点的开始/结束、循环迭代、错误都会**实时打印到控制台**（`[需求分析] node_start` 等），
> 无需等整条流程结束才有输出；联网模式下也能一眼看出卡在哪个节点。

> 配置了 API Key 时，各 Agent 会调用 LLM，耗时较长；若只想快速验证流程，可强制离线：
> ```bash
> python main.py --offline demo
> ```
> 注意 `--offline` 是全局参数，要写在子命令 `demo` 前面。

### 2. Mini SE 流程（需求 → 设计 → 代码 → 并行审查）

```bash
# 使用内置示例需求
python main.py mini-se

# 指定需求文本
python main.py mini-se --requirement "开发一个个人任务管理小工具。必须支持任务的创建、删除和查询。"

# 从文件读取需求
python main.py mini-se --file examples/requirement.txt --out output/mini-se
```

产出：结构化需求 → 模块设计 → **可直接运行的 Python 源码**（`output/mini-se/generated/*.py`）→ 并行审查报告。

### 3. 代码审查修复流程（审查 → 修复，循环）

```bash
# 使用内置问题代码
python main.py review-fix

# 审查并修复自己的文件（会自动复制到 output 目录再修，不改原文件）
python main.py review-fix examples/sample.py
```

循环逻辑：审查发现问题 → 自动修复 → 重新审查 → …… 直到没有可修复问题（或达到最大迭代次数 3）。

### 4. 导出流程图

```bash
python main.py graph
```

### 5. 运行测试

```bash
python -m unittest tests.test_workflow -v
```

---

## 四、离线 / 在线模式

| 模式 | 条件 | 行为 |
|------|------|------|
| **离线（默认）** | 未配置 Key 或 `--offline` | 需求解析=规则；模块设计=模板；代码生成=模板；审查/修复=规则。秒级完成，完全可复现 |
| **在线** | 配置 API Key | 需求分析、架构设计、编码均由 LLM 完成；审查/修复仍用规则引擎（保证结论稳定） |

各 Agent 的实际驱动方式：

| Agent | 在线模式 | 离线模式 |
|-------|----------|----------|
| RequirementAgent | LLM 解析需求 | 关键词规则 |
| DesignAgent | LLM 输出模块设计 | 模板映射 |
| CodingAgent | LLM 生成源码（语法校验 + 失败回退模板） | 模板生成 |
| ReviewAgent | 规则（`ast` 静态分析） | 规则 |
| FixAgent | 规则（文本修复） | 规则 |

切换在线模式（任意 OpenAI 兼容服务，零代码改动）：

```bash
# Windows PowerShell（智谱 GLM 示例，本项目已实测）
$env:OPENAI_API_KEY="你的智谱Key"
$env:OPENAI_BASE_URL="https://open.bigmodel.cn/api/paas/v4"
$env:OPENAI_MODEL="glm-5.3-flashx"
python main.py mini-se

# Linux / macOS（DeepSeek 示例）
export OPENAI_API_KEY="sk-xxxx"
export OPENAI_BASE_URL="https://api.deepseek.com"
export OPENAI_MODEL="deepseek-chat"
python main.py demo
```

常用服务商：

| 服务 | OPENAI_BASE_URL | OPENAI_MODEL |
|------|-----------------|--------------|
| 智谱 GLM | https://open.bigmodel.cn/api/paas/v4 | glm-5.3-flashx |
| DeepSeek | https://api.deepseek.com | deepseek-chat |
| OpenAI | https://api.openai.com/v1 | gpt-4o-mini |
| 本地 Ollama | http://localhost:11434/v1 | qwen2.5:7b |

如何确认真的走了 AI：运行日志中出现 `（llm 模式）`，编码节点显示 `AI 生成 N 个`，即为在线链路打通；若显示 `offline` 或出现“LLM 调用失败，降级离线”，则说明 Key/网络/模型名有问题。

> 在线模式下 LLM 不可用（网络错误/超时）时，Agent 会**自动降级到离线规则引擎**并记录日志，保证流程不中断。
> 测试与快速演示可设置 `CODEAGENT_OFFLINE=1` 或使用 `python main.py --offline demo` 强制离线。

---

## 五、运行产物说明

每次运行会在 `--out` 目录（默认 `output/<流程名>`）生成：

| 文件 | 内容 |
|------|------|
| `*_log.txt` | 执行日志（每个节点的开始/结束/错误，含耗时） |
| `*_state.json` | 最终共享状态快照（需求/设计/代码/审查/修复全部产物） |
| `*_flow.md` | 该流程的 Mermaid 流程图 |
| `*_messages.json` | Agent 间消息历史（通信实证） |
| `generated/` | Mini SE 流程生成的源码文件 |

> 审查修复流程会在 `*_state.json` 的 `fix_notes` 与 `fix_total` 中记录累计的
> "修复带来的语义变更"（例如硬编码值被改成环境变量读取）与累计修复处数，
> 方便人工复核自动修改是否符合预期。

---

## 六、设计文档

架构设计、核心机制与扩展方式详见 [Design.md](Design.md)。

## 七、提交信息

- 代码仓库：GitHub/Gitee（见课程提交要求）
- 备份压缩包：学号姓名.rar（< 200M）
