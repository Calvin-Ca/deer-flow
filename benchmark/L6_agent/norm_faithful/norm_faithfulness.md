# Judge：norm-qa 忠实度（faithfulness / 防幻觉）

> Langfuse LLM-as-a-judge evaluator 的评分细则。**这份文件是单一事实源**：要改判官口径，改这里、commit，再到 UI 里把 Prompt 框更新成最新版本（UI 不随 git，靠本文件留底）。

## 用途

评 norm（规范条文问答）类 trace：agent 调 `qa.py` 检索到一组条文（`cited_clauses`），再据此作答。本 judge 打**答案是否只用了检索到的条文、没有编造条文号/条文内容**——对标 `AGENT_BENCHMARK.md` 的 L6-C 忠实度、`LANGFUSE.md §7` TODO。

## 在 Langfuse UI 怎么配

Evaluators → New evaluator → Custom（LLM-as-judge）：
1. **Model**：选一个 model connection（见 `LANGFUSE.md` runbook「配 model connection」）——judge 模型建议比被测 agent 强一档。
2. **Prompt**：粘下方「判官 Prompt」。
3. **Variable mapping**（把 `{{...}}` 映射到 trace 字段）：
   - `{{question}}` → trace input（用户原问题）
   - `{{context}}` → 检索到的条文。trace 里 `qa.py` 输出的 `cited_clauses`（若在子 span，映射到该 span 的 output；拿不到就退而映射整条 trace output 里的条文段）
   - `{{answer}}` → trace output（agent 面向用户的最终回答）
4. **Score**：Numeric，名字 `norm_faithfulness`，值域 {0,1}。
5. **Target**：限定到 norm 类 run/trace（可按 tag 或 dataset run 过滤），别让它去评 cost trace。

## 判官 Prompt（粘进 UI）

```
你是规范条文问答的「忠实度判官」。给你三样东西：用户问题、系统检索到的条文（context）、以及 agent 的回答（answer）。

你只判一件事：**回答里的每一处条文号、条文内容、计量规则，是否都能在 context 里找到出处**。

打 0 的情形（任一命中即 0）：
- 回答给出了 context 里不存在的条文号（如编造「第 4.2.3 条」而 context 无此条）；
- 回答陈述的规则/数值与 context 矛盾，或 context 根本没提；
- context 为空（零召回）但回答仍给出了具体条文号或规则，而不是如实说「未检索到」。

打 1 的情形：
- 回答的所有条文号与规则都能在 context 里对上；
- 或 context 为空时，回答如实说明「未检索到相关条文」、没有编造。

注意：不评回答「全不全」或「好不好」，只评「有没有编造/越界」。措辞润色、合理归纳 context 内信息不算编造。

用户问题：
{{question}}

检索到的条文（context）：
{{context}}

agent 的回答：
{{answer}}

只输出一个 JSON：{"score": 0 或 1, "reasoning": "一句话说明判 0/1 的依据，若判 0 指出是哪处编造"}
```

## 打分口径

- `score=1` 忠实（无编造）；`score=0` 有幻觉/越界。
- 聚合看**忠实度 = 均值**（1 占比）。建议门：≥0.95（红线级，编造条文号是造价场景不可接受的错）。
- 与确定性的「召回/命中」指标互补：忠实度只管「说的有没有出处」，不管「该说的有没有说全」。
