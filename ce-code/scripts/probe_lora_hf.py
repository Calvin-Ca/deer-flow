#!/usr/bin/env python
"""用 Transformers + PEFT 原生路径复核单个 LoRA adapter。

该脚本只用于诊断 vLLM 与原生推理是否一致，不写入正式评测结果。

示例（物理 GPU 2 会在进程内映射为 cuda:0）：
    CUDA_VISIBLE_DEVICES=2 .venv/bin/python scripts/probe_lora_hf.py --group a
"""

from __future__ import annotations

import argparse
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_MODEL_PATH = Path("/mnt/nvme/calvin/models/Qwen2.5-7B-Instruct")
_DEFAULT_QUESTION = "请说明GB50010-2010第9.5.4条的规定。"
_SYSTEM_PROMPT = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用 Transformers/PEFT 原生推理复核 LoRA 输出"
    )
    parser.add_argument("--group", choices=("a", "b", "c", "d"), required=True)
    parser.add_argument("--question", default=_DEFAULT_QUESTION)
    parser.add_argument("--model-path", type=Path, default=_MODEL_PATH)
    parser.add_argument("--checkpoint-root", type=Path, default=_ROOT / "checkpoints")
    parser.add_argument("--device", default="cuda:0")
    parser.add_argument("--max-new-tokens", type=int, default=256)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    import torch
    from peft import PeftModel
    from transformers import AutoModelForCausalLM, AutoTokenizer

    model_path = args.model_path.resolve()
    adapter_path = (args.checkpoint_root / f"group_{args.group}").resolve()
    for required in (
        model_path / "config.json",
        adapter_path / "adapter_config.json",
        adapter_path / "adapter_model.safetensors",
    ):
        if not required.is_file():
            raise FileNotFoundError(f"缺少文件：{required}")
    if not torch.cuda.is_available():
        raise RuntimeError("CUDA 不可用；7B 原生复核需要 GPU")

    print(f"device  : {args.device} ({torch.cuda.get_device_name(args.device)})")
    print(f"model   : {model_path}")
    print(f"adapter : {adapter_path}")
    print(f"question: {args.question}")

    tokenizer = AutoTokenizer.from_pretrained(
        model_path,
        local_files_only=True,
    )
    base_model = AutoModelForCausalLM.from_pretrained(
        model_path,
        dtype=torch.bfloat16,
        device_map={"": args.device},
        local_files_only=True,
        low_cpu_mem_usage=True,
    )
    model = PeftModel.from_pretrained(
        base_model,
        adapter_path,
        is_trainable=False,
    )
    model.eval()

    messages = [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": args.question},
    ]
    input_ids = tokenizer.apply_chat_template(
        messages,
        add_generation_prompt=True,
        return_tensors="pt",
    ).to(args.device)
    pad_token_id = tokenizer.pad_token_id
    if pad_token_id is None:
        pad_token_id = tokenizer.eos_token_id

    with torch.inference_mode():
        output_ids = model.generate(
            input_ids=input_ids,
            do_sample=False,
            max_new_tokens=args.max_new_tokens,
            pad_token_id=pad_token_id,
            use_cache=True,
        )
    answer = tokenizer.decode(
        output_ids[0, input_ids.shape[-1] :],
        skip_special_tokens=True,
    )
    print("\n===== answer =====")
    print(answer)
    print("==================")
    print(f"generated_tokens: {output_ids.shape[-1] - input_ids.shape[-1]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
