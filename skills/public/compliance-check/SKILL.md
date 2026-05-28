---
name: compliance-check
description: 建筑规范项目级合规审查技能。引导用户完成多轮参数收集，派发 compliance-checker sub-agent 执行完整检查（参数提取→并行检索→逐维度合规判定→反思校验），输出结构化强条清单（含逐条判定状态）。也支持直接 bash 调用。基于《建筑设计防火规范》GB 50016-2014(2018)。
---

# 建筑规范合规审查

## 能力

用户描述项目 → 完整合规报告：

1. **多轮参数收集**：识别描述中的模糊信息，主动追问补充
2. **参数提取**：Qwen3-8B 从自由文本提取建筑类型、高度、面积、用途等结构化参数
3. **维度展开**：按 GB 50016 合规维度自动展开 8-16 个检索查询
4. **并行检索**：调用 building-code-rag 检索模块，合并去重
5. **合规判定**：Qwen3-8B 逐条给出：符合 / 不符合 / 需核实 / 需补充信息 / 不适用
6. **反思校验**：检查是否有合规维度遗漏

## 与 building-code-rag 的关系

| | building-code-rag | compliance-check |
|---|---|---|
| 用户输入 | 一个具体问题 | 项目参数描述 |
| 查询来源 | 用户提问 | 系统按维度自动展开 |
| 覆盖范围 | 问什么答什么 | 主动穷举所有适用维度 |
| 输出 | 相关条款 + 回答 | 全量强条清单 + 逐条判定 |

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| Milvus | localhost:19530 | 向量库 |
| vLLM BGE-large | localhost:8097 | 文本 embedding |
| vLLM Qwen3-8B | localhost:8099 | 参数提取 + 合规判定 |

---

## 调用方式一：deer-flow 对话模式（推荐）

### 第一步：评估参数完整性

收到合规检查请求后，判断描述是否包含**必要参数**：

| 参数 | 重要性 |
|---|---|
| 建筑用途（住宅/办公/商业等） | 必须 |
| 建筑高度或层数 | 必须 |
| 每层面积 | 推荐 |
| 地下层数/用途 | 如有则必须说明 |

- 关键参数不足 → 主动追问（一次最多问 2 个问题）
- 关键参数已足够 → 直接进入下一步，不要过度追问

### 第二步：派发 compliance-checker sub-agent

```
task(
  description="建筑合规检查",
  subagent_type="compliance-checker",
  prompt="请对以下建筑项目执行完整的 GB 50016 合规检查：\n\n<用户提供的完整项目描述>"
)
```

告知用户检查正在运行，预计需要 2-5 分钟。

### 第三步：呈现报告

收到 sub-agent 结果后，结构化呈现：

```
## 合规检查报告

**建筑类别**：[类别]
**强条总计**：[N] 条（GB 50016-2014(2018)）

### [维度名称]
| 条款号 | 状态 | 说明 |
|---|---|---|
| 5.3.1 | ✅ 符合 | 每层850m²<1500m²限值 |
| 5.5.27 | ⚠️ 需核实 | 须设防烟楼梯间，需图纸确认 |

**需补充参数**：[若有]
> 以上结果仅供参考，不替代专业审查。
```

状态图标：✅ 符合 / ❌ 不符合 / ⚠️ 需核实 / ❓ 需补充信息

### 第四步：支持追问

- 解释具体条款含义
- 若用户补充了重要参数，询问是否重新执行检查
- 若报告有 `missed_dimensions_warning`，说明覆盖盲区（如火灾自动报警需参考 GB 50116）

---

## 调用方式二：bash 直接调用

```bash
cd /mnt/nvme/calvin/code/deer-flow

# 基本调用
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "地上11层住宅楼，总高32米，每层850平方米，地下一层车库"

# 保存报告
building-code-rag-poc/.venv/bin/python \
  skills/public/compliance-check/check.py \
  --project "..." \
  --output /tmp/compliance_report.json
```

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--project` / `-p` | 必填 | 项目自由文本描述 |
| `--standard` | `GB_50016-20142018` | 规范代号 |
| `--skip-reflection` | 关 | 跳过反思校验（调试用） |
| `--output` | stdout | 报告 JSON 写入路径（可选） |

---

## 输出格式

```json
{
  "project_params": {
    "building_type": "住宅",
    "building_category": "二类高层住宅",
    "height_m": 32,
    "floors_above_ground": 11,
    "floors_underground": 1,
    "floor_area_m2": 850,
    "ambiguities": ["耐火等级未说明"]
  },
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
  "mandatory_clauses_total": 85,
  "uncertain_params": ["耐火等级未说明"],
  "missed_dimensions_warning": [],
  "disclaimer": "以上结果仅供参考，不替代具有执业资格的注册工程师专业审查。"
}
```

## 使用原则

1. **强条不可漏**：`is_mandatory: true` 的条款必须全部出现在报告中
2. **状态要诚实**：缺少参数时填"需补充信息"，不得猜测
3. **免责声明**：所有报告末尾必须含免责声明

## 常见错误排查

| 错误信息 | 原因 | 处理 |
|---|---|---|
| `向量索引目录不存在` | 索引未建 | 服务器运行 `uv run scripts/04_build_index.py` |
| `服务调用失败` | Milvus / vLLM 未启动 | `curl http://localhost:8097/health` 检查 |
