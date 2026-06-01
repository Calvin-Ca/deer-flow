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

## 架构（重要）

本 skill 是一个**薄 HTTP 客户端**。真正的检索/生成逻辑跑在服务器上一个
**常驻 HTTP 服务**（`building-code-rag-poc/service/server.py`，默认
`http://localhost:8100`）里。`retrieve.py` 只用 Python 标准库 urllib 把查询
转发过去——**沙箱内零第三方依赖，无需 venv、无需向量索引数据、无需 POC 脚本**。

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| **检索服务** | localhost:8100 | 本 skill 直接调用的 HTTP 服务 |
| Milvus | localhost:19530 | 向量库（被检索服务使用） |
| vLLM BGE-large | localhost:8097 | 文本 embedding（被检索服务使用） |
| vLLM Qwen3-8B | localhost:8099 | 结构化生成（被检索服务使用） |

> 检索服务启动方式（服务器上，一次性常驻）：
> ```bash
> cd building-code-rag-poc
> .venv/bin/python service/server.py        # 监听 0.0.0.0:8100
> ```

## 调用方式

**通过 bash 工具调用 `retrieve.py`**（用系统 `python3` 即可，无需特定 venv）：

```bash
# 基本查询（输出 JSON 到 stdout）
python3 /mnt/skills/public/building-code-rag/retrieve.py \
  --query "防火墙的耐火极限要求是多少？"

# 保存结果到文件后读取
python3 /mnt/skills/public/building-code-rag/retrieve.py \
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
| `--service-url` | `http://localhost:8100` | 检索服务地址（也可用环境变量 `BUILDING_CODE_RAG_URL`） |
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
  },
  "meta": {
    "request_id": "a1b2c3d4",
    "bm25_hits": 40, "vector_hits": 40, "merged": 55, "expanded": 62,
    "final": 20, "mandatory": 8,
    "retrieve_ms": 850, "generate_ms": 3200, "elapsed_ms": 4050
  }
}
```

> `meta` 是过程可观测性摘要（各阶段命中数 + 耗时）；保存到文件时也会在 stderr 打一行摘要。

## 使用原则

1. **强条不可漏**：`is_mandatory: true` 的条款必须全部呈现，宁可多召回也不能漏
2. **强制性与推荐性分开陈述**：不得将"必须/严禁/不应"与"宜/可"合并描述
3. **无依据则拒答**：若规范无相关条文，明确说明"本规范未涉及"，不得编造
4. **免责声明**：所有回答仅供参考，不替代专业审查

## 常见错误排查

> 这些都是**服务端配置问题**，agent 在沙箱内无法补救（不要尝试建 venv、装包或拷脚本）。
> 应把错误原文转达用户，由用户在服务器侧处理。

| 错误信息 | 原因 | 处理（在服务器上） |
|---|---|---|
| `无法连接检索服务` | 8100 服务未启动 | `cd building-code-rag-poc && .venv/bin/python service/server.py` |
| 返回 503 `向量索引未就绪` | GB 50016 索引未建 | `uv run scripts/04_build_index.py --standard gb50016` |
| 返回 500 `检索失败` | Milvus 或 embedding 服务未启动 | `curl http://localhost:8097/health`、Milvus 19530 检查 |
| 返回 500 `生成失败` | Qwen3 服务未启动 | `curl http://localhost:8099/health` 检查服务 |
