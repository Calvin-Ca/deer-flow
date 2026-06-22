---
name: norm-qa
description: 造价规范问答技能。接收造价/计量/计价类自然语言问题，检索造价规范条文并由 Qwen3 生成带引用的结构化回答。覆盖《建设工程工程量清单计价规范/标准》GB 50500、《房屋建筑与装饰工程工程量计算规范/标准》GB 50854、《通用安装工程工程量计算标准》GB 50856（2013/2024 双版隔离）。适用于"某构件按什么计量""综合单价含哪些费用""某项目特征怎么描述"等条文查询。强制忠实引用、无依据则拒答、不编造条文。
---

# 造价规范问答（norm-qa）

## 能力

对造价计量计价类中文规范做混合检索（BM25 + 向量语义 + 引用图扩展 + cross-encoder 精排），
交 Qwen3-8B 生成：
- 带条文引用的结构化回答（只引检索到的条文，不杜撰）
- 条文自带强制性字样（"应/不应/严禁"）则照标，无依据则明确拒答
- 不确定方面 / 超范围提示 + 免责声明

## 支持规范（standard 代号，**必填**）

| 代号 | 规范 |
|---|---|
| `gb50500-2013` | 建设工程工程量清单计价规范 GB 50500-2013 |
| `gb50500-2024` | 建设工程工程量清单计价标准 GB/T 50500-2024 |
| `gb50854-2013` | 房屋建筑与装饰工程工程量计算规范 GB 50854-2013 |
| `gb50854-2024` | 房屋建筑与装饰工程工程量计算标准 GB/T 50854-2024 |
| `gb50856-2024` | 通用安装工程工程量计算标准 GB/T 50856-2024 |

> ⚠️ **版本红线**：2013/2024 同 9 位码不同义（如现浇/预制编码段在两版间整体平移），版本错了就串库。
> 调用前**必须**和用户确认查哪部、哪版规范——若用户没说版本，先反问，不要替他猜默认。

## 架构（重要）

本 skill 是一个**薄 HTTP 客户端**。真正的逻辑跑在服务器上的常驻服务里，分两层：
- **任务服务**（`ce-services/main.py`，默认 `http://localhost:8101`）——本 skill 默认打 `/norm/qa`（检索 + 结构化生成）。
- **知识服务**（`ce-code/service/knowledge_api.py`，`python -m service.knowledge_api`，默认 `http://localhost:8100`）——只提供裸检索原语；任务服务 HTTP 调它，`--no-generate` 时本 skill 也直接打它的 `/search`。

`qa.py` 只用 Python 标准库 urllib 转发问题——**沙箱内零第三方依赖，无需 venv、无需向量索引数据、无需 POC 脚本**。

## 前置依赖（需在服务器上运行）

| 服务 | 地址 | 用途 |
|---|---|---|
| **任务服务** | localhost:8101 | 本 skill 默认调用的 HTTP 服务（检索+生成，/norm/qa） |
| **知识服务** | localhost:8100 | 裸检索原语（被任务服务调用；`--no-generate` 直接打 /search） |
| Milvus | localhost:19530 | 向量库（被知识服务使用） |
| vLLM embedding | localhost:8097 | 文本 embedding（被知识服务使用） |
| vLLM Qwen3-8B | localhost:8099 | 结构化生成（被任务服务使用） |

> 服务启动方式（服务器上，常驻；需先起知识服务再起任务服务）：
> `cd ce-code && uv run python -m service.knowledge_api`（知识服务 :8100）
> `cd ce-services && uv run python main.py`（任务服务 :8101）

## 调用方式

**通过 bash 工具调用 `qa.py`**（用系统 `python3` 即可，无需特定 venv）：

```bash
# 基本查询（standard 必填）
python3 /mnt/skills/public/norm-qa/qa.py \
  --query "现浇混凝土矩形柱按什么计量？" \
  --standard gb50854-2024

# 保存结果到文件后读取
python3 /mnt/skills/public/norm-qa/qa.py \
  --query "综合单价由哪些费用构成？" \
  --standard gb50500-2024 \
  --output /tmp/norm_result.json \
&& cat /tmp/norm_result.json
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--query` / `-q` | 必填 | 自然语言问题 |
| `--standard` | **必填（无默认）** | 规范代号（见上表）；2013/2024 错版会串库，须先确认 |
| `--top-k` | `15` | 检索召回条数 |
| `--skip-rerank` | 关 | 跳过 cross-encoder 精排（调试用） |
| `--no-generate` | 关 | 只检索不生成：打知识服务 `/search` 返回裸条款（无 `answer`，多 `clauses`）；供算量/审图等复用 |
| `--service-url` | `http://localhost:8101` | 任务服务地址（环境变量 `NORM_QA_URL`） |
| `--knowledge-url` | `http://localhost:8100` | 知识服务地址，`--no-generate` 时用（环境变量 `NORM_QA_KNOWLEDGE_URL`） |
| `--output` | stdout | 结果 JSON 写入路径（可选） |

## 输出格式

```json
{
  "answer": "自然语言回答正文，含条文引用及免责声明",
  "cited_clauses": [
    {"clause": "E.2.1", "standard": "gb50854-2024", "text": "现浇混凝土柱按设计图示尺寸以体积计算……"}
  ],
  "uncertain_aspects": ["需人工核实的方面，若无则空数组"],
  "out_of_scope_warnings": ["超出该规范适用范围的提示，若无则空数组"],
  "meta": {"standard": "gb50854-2024", "retrieved": 15, "retrieve_ms": 177, "generate_ms": 4300}
}
```

> 零召回时 `answer` 为"未检索到相关条文，无法作答"、`cited_clauses` 为空——这是**正确行为**（不喂空上下文给 LLM 编答案），应如实转达用户、建议换规范或换问法。

## 使用原则

1. **忠实引用**：只引 `cited_clauses` 里检索到的条文，不补充、不编造条文号或原文
2. **版本不猜**：用户没说规范版本就反问，绝不替他选默认版本
3. **无依据拒答**：零召回时明确"未检索到"，不强答
4. **免责声明**：所有回答仅供参考，不替代专业造价审核

## 常见错误排查

> 这些都是**服务端配置问题**，agent 在沙箱内无法补救（不要尝试建 venv、装包或拷脚本）。
> 应把错误原文转达用户，由用户在服务器侧处理。

| 错误信息 | 原因 | 处理（在服务器上） |
|---|---|---|
| `无法连接服务`（默认路径） | 8101 任务服务未启动 | `cd ce-services && uv run python main.py` |
| `无法连接服务`（--no-generate） | 8100 知识服务未启动 | `cd ce-code && uv run python -m service.knowledge_api` |
| `未知规范代号` | `--standard` 拼错或传了不支持的代号 | 用上表代号；确认规范+版本 |
| 返回 400 `未知规范` | 知识服务侧该规范别名未配 | 检查 ce-code `config.STANDARD_ALIASES` |
| 返回 503 `索引未就绪` | 该规范向量索引未建 | 在 ce-code 按 README 建对应 `building_code_*` 索引 |
| 返回 502 `LLM ...` | Qwen3 服务未启动/输出非法 | `curl http://localhost:8099/health` 检查 |
