### A组数据：基于规则生成
### B组数据：基于LLM反向生成（qwen32b）
### C组数据：B组 + 三重过滤
### D组数据：C+难样本
数据量为自然生成，但要注意控制变量

B组合成数据      Qwen3-32B-AWQ（本地，零费用）      —— 生成任务，需要较强
B组过滤器①判官   Qwen3-8B（本地，零费用）           —— 判别式，8B 够用
D1：跨条文样本（基于 refs 单条校验）   同上          —— 判别式，8B
评测集出题    gen_evalset.py（锚定修好的条文库）。      用模型 Qwen-Max 
5.5 最终判分  GPT-4o 或 Qwen-Max                 —— 评价式，必须强于 7B

### 训练相关
per_device_train_batch_size: 1
gradient_accumulation_steps: 16
max_steps: 1500
每次算一条数据，计算梯度
将16次的梯度累加后，更新参数
一共更新1500次