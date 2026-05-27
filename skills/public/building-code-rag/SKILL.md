---
name: building-code-rag
description: 建筑规范条文检索技能。接收自然语言查询，返回结构化条款结果（含原文、条文号、强制性标识）。当前支持《建筑设计防火规范》GB 50016-2014(2018)。适用于条文查询、合规判定辅助等场景。强条必须 100% 召回，is_mandatory=true 的条款不得遗漏。
---

# 建筑规范 RAG 检索技能

## 能力

对中文建筑规范进行混合检索（BM25 + 向量语义 + 引用图扩展），返回：
- 相关条款原文（含条文号）
- 强制性 / 推荐性明确区分（`is_mandatory`）
- 自动拉取关联引用条款
- Qwen3-8B 生成结构化回答

## 当前支持规范

| 代号 | 规范 |
|---|---|
| `gb50016` | 《建筑设计防火规范》GB 50016-2014(2018) |

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| Milvus | localhost:19530 | 向量库 |
| vLLM BGE-large | localhost:8097 | 文本 embedding |
| vLLM Qwen3-8B | localhost:8099 | 结构化生成 |

## 调用方式

**通过 bash 工具调用 `retrieve.py`**：

```bash
# 基本查询（输出 JSON 到 stdout）
cd /mnt/nvme/calvin/code/deer-flow
building-code-rag-poc/.venv/bin/python \
  skills/public/building-code-rag/retrieve.py \
  --query "防火墙的耐火极限要求是多少？"

# 保存结果到文件后读取
building-code-rag-poc/.venv/bin/python \
  skills/public/building-code-rag/retrieve.py \
  --query "24米高住宅疏散楼梯最小净宽" \
  --output /tmp/rag_result.json \
&& cat /tmp/rag_result.json
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--query` / `-q` | 必填 | 自然语言查询问题 |
| `--standard` | `gb50016` | 规范代号（见上表） |
| `--top-k` | `20` | 最终返回条款数（强条不截断） |
| `--skip-rerank` | 关 | 跳过 Rerank，用 RRF 排序（调试用） |
| `--output` | stdout | 结果 JSON 写入路径（可选） |

## 输出格式

```json
{
  "query": "用户查询",
  "standard": "GB_50016-20142018",
  "retrieved_clauses_count": 20,
  "mandatory_clauses_count": 8,
  "response": {
    "answer": "自然语言回答正文，含条文引用及免责声明",
    "applicable_clauses": [
      {
        "clause": "6.1.1",
        "standard": "GB 50016-2014(2018)",
        "text": "防火墙的耐火极限不应低于 3.00h。",
        "is_mandatory": true,
        "relevance": "direct"
      }
    ],
    "referenced_clauses": [
      {
        "clause": "6.1.2",
        "standard": "GB 50016-2014(2018)",
        "text": "...",
        "is_mandatory": false
      }
    ],
    "uncertain_aspects": ["需人工核实的方面，若无则空数组"],
    "out_of_scope_warnings": ["超出规范适用范围的提示，若无则空数组"]
  }
}
```

## 使用原则

1. **强条不可漏**：`is_mandatory: true` 的条款必须全部呈现，宁可多召回也不能漏
2. **强制性与推荐性分开陈述**：不得将"必须/严禁/不应"与"宜/可"合并描述
3. **无依据则拒答**：若规范无相关条文，明确说明"本规范未涉及"，不得编造
4. **免责声明**：所有回答仅供参考，不替代专业审查

## 常见错误排查

| 错误信息 | 原因 | 处理 |
|---|---|---|
| `向量索引目录不存在` | GB 50016 索引未建 | 服务器上运行 `uv run scripts/04_build_index.py --standard gb50016` |
| `检索失败` | Milvus 或 embedding 服务未启动 | `curl http://localhost:8097/health` 检查服务 |
| `生成失败` | Qwen3 服务未启动 | `curl http://localhost:8099/health` 检查服务 |
| `POC 模块加载失败` | 代码目录结构异常 | 确认 `building-code-rag-poc/scripts/` 目录存在 |
