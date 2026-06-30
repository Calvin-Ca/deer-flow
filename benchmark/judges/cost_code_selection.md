# Judge：cost 选码合理性

> Langfuse LLM-as-a-judge evaluator 的评分细则。**本文件是单一事实源**：改口径改这里、commit，再到 UI 更新 Prompt 版本。

## 用途

评 cost（算量组价）类 trace：agent 调 `cost.py`，从构件/做法描述选出 9 位清单编码并取定额工料机。本 judge 打**选出的编码与「构件描述 + 规范版本」是否匹配、有没有串库或张冠李戴**——对标 `AGENT_BENCHMARK.md` cost_task。

> 边界：judge 评的是**选码合理性**（语义对不对、版本对不对），**不验编码是否真实存在**于库中（那要查库，是确定性校验，不归 LLM-judge）。两者互补。

## 在 Langfuse UI 怎么配

Evaluators → New evaluator → Custom（LLM-as-judge）：
1. **Model**：选 model connection，judge 模型建议强一档。
2. **Prompt**：粘下方「判官 Prompt」。
3. **Variable mapping**：
   - `{{description}}` → trace input（构件/做法描述）
   - `{{spec}}` → 规范版本 2013/2024。来自调用参数 `--spec`（在 `cost.py` 子 span 的 input，或整条 trace input 的版本字段）
   - `{{result}}` → `cost.py` 输出：选中的 9 位编码 + 名称 +（如有）`need_review`/`price_status`。映射到该 span 的 output，拿不到就映射 trace output 里的组价结果段
4. **Score**：Numeric，名字 `cost_code_reasonable`，值域 {0,1}。
5. **Target**：限定到 cost 类 run/trace，别评 norm trace。

## 判官 Prompt（粘进 UI）

```
你是清单组价的「选码合理性判官」。给你三样：构件/做法描述、规范版本（spec：2013 或 2024）、以及 agent 选出的 9 位清单编码及取数结果（result）。

你判：**选出的编码，与构件描述在语义上是否对得上，且符合所声明的规范版本**。

打 0 的情形（任一命中即 0）：
- 编码对应的清单项与构件描述明显不是一类（如描述是「现浇矩形柱」却选了「砌体墙」类编码）；
- 版本串库迹象：result 自报或明显是另一版（2013↔2024）的编码体系，与 spec 不一致；
- 描述里明确的关键特征（混凝土强度等级、构件类型、部位）被无视，选了不匹配的项；
- 该标 need_review / 缺价却被当成定稿确定性给出（把不确定包装成确定也算不合理）。

打 1 的情形：
- 编码所属清单项与构件描述同类、关键特征对得上，且与 spec 版本自洽；
- 或 result 已如实标 `need_review=true` / `code=null` / 缺价透传——「如实说不确定」算合理，不扣分。

注意：不评价格算得对不对（agent 只到选码取数、不组装综合单价），只评「码选得合不合理 + 版本对不对 + 不确定有没有如实透传」。

构件/做法描述：
{{description}}

规范版本 spec：
{{spec}}

agent 选码与取数结果（result）：
{{result}}

只输出一个 JSON：{"score": 0 或 1, "reasoning": "一句话依据，判 0 时指出是串库/不同类/无视特征中的哪种"}
```

## 打分口径

- `score=1` 选码合理（含「如实透传不确定」）；`score=0` 串库/张冠李戴/把不确定当定稿。
- 聚合看**合理率 = 均值**。建议门：≥0.9。版本串库（2013↔2024）是造价红线，单独看 spec 不一致的占比更敏感。
