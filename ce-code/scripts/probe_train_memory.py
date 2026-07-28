"""实测训练各环节的显存占用，定位 OOM 的真正来源。

动因——OOM 后对显存去向的分析容易停留在估算（"logits 大概占 7GB"），
而估算可能错得离谱：本项目已有先例，一份看似精确的诊断把排查引向了
"调大 max_tokens"这个完全错误的方向（见 PROGRESS 贯穿性教训）。
故实测。

两个模式：

  --stage logits   只测 lm_head 投影 + 交叉熵这一段（默认）
                   不加载 7B 权重，几秒出结果。用随机张量模拟最后一层 hidden，
                   走真实的 F.linear + F.cross_entropy，量的是真实算子行为。

  --stage full     加载真实模型走一次前向反向，逐阶段打印显存。
                   需要一张空卡与约 20GB 显存；OOM 时打印它卡在哪一步——
                   卡在哪一步本身就是答案。

用法（在服务器上，卡要空闲）：
    python scripts/probe_train_memory.py
    python scripts/probe_train_memory.py --batch 1
    python scripts/probe_train_memory.py --stage full --gpu 2
"""
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).parents[1]
GB = 1024 ** 3


def _mem() -> tuple[float, float]:
    """返回 (当前已分配 GB, 历史峰值 GB)。

    Args:
        无

    Returns:
        (allocated, max_allocated)，单位 GB
    """
    import torch
    return (torch.cuda.memory_allocated() / GB,
            torch.cuda.max_memory_allocated() / GB)


def _mark(tag: str, base: float = 0.0) -> float:
    """打印一个显存检查点。

    Args:
        tag:  阶段名
        base: 基准值，用于算增量

    Returns:
        当前已分配显存（GB）
    """
    cur, peak = _mem()
    delta = f"  (+{cur - base:.2f})" if base else ""
    print(f"  {tag:<38}{cur:>7.2f} GB{delta:<10}峰值 {peak:>6.2f} GB")
    return cur


def probe_logits(batch: int, seq: int, hidden: int, vocab: int) -> None:
    """只测 lm_head 投影 + 交叉熵这一段的显存。

    不加载 7B 权重：这一段的显存只取决于 [batch, seq, vocab] 的规模，
    与模型其余部分无关，故可以隔离测量。用随机张量模拟最后一层 hidden，
    但走的是真实的 F.linear 与 F.cross_entropy，量的是真实算子行为。

    Args:
        batch:  批大小
        seq:    序列长度
        hidden: 隐层维度
        vocab:  词表大小

    Returns:
        None（结果打印到标准输出）
    """
    import torch
    import torch.nn.functional as F

    torch.cuda.reset_peak_memory_stats()
    print(f"\n【只测 lm_head + 交叉熵】batch={batch} seq={seq} "
          f"hidden={hidden} vocab={vocab}")
    print(f"  logits 张量 [{batch}, {seq}, {vocab}] = "
          f"{batch*seq*vocab:,} 元素 = {batch*seq*vocab*2/GB:.2f} GB (bf16)\n")

    dev = "cuda"
    base = _mark("起点")

    # 最后一层 hidden：真实训练里由 28 层 transformer 产出，此处用随机张量代替
    h = torch.randn(batch, seq, hidden, device=dev, dtype=torch.bfloat16,
                    requires_grad=True)
    _mark("hidden state [B,S,H]", base)

    w = torch.randn(vocab, hidden, device=dev, dtype=torch.bfloat16,
                    requires_grad=True)
    _mark("lm_head 权重 [V,H]", base)

    logits = F.linear(h, w)
    _mark("→ logits [B,S,V]", base)

    labels = torch.randint(0, vocab, (batch, seq), device=dev)
    loss = F.cross_entropy(logits.float().view(-1, vocab), labels.view(-1))
    _mark("→ 交叉熵（logits 转 fp32）", base)

    loss.backward()
    _mark("→ 反向传播后", base)

    _, peak = _mem()
    print(f"\n  这一段的显存峰值：{peak:.2f} GB")
    print(f"  作为对比，单层 hidden state 只有 "
          f"{batch*seq*hidden*2/GB:.3f} GB —— 相差 {peak/(batch*seq*hidden*2/GB):.0f} 倍")


def probe_full(cfg_path: Path, batch: int) -> None:
    """加载真实模型走一次前向反向，逐阶段打印显存。

    OOM 时不捕获异常——卡在哪一步本身就是答案，让 traceback 如实抛出。

    Args:
        cfg_path: 训练 yaml，从中读 model_name_or_path / cutoff_len
        batch:    批大小

    Returns:
        None
    """
    import torch
    import yaml
    from transformers import AutoModelForCausalLM, AutoTokenizer

    conf = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    model_path = conf["model_name_or_path"]
    seq = conf.get("cutoff_len", 2048)

    torch.cuda.reset_peak_memory_stats()
    print(f"\n【加载真实模型】{model_path}")
    print(f"  batch={batch}  seq={seq}\n")

    base = _mark("起点")
    tok = AutoTokenizer.from_pretrained(model_path)
    model = AutoModelForCausalLM.from_pretrained(
        model_path, torch_dtype=torch.bfloat16, device_map={"": 0})
    _mark("加载权重后", base)

    model.gradient_checkpointing_enable()
    model.train()
    _mark("开 gradient_checkpointing", base)

    ids = torch.randint(0, tok.vocab_size, (batch, seq), device="cuda")
    out = model(input_ids=ids, labels=ids)
    _mark("→ 前向（含 logits 与 loss）", base)
    print(f"     其中 logits 张量 {tuple(out.logits.shape)} = "
          f"{out.logits.numel() * out.logits.element_size() / GB:.2f} GB")

    out.loss.backward()
    _mark("→ 反向传播后", base)

    _, peak = _mem()
    print(f"\n  全程峰值：{peak:.2f} GB / 卡容量 "
          f"{torch.cuda.get_device_properties(0).total_memory / GB:.2f} GB")


def main() -> int:
    """入口。

    Args:
        无（命令行读 --stage / --batch / --gpu / --config）

    Returns:
        退出码
    """
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["logits", "full"], default="logits")
    ap.add_argument("--batch", type=int, default=2)
    ap.add_argument("--seq", type=int, default=2048)
    ap.add_argument("--gpu", default="0", help="用哪张卡（须空闲）")
    ap.add_argument("--config", default="configs/group_a.yaml")
    args = ap.parse_args()

    os.environ["CUDA_VISIBLE_DEVICES"] = args.gpu
    import torch
    if not torch.cuda.is_available():
        print("没有可用的 CUDA 设备")
        return 1
    print(f"卡：{torch.cuda.get_device_name(0)}  "
          f"{torch.cuda.get_device_properties(0).total_memory / GB:.2f} GB")

    if args.stage == "logits":
        # Qwen2.5-7B 的实际结构参数，从模型 config 读而非写死
        cfg = _ROOT / args.config
        hidden, vocab = 3584, 152064
        try:
            import yaml
            from transformers import AutoConfig
            mc = AutoConfig.from_pretrained(
                yaml.safe_load(cfg.read_text(encoding="utf-8"))["model_name_or_path"])
            hidden, vocab = mc.hidden_size, mc.vocab_size
            print(f"（结构参数读自模型 config：hidden={hidden} vocab={vocab}）")
        except Exception as exc:
            print(f"（读模型 config 失败，用默认值 hidden={hidden} vocab={vocab}：{exc}）")
        probe_logits(args.batch, args.seq, hidden, vocab)
    else:
        probe_full(_ROOT / args.config, args.batch)
    return 0


if __name__ == "__main__":
    sys.exit(main())
