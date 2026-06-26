# 提示词工程（Prompt Engineering）完整指南

## 1. 什么是提示词工程

提示词工程（Prompt Engineering）是指通过设计、优化和组织输入给大语言模型（LLM）的提示（Prompt），引导模型产生更加准确、稳定、可控且符合预期的输出。

随着 GPT、Claude、Gemini、Qwen、DeepSeek 等模型能力不断增强，提示词工程已经从“如何写一句 Prompt”，发展到 **Prompt + Workflow + Tool + Memory** 的系统化设计。

---

## 2. Prompt 的基本组成

一个优秀的 Prompt 通常包含以下几个部分：

- **角色（Role）**
- **任务（Task）**
- **背景（Context）**
- **输入（Input）**
- **约束（Constraint）**
- **输出格式（Output Format）**

### 示例

```text
角色：你是一名资深 Java 开发工程师。

任务：帮助分析下面代码中的 Bug。

背景：项目采用 Spring Boot + MyBatis。

要求：
1. 找出 Bug
2. 分析原因
3. 给出修改方案
4. 给出修改后的代码

输出格式：Markdown
```

---

## 3. Prompt 的六大核心原则

### （1）目标明确（Specific）

❌ 不好的 Prompt

```text
介绍一下 Python
```

✅ 好的 Prompt

```text
请面向 Java 程序员介绍 Python，
重点介绍语法差异、适用场景、常见库，
控制在 1000 字以内。
```

### （2）上下文充分（Context）

模型不了解你的业务，需要提供：

- 项目背景
- 用户身份
- 行业
- 已知信息
- 输入数据

例如：

```text
背景：
我们是一家建筑造价软件公司，
正在开发智能组价 Agent。
```

### （3）约束条件明确（Constraint）

例如：

- 不要输出 JSON
- 不要杜撰内容
- 不知道请直接回答不知道
- 必须引用规范编号
- 字数控制 500 字以内

### （4）输出格式固定（Format）

支持格式示例：

- Markdown
- JSON
- XML
- CSV
- Markdown Table

### （5）角色设定（Role）

例如：

- 建筑造价专家
- 资深产品经理
- 算法工程师
- Prompt 工程师
- 高校教师
- SCI 论文审稿人

### （6）任务拆解（Step-by-step）

```text
第一步：理解需求
第二步：分析问题
第三步：提出方案
第四步：输出最终结果
```

---

## 4. 常见 Prompt 技术

### Zero-shot Prompt

不给任何例子。

### One-shot Prompt

给一个例子。

### Few-shot Prompt

给多个示例，让模型学习输出格式。

### Chain of Thought（CoT）

让模型逐步分析，再给答案。

适用于：

- 数学
- 推理
- 规划
- Agent

### Self-Consistency

生成多个推理路径，再选择一致性最高的答案。

### Tree of Thoughts（ToT）

分别分析多个方案，再择优。

### ReAct

```text
Reason
↓
Action
↓
Observation
↓
Reason
```

### Plan and Execute

```text
Plan
↓
Task1
Task2
Task3
↓
Execute
```

---

## 5. Prompt 框架

### RTF

- Role
- Task
- Format

### CO-STAR

- Context
- Objective
- Style
- Tone
- Audience
- Response

### CRISPE

- Capacity
- Role
- Insight
- Statement
- Personality
- Experiment

---

## 6. Prompt 模板

### （1）代码生成

```text
你是一位高级软件工程师。

请完成以下需求：

【需求】
……

要求：
1. 使用 Python
2. 提供完整代码
3. 添加注释
4. 给出运行结果示例
```

### （2）代码 Review

重点关注：

- Bug
- 性能
- 安全
- 可维护性

### （3）知识问答

要求：

- 引用依据
- 给出原因
- 无依据请明确说明

### （4）文档生成

包括：

- 背景
- 需求
- 架构
- 数据库设计
- 接口设计
- 测试方案
- 风险分析

### （5）Agent Prompt

```text
角色：建筑造价专家

目标：完成智能组价

工作流程：
理解问题
↓
分析工程量
↓
检索规范
↓
检索定额
↓
判断适用条件
↓
输出组价结果

输出要求：
- 引用规范
- 引用定额
- 说明理由
- Markdown 输出
```

---

## 7. Agent Prompt 设计

通常包括：

- Role
- Goal
- Workflow
- Tool
- Memory
- Knowledge
- Constraint
- Output

---

## 8. Prompt 在 RAG 中的应用

```text
System Prompt
↓
Retriever
↓
Retrieved Context
↓
Question
↓
LLM
```

原则：

> 只能依据提供的知识回答；知识库没有则明确说明，禁止编造。

---

## 9. Prompt 在多 Agent 中的应用

```text
Planner Agent
↓
Task Agent
↓
Search Agent
↓
RAG Agent
↓
Code Agent
↓
Review Agent
↓
Summary Agent
```

---

## 10. Prompt 最佳实践

1. 使用结构化 Prompt。
2. 提供充分背景。
3. 明确角色、目标、约束和输出格式。
4. 复杂任务采用分步执行。
5. 使用 Few-shot 统一输出风格。
6. 企业场景结合 RAG、工具调用和工作流。
7. 增加结果校验，避免幻觉。
8. 持续迭代 Prompt，并结合实际数据评估。

---

## 11. 学习路线

### 第一阶段：基础 Prompt

- Prompt 基本语法
- Role Prompt
- Few-shot
- Chain of Thought

### 第二阶段：高级 Prompt

- ReAct
- Tree of Thoughts
- Self-Consistency
- Structured Output

### 第三阶段：Agent Prompt

- LangGraph
- Workflow
- MCP
- Tool Calling
- RAG

### 第四阶段：企业级 Prompt

- Multi-Agent
- Prompt Version Control
- Prompt Evaluation
- Prompt Optimization
- Prompt Security
- Prompt Testing

---

## 12. 总结

```text
Prompt
├── Workflow（工作流）
├── Tools（工具调用）
├── RAG（知识检索）
├── Memory（记忆）
├── Planning（规划）
└── Evaluation（评估）
```

Prompt 是智能体的大脑入口，而工作流、知识库、工具和记忆共同决定了 Agent 的整体能力。掌握这些模块的协同设计，是构建高质量 AI Agent 的关键。
