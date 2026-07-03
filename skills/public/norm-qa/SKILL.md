---
name: norm-qa
description: 造价规范问答技能。接收造价/计量/计价类自然语言问题，检索造价规范条文并由 Qwen3 生成带引用的结构化回答。覆盖《建设工程工程量清单计价规范/标准》GB 50500、《房屋建筑与装饰工程工程量计算规范/标准》GB 50854、《通用安装工程工程量计算标准》GB 50856（2013/2024 双版隔离）。适用于"某构件按什么计量""综合单价含哪些费用""某项目特征怎么描述"等条文查询。强制忠实引用、无依据先联网兜底（Tier-2 降级标注）仍无则拒答、不编造条文；口径反问会话内仅首次（EH-05 会话粘性）。
---

# 造价规范问答（norm-qa）

## ⛔ 红线（最优先，先于一切回答）

1. **口径反问：会话内仅首次（EH-05 会话粘性）**：用户没说查哪个地区、哪版规范时，**本会话首次**用 `ask_clarification` 问一次「请问您咨询的是哪个地区、哪个版本的规范（2013/2024）？」；用户确认后**会话内记住并沿用**，同会话后续问题**不再重复问**（除非用户改口径）。会话早前已说明过口径、或问题里自带版本/规范号，直接沿用不问。哪部规范（房建/安装/计价）**不用问**——服务端按问题类型确定性裁定（T-A2），你只需要口径（地区+版本）。
2. **只调工具、不自答**：造价/计量/计价类规范条文问题，一律**优先调用 MCP 工具 `ce-task_norm_qa`** 检索后作答，**严禁**凭记忆直接回答条文/编码，**严禁自己用联网搜索代替**（联网兜底在服务端有三道闸，你不要自己上网）。
3. **忠实引用、分级呈现**：本地命中 → 只引工具返回 `cited_clauses` 里的条文，**不补充、不编造**条文号或原文。本地零召回时服务端会**自动走联网兜底**（FR-K07）：返回的 `answer` 自带「⚠️ 联网检索结果」降级标注头 + `web_citations`（URL+访问日期）——**原样保留标注头与来源**转达，不当权威条文口径呈现。联网仍无可信命中 → `answer` 为拒答（含已查范围+建议渠道），如实转达不强答。

**首选：MCP 工具 `ce-task_norm_qa`**——工具的检索条文/cited_clauses 会**结构化渲染进对话中间过程**（用户看得见「凭哪条条文答的」），优于 bash 把结论埋进 stdout：

```
调用工具 ce-task_norm_qa，参数：
  query    = "<用户原问题>"
  standard = <可选 hint：gb50500-2013|gb50500-2024|gb50854-2013|gb50854-2024|gb50856-2024>
             （会话已确认口径→带对应代号；服务端确定性定族，hint 错族会被夺回）
```

> bash `qa.py`（见下「调用方式」）保留作 **curl/无 MCP 环境兜底**；对话主路径走 MCP 工具。

## 能力

对造价计量计价类中文规范做混合检索（BM25 + 向量语义 + 引用图扩展 + cross-encoder 精排），
交 Qwen3-8B 生成：
- 带条文引用的结构化回答（只引检索到的条文，不杜撰）
- 条文自带强制性字样（"应/不应/严禁"）则照标，无依据则明确拒答
- 不确定方面 / 超范围提示 + 免责声明

## 支持规范（standard 代号，**可选 hint**）

| 代号 | 规范 |
|---|---|
| `gb50500-2013` | 建设工程工程量清单计价规范 GB 50500-2013 |
| `gb50500-2024` | 建设工程工程量清单计价标准 GB/T 50500-2024 |
| `gb50854-2013` | 房屋建筑与装饰工程工程量计算规范 GB 50854-2013 |
| `gb50854-2024` | 房屋建筑与装饰工程工程量计算标准 GB/T 50854-2024 |
| `gb50856-2024` | 通用安装工程工程量计算标准 GB/T 50856-2024 |

> ⚠️ **版本红线**：2013/2024 同 9 位码不同义（如现浇/预制编码段在两版间整体平移），版本错了就串库——
> 所以口径（版本）要问用户（红线 1，会话内仅首次）。**哪部规范不用问**：服务端 `standard_router`
> 按问题类型确定性裁定（计量→50854 / 计价→50500 / 安装→50856），hint 错族会被夺回。

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

### 首选：MCP 工具 `ce-task_norm_qa`（对话主路径）

直接作为 function-calling 工具调用，参数 `query`（必填）/ `standard`（可选 hint，见上表）/ `top_k`（默认 15）/ `skip_rerank`（默认关）。返回结构同下「输出格式」，且 `cited_clauses` 会在对话中间过程里渲染成依据条目。注册见 `extensions_config.json` 的 `ce-task`（`http://localhost:8101/mcp`），任务服务 :8101 起着即可用。

### 兜底：bash 调用 `qa.py`（curl/无 MCP 环境）

**通过 bash 工具调用 `qa.py`**（用系统 `python3` 即可，无需特定 venv）；`--no-generate` 取裸条款仅此路径有：

```bash
# 基本查询（standard 必填）
python3 /mnt/skills/public/norm-qa/qa.py \
  --query "现浇混凝土矩形柱按什么计量？" \
  --standard gb50854-2024

# 保存结果到文件后读取
python3 /mnt/skills/public/norm-qa/qa.py \
  --query "综合单价由哪些费用构成？" \
  --standard gb50500-2024 \
  --output /mnt/user-data/workspace/norm_result.json \
&& cat /mnt/user-data/workspace/norm_result.json
```

### 参数说明

| 参数 | 默认值 | 说明 |
|---|---|---|
| `--query` / `-q` | 必填 | 自然语言问题 |
| `--standard` | 可选 hint（`--no-generate` 时必填） | 规范代号（见上表）；服务端确定性定族，错族被夺回 |
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

> **零召回 → 联网兜底（FR-K07，服务端自动）**：本地库无命中时服务端自动走三道闸联网兜底——
> 命中可信源则 `answer` 带「⚠️ 非本系统深圳·2013 权威口径，联网检索结果，请人工核验」标注头，
> `web_citations` 带 URL + 访问日期 + 域名层级（`meta.guard.tier="web"`）；仍无可信命中才返回拒答
> （含已查范围 + 建议渠道）。两种都是**正确行为**，如实转达：标注头与来源**一字不删**。

## 使用原则

1. **忠实引用**：本地命中只引 `cited_clauses`，联网兜底只引 `web_citations`，不补充、不编造
2. **口径反问仅首次**：会话内首次缺口径问一次（地区+版本），确认后沿用不再问（EH-05 会话粘性）
3. **分级呈现**：联网结果的降级标注头原样保留，不冒充权威口径；仍无可信命中如实拒答不强答
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
