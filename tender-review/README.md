# tender-review

招投标智能评审域项目，与 `ce-code/`、`backend/` 平级。

本项目负责把招标文件、投标文件和项目评审办法转换为可执行的评审任务，输出带原文证据、规则依据和人工复核状态的评审报告。系统只提供辅助评审结论，最终定标或否决决定由有权限的评审人员作出。

## 当前能力

- 定义资格、符合性、技术、商务和报价五类评审范围。
- 以可插拔规则组成评审流水线。
- 每条问题强制关联证据位置，避免无依据结论。
- 按问题严重性生成 `pass`、`manual_review` 或 `reject` 建议。
- 提供纯 Python 单元测试，不依赖外部模型或服务。

当前是 Phase 0 工程骨架。文档解析、评审规则库、RAG/LLM、API 和前端尚未接入，详见 [TODO.md](TODO.md)。

## 目录

```text
tender-review/
├── data/                       # 原始文件、解析产物、结构化数据和评审结果
├── src/tender_review/          # 领域模型与评审流水线
├── tests/                      # 单元测试
├── PRD.md                      # 产品范围与验收原则
├── DEV.md                      # 技术设计与开发约定
└── TODO.md                     # 迭代计划
```

## 本地验证

要求 Python 3.12 和 uv。首次进入本目录后执行：

```bash
uv sync --extra dev
uv run pytest
uv run ruff check .
```

按仓库约定，本地不提交 `uv.lock`；锁文件由实际部署环境统一维护。

## 最小用法

```python
from tender_review import ReviewContext, ReviewPipeline

context = ReviewContext(
    project_id="demo-project",
    tender_document_id="tender-v1",
    bid_document_id="bidder-a-v1",
)
report = ReviewPipeline([]).run(context)
assert report.recommendation.value == "pass"
```
