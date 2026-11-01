"""HW2-Workflow 便捷入口。

用法示例：
    python main.py demo                        # 一键运行两条演示流程
    python main.py --offline demo              # 强制离线（不调 LLM，秒级完成）
    python main.py mini-se --requirement "..." # 运行 Mini SE 流程
    python main.py review-fix examples/sample.py
    python main.py graph                       # 打印 Mermaid 流程图
"""
from workflow.cli import main

if __name__ == "__main__":
    raise SystemExit(main())
