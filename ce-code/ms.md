### 什么是 OpenAI 接口协议？有哪些常用的接口？
OpenAI的云端服务接口，后续很多服务vLLM、ollama等都选择兼容它

Python SDK	HTTP 接口
client.responses.create()	POST /v1/responses
client.chat.completions.create()	POST /v1/chat/completions
client.completions.create()	POST /v1/completions
client.embeddings.create()	POST /v1/embeddings
client.models.list()	GET /v1/models

POST /v1/chat/completions
入参/出参介绍

请求示例

### 什么是vLLM，如何使用？有哪些常用的功能？
1. PagedAttention
   大模型生成文本时，需要保存前面 Token 的 Key、Value，这些数据称为 KV Cache。
   普通推理框架可能为每个请求预留一块连续显存。例如一个请求最多支持 32K Token，即使实际上只使用了 2K Token，也可能造成显存浪费。
   vLLM 的 PagedAttention 会把 KV Cache 划分成很多小块，类似操作系统的分页内存：
   传统方式：
   请求 A：[                 一整块连续显存                  ]
   请求 B：[                 一整块连续显存                  ]
   vLLM：
   请求 A：[块1][块5][块8]
   请求 B：[块2][块3]
   空闲块：[块4][块6][块7]
   这样可以减少显存碎片，提高 KV Cache 的实际利用率，从而在同一张 GPU 上处理更多请求。
2. Continuous Batching
   传统静态批处理通常要等一批请求全部完成，才能处理下一批。
   假设：
   请求 A：生成 10 个 Token
   请求 B：生成 100 个 Token
   请求 C：生成 30 个 Token
   静态批处理可能需要等请求 B 完成后，整个批次才结束。
   vLLM 使用连续批处理：
   请求 A 完成 → 立即移出
   新的请求 D → 立即加入当前批次
   因此不会因为某个长请求，让其他请求一直等待。连续批处理、分块预填充和前缀缓存都是 vLLM 提升并发吞吐的重要机制。
3. 如何使用？
   模型权重占固定显存，KV Cache 占动态显存。上下文越长、并发越高，需要的 KV Cache 越多。
- 确认模型权重
    模型加载阶段 OOM，首先处理的是模型权重。模型权重装不下时的处理顺序：
    1. 使用更小模型
    2. 使用 AWQ、GPTQ、FP8 等量化权重
    3. 使用多 GPU Tensor Parallel
    4. 必要时使用 CPU Offload
- 确定 vLLM 的显存预算
    决定当前 vLLM 实例可以使用多少比例的 GPU 显存，包括模型权重，KV Cache，CUDA Graph，激活值，框架运行开销。
    gpu-memory-utilization = 0.90，大致给 vLLM 规划：40GB × 90% ≈ 36G
- 确定真实的上下文需求
    为什么先调上下文再调整并发?因为上下文长度是业务需求,例如：
    System Prompt：2000 Token
    工具 Schema：1500 Token
    历史对话：2000 Token
    RAG 文档：4000 Token
    用户问题：500 Token
    预留输出：2000 Token
    合计约：12000 Token
    可以设置：--max-model-len 16384，没有必要因为模型支持 128K，就直接设置--max-model-len 131072。
    上下文上限设置过大，会让 vLLM 为超长请求保留能力，降低同等 KV Cache 容量下能够支持的并发。
    简单聊天	4K～8K
    Agent 工具调用	8K～16K
    普通 RAG	8K～16K
    长文档问答	32K
    超长文档	64K 以上
    Embedding 切片	1K～8K
- 用剩余 KV Cache 决定并发
  主要参数：--max-num-seqs 它表示一个调度周期最多同时处理多少条活跃序列。
  这里需要区分：外部并发请求数≠max-num-seqs假设同时来了 50 个请求，而：
  --max-num-seqs 8，vLLM 可以让部分请求先运行，其他请求排队，并不是只能接收 8 个 HTTP 请求
  下文和并发如何取舍？长上下文、低并发，中等上下文、中等并发，短上下文、高并发
- 最后调 Token 调度预算
  主要参数：--max-num-batched-tokens 4096它控制每次调度迭代最多处理多少个 Token。
  它与 max-num-seqs 的区别是：
  max-num-seqs → 一次最多调度多少条序列； max-num-batched-tokens → 一次最多调度多少个 Token
  例如：--max-num-seqs 8；--max-num-batched-tokens 4096
  表示：最多同时调度 8 条序列；这一轮所有序列合计最多处理 4096 个 Token。

  第六步：再考虑 KV Cache 优化
  当上下文和并发已经无法再降低，但仍然缺 KV Cache 时，再考虑高级优化。
  1. 使用 FP8 KV Cache
  --kv-cache-dtype fp8，方向是：每个 Token 的 KV Cache 占用降低
  → 同样显存可以容纳更多 Token
  → 可以增加上下文或并发
  但需要：GPU 和 vLLM 内核支持；验证模型输出质量；做性能压测。
  第一次部署建议先使用：--kv-cache-dtype auto 稳定后再测试 FP8。
  2. 增加 GPU 数量
  例如：--tensor-parallel-size 2
  模型权重拆到两张 GPU 后，每张 GPU 的权重占用减少，从而可以为 KV Cache 留出更多空间。
  不过 Tensor Parallel 会增加 GPU 间通信，因此不是 GPU 越多越快。官方也指出，提高 TP 可以释放每张 GPU 上的权重显存，但可能带来同步开销。
  3. 开启前缀缓存
  --enable-prefix-caching
  它不会直接让 KV Cache 容量无限增加，而是复用相同前缀的计算结果。
  适合：
  相同 System Prompt；
  固定工具定义；
  相同长文档；
  多轮对话；
  相同业务规则。
  你的 Agent 请求中工具 Schema 和 System Prompt 比较固定，适合开启。

每个基础模型创建一个独立 vLLM 容器，本质上就是：
使用同一个 vllm/vllm-openai 镜像启动多次，但每次指定不同的容器名、GPU、模型路径和宿主机端口。
例如你的 4 张 4090 可以这样分：
GPU 0、1 → Qwen3-32B-AWQ → 容器 main-llm → 端口 8001
GPU 2   → Qwen3-8B-AWQ  → 容器 small-llm → 端口 8002
GPU 3   → BGE-M3        → 容器 embedding → 端口 8003