---
name: compliance-check
description: 建筑规范项目级合规检查技能。接收项目自由文本描述，自动提取建筑参数、展开合规维度、并行检索强条，输出结构化合规报告（含逐条判定状态）。基于《建筑设计防火规范》GB 50016-2014(2018)。强条必须 100% 召回，is_mandatory=true 的条款不得遗漏。
---

# 建筑规范项目级合规检查技能

## 能力

用户描述项目参数 → 输出所有适用强条 + 逐条合规判定：

1. **参数提取**：Qwen3-8B 从自由文本中提取建筑类型、高度、面积、用途等结构化参数
2. **维度展开**：按 GB 50016 合规维度（防火间距/防火分区/疏散/消防设施等）自动展开 8-15 个检索查询
3. **并行检索**：并行调用 building-code-rag 检索模块，合并去重
4. **合规判定**：Qwen3-8B（thinking 模式）逐条给出：符合 / 不符合 / 需核实 / 需补充信息 / 不适用
5. **反思校验**：检查是否有合规维度遗漏

## 与 building-code-rag 的关系

| | building-code-rag | compliance-check |
|---|---|---|
| 用户输入 | 一个具体问题 | 项目参数描述 |
| 查询来源 | 用户提问 | 系统自动展开 8-15 个维度 |
| 覆盖范围 | 问什么答什么 | 主动穷举所有适用维度 |
| 输出 | 相关条款 + 回答 | 全量强条清单 + 逐条判定 |

`compliance-check` 内部多次调用 `building-code-rag` 的检索模块，是其编排层。

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| Milvus | localhost:19530 | 向量库 |
| vLLM BGE-large | localhost:8097 | 文本 embedding |
| vLLM Qwen3-8B | localhost:8099 | 参数提取 + 合规判定 |

## 调用方式

**通过 bash 工具调用 `check.py`**：

```bash
cd /mnt/nvme/calvin/code/deer-flow

# 基本调用（输出 JSON 到 stdout）
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库，位于城市建成区"

# 保存报告到文件
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "..." \
  --output /tmp/compliance_report.json \
&& cat /tmp/compliance_report.json

# 跳过反思校验（调试加速）
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "..." \
  --skip-reflection
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--project` / `-p` | 必填 | 项目自由文本描述 |
| `--standard` | `GB_50016-20142018` | 规范代号 |
| `--skip-reflection` | 关 | 跳过反思校验（调试用） |
| `--output` | stdout | 报告 JSON 写入路径（可选） |

## 输出格式

```json
{
  "project_description": "原始项目描述",
  "project_params": {
    "building_type": "住宅",
    "building_category": "二类高层住宅",
    "height_m": 32,
    "floors_above_ground": 11,
    "floors_underground": 1,
    "floor_area_m2": 850,
    "ambiguities": ["耐火等级未说明"]
  },
  "building_category": "二类高层住宅",
  "dimensions": [
    {
      "dimension": "防火分区",
      "clauses": [
        {
          "clause": "5.3.1",
          "text": "高层民用建筑每个防火分区最大建筑面积：1500m²",
          "is_mandatory": true,
          "compliance_status": "符合",
          "note": "每层850m²<1500m²限值"
        }
      ]
    }
  ],
  "mandatory_clauses_total": 18,
  "uncertain_params": ["耐火等级未说明", "地下车库面积未说明"],
  "missed_dimensions_warning": [],
  "disclaimer": "以上结果仅供参考，不替代具有执业资格的注册工程师专业审查。"
}
```

## 使用原则

1. **强条不可漏**：`is_mandatory: true` 的条款必须全部出现在报告中
2. **状态要诚实**：缺少参数时必须填"需补充信息"，不得猜测
3. **免责声明**：所有报告末尾必须含免责声明

## 常见错误排查

| 错误信息 | 原因 | 处理 |
|---|---|---|
| `向量索引目录不存在` | 索引未建 | 服务器运行 `uv run scripts/04_build_index.py` |
| `模块加载失败` | 代码目录异常 | 确认 `building-code-rag-poc/scripts/` 存在 |
| `服务调用失败` | Milvus / vLLM 未启动 | `curl http://localhost:8097/health` 检查 |
