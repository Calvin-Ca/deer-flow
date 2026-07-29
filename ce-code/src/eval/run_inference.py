"""阶段 5.1：通过 vLLM OpenAI-compatible API 批量生成六组评测答案。

设计目标：
  - 铁律 4 的四个推理参数写死并记录：temperature=0、top_p=1、
    seed=42、max_tokens=1024；
  - 支持按模型断点续跑，不重复写同一个评测 id；
  - API 失败单独留痕，失败样本不冒充已完成样本；
  - 每次启动核对评测集、模型映射与 few-shot prompt 指纹，防止混跑；
  - smoke 固定抽取每题型 4 题，共 20 题。

用法：
    python -m src.eval.run_inference --run-id eval_v1_20260730
    python -m src.eval.run_inference --run-id smoke_v1 --smoke
    python -m src.eval.run_inference --run-id eval_v1_20260730 --check-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

_ROOT = Path(__file__).resolve().parents[2]
_DEFAULT_EVAL_FILE = _ROOT / "data/eval/evalset_v1.jsonl"
_DEFAULT_FEWSHOT_FILE = _ROOT / "configs/prompts/eval_fewshot.json"
_DEFAULT_MODEL_PATH = Path("/mnt/nvme/calvin/models/Qwen2.5-7B-Instruct")
_DEFAULT_CHECKPOINT_ROOT = _ROOT / "checkpoints"
_RESULTS_ROOT = _ROOT / "results"

MODEL_IDS = ("base", "base_fewshot", "group_a", "group_b", "group_c", "group_d")
EVAL_TYPES = ("single_clause", "cross_clause", "calculation", "clause_verify", "refusal")
SYSTEM_PROMPT = "You are Qwen, created by Alibaba Cloud. You are a helpful assistant."

# CLAUDE.md §3 铁律 4。不要增加同名 CLI 参数，避免误操作改变实验口径。
TEMPERATURE = 0.0
TOP_P = 1.0
SEED = 42
MAX_TOKENS = 1024
SMOKE_PER_TYPE = 4
MANIFEST_VERSION = 1


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _load_jsonl(path: Path) -> list[dict[str, Any]]:
    """读取 JSONL，并拒绝空行、非法 JSON 和重复 id。"""
    if not path.is_file():
        raise FileNotFoundError(f"文件不存在：{path}")

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_no} 是空行")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} JSON 非法：{exc}") from exc
            sample_id = row.get("id")
            if not isinstance(sample_id, str) or not sample_id:
                raise ValueError(f"{path}:{line_no} 缺少非空字符串 id")
            if sample_id in seen:
                raise ValueError(f"{path}:{line_no} 出现重复 id：{sample_id}")
            seen.add(sample_id)
            rows.append(row)
    return rows


def _validate_evalset(rows: list[dict[str, Any]]) -> None:
    """检查阶段 5 推理所需的最小评测字段与题型。"""
    if not rows:
        raise ValueError("评测集为空")
    problems: list[str] = []
    counts = {kind: 0 for kind in EVAL_TYPES}
    for row in rows:
        sample_id = row["id"]
        kind = row.get("type")
        if kind not in counts:
            problems.append(f"{sample_id}: 未知 type={kind!r}")
        else:
            counts[kind] += 1
        if not isinstance(row.get("question"), str) or not row["question"].strip():
            problems.append(f"{sample_id}: question 为空")
    missing = [kind for kind, count in counts.items() if count == 0]
    if missing:
        problems.append(f"缺少题型：{', '.join(missing)}")
    if problems:
        preview = "\n".join(f"  - {problem}" for problem in problems[:20])
        raise ValueError(f"评测集校验失败：\n{preview}")


def select_eval_rows(rows: list[dict[str, Any]], smoke: bool) -> list[dict[str, Any]]:
    """全量时保持原顺序；smoke 时按原顺序从每题型取 4 题。"""
    if not smoke:
        return list(rows)
    selected: list[dict[str, Any]] = []
    counts = {kind: 0 for kind in EVAL_TYPES}
    for row in rows:
        kind = row["type"]
        if counts[kind] < SMOKE_PER_TYPE:
            selected.append(row)
            counts[kind] += 1
    short = {kind: count for kind, count in counts.items() if count < SMOKE_PER_TYPE}
    if short:
        raise ValueError(f"smoke 每题型需要 {SMOKE_PER_TYPE} 题，实际不足：{short}")
    # 按原评测文件顺序推理，避免题型重排成为额外变量。
    selected_ids = {row["id"] for row in selected}
    return [row for row in rows if row["id"] in selected_ids]


def load_fewshot(path: Path) -> list[dict[str, str]]:
    """加载恰好三条 user/assistant 示例；不允许从评测集临时取例子。"""
    if not path.is_file():
        raise FileNotFoundError(
            f"base_fewshot 需要已冻结的 3-shot 文件：{path}\n"
            "请先确认三条示例，并按 configs/prompts/eval_fewshot.example.json "
            "的格式保存。"
        )
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"3-shot 文件 JSON 非法：{path}: {exc}") from exc
    if not isinstance(value, list) or len(value) != 3:
        actual_type = type(value).__name__
        raise ValueError(
            f"3-shot 文件必须是恰好 3 个元素的数组，实际为 {actual_type}"
        )
    normalized: list[dict[str, str]] = []
    for index, item in enumerate(value, 1):
        if not isinstance(item, dict):
            raise ValueError(f"3-shot 第 {index} 项必须是对象")
        user = item.get("user")
        assistant = item.get("assistant")
        if not isinstance(user, str) or not user.strip():
            raise ValueError(f"3-shot 第 {index} 项 user 为空")
        if not isinstance(assistant, str) or not assistant.strip():
            raise ValueError(f"3-shot 第 {index} 项 assistant 为空")
        if "【待确认】" in user or "【待确认】" in assistant:
            raise ValueError(f"3-shot 第 {index} 项仍是待确认占位内容")
        normalized.append({"user": user.strip(), "assistant": assistant.strip()})
    return normalized


def build_messages(
    question: str,
    *,
    model_id: str,
    fewshot: list[dict[str, str]] | None,
) -> list[dict[str, str]]:
    """为六个模型构造消息；只有 base_fewshot 增加三条示例。"""
    messages = [{"role": "system", "content": SYSTEM_PROMPT}]
    if model_id == "base_fewshot":
        if fewshot is None or len(fewshot) != 3:
            raise ValueError("base_fewshot 必须提供恰好三条示例")
        for demo in fewshot:
            messages.append({"role": "user", "content": demo["user"]})
            messages.append({"role": "assistant", "content": demo["assistant"]})
    messages.append({"role": "user", "content": question})
    return messages


def parse_models(value: str) -> tuple[str, ...]:
    """解析逗号分隔的实验模型 ID，保持顺序并拒绝重复/未知值。"""
    models = tuple(item.strip() for item in value.split(",") if item.strip())
    if not models:
        raise ValueError("--models 不能为空")
    unknown = [item for item in models if item not in MODEL_IDS]
    if unknown:
        raise ValueError(f"未知模型 ID：{unknown}；允许值：{MODEL_IDS}")
    if len(set(models)) != len(models):
        raise ValueError(f"--models 中有重复项：{models}")
    return models


def model_mapping(base_model: str) -> dict[str, str]:
    """实验 ID → vLLM /v1/models 中的模型名。"""
    return {
        "base": base_model,
        "base_fewshot": base_model,
        "group_a": "group_a",
        "group_b": "group_b",
        "group_c": "group_c",
        "group_d": "group_d",
    }


def artifact_fingerprints(
    model_path: Path,
    checkpoint_root: Path,
    models: tuple[str, ...],
) -> dict[str, Any]:
    """记录基座配置和实际参与评测的 LoRA 权重指纹。"""
    base_files = ("config.json", "model.safetensors.index.json")
    base_hashes: dict[str, str] = {}
    for name in base_files:
        path = model_path / name
        if not path.is_file():
            raise FileNotFoundError(f"基座模型缺文件：{path}")
        base_hashes[name] = _sha256(path)

    adapters: dict[str, Any] = {}
    for model_id in models:
        if not model_id.startswith("group_"):
            continue
        adapter_dir = checkpoint_root / model_id
        files: dict[str, str] = {}
        for name in ("adapter_config.json", "adapter_model.safetensors"):
            path = adapter_dir / name
            if not path.is_file():
                raise FileNotFoundError(f"{model_id} 缺 adapter 文件：{path}")
            files[name] = _sha256(path)
        adapters[model_id] = {
            "path": str(adapter_dir.resolve()),
            "files": files,
        }
    return {
        "base": {"path": str(model_path.resolve()), "files": base_hashes},
        "adapters": adapters,
    }


def _read_completed(
    path: Path,
    expected_ids: set[str],
    expected_model_id: str | None = None,
) -> dict[str, dict[str, Any]]:
    """读取已有成功结果，重复 id 或不属于本轮的 id 均视为污染并停止。"""
    if not path.exists():
        return {}
    completed: dict[str, dict[str, Any]] = {}
    with path.open(encoding="utf-8") as file:
        for line_no, line in enumerate(file, 1):
            if not line.strip():
                raise ValueError(f"{path}:{line_no} 是空行")
            try:
                row = json.loads(line)
            except json.JSONDecodeError as exc:
                raise ValueError(f"{path}:{line_no} JSON 非法：{exc}") from exc
            sample_id = row.get("id")
            if sample_id not in expected_ids:
                raise ValueError(f"{path}:{line_no} id 不属于本轮评测：{sample_id!r}")
            if sample_id in completed:
                raise ValueError(f"{path}:{line_no} 重复 id：{sample_id}")
            if not isinstance(row.get("answer"), str):
                raise ValueError(f"{path}:{line_no} 缺少 answer 字符串")
            if expected_model_id is not None and row.get("model_id") != expected_model_id:
                raise ValueError(
                    f"{path}:{line_no} model_id={row.get('model_id')!r}，"
                    f"期望 {expected_model_id!r}"
                )
            completed[sample_id] = row
    return completed


def _write_jsonl(path: Path, row: dict[str, Any], lock: threading.Lock) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(row, ensure_ascii=False) + "\n"
    with lock:
        with path.open("a", encoding="utf-8") as file:
            file.write(line)
            file.flush()


def _request_one(
    client: Any,
    *,
    row: dict[str, Any],
    model_id: str,
    served_model: str,
    fewshot: list[dict[str, str]] | None,
) -> dict[str, Any]:
    """执行一次确定性 chat completion，并返回可直接落盘的原始记录。"""
    started = time.monotonic()
    response = client.chat.completions.create(
        model=served_model,
        messages=build_messages(row["question"], model_id=model_id, fewshot=fewshot),
        temperature=TEMPERATURE,
        top_p=TOP_P,
        seed=SEED,
        max_tokens=MAX_TOKENS,
    )
    choice = response.choices[0]
    usage = response.usage
    return {
        "id": row["id"],
        "type": row["type"],
        "model_id": model_id,
        "served_model": served_model,
        "question": row["question"],
        "answer": choice.message.content or "",
        "finish_reason": choice.finish_reason,
        "prompt_tokens": usage.prompt_tokens if usage else None,
        "completion_tokens": usage.completion_tokens if usage else None,
        "latency_seconds": round(time.monotonic() - started, 4),
        "generated_at": _utc_now(),
    }


def _failure_record(
    *,
    row: dict[str, Any],
    model_id: str,
    served_model: str,
    error: Exception,
) -> dict[str, Any]:
    return {
        "id": row["id"],
        "type": row["type"],
        "model_id": model_id,
        "served_model": served_model,
        "error_type": type(error).__name__,
        "error": str(error),
        "failed_at": _utc_now(),
    }


def _manifest_contract(
    *,
    eval_file: Path,
    eval_sha256: str,
    fewshot_file: Path | None,
    fewshot_sha256: str | None,
    selected_rows: list[dict[str, Any]],
    smoke: bool,
    models: tuple[str, ...],
    mapping: dict[str, str],
    base_url: str,
    artifacts: dict[str, Any],
) -> dict[str, Any]:
    return {
        "manifest_version": MANIFEST_VERSION,
        "eval_file": str(eval_file.resolve()),
        "eval_sha256": eval_sha256,
        "fewshot_file": str(fewshot_file.resolve()) if fewshot_file else None,
        "fewshot_sha256": fewshot_sha256,
        "smoke": smoke,
        "selected_count": len(selected_rows),
        "selected_ids": [row["id"] for row in selected_rows],
        "models": list(models),
        "model_mapping": {model: mapping[model] for model in models},
        "base_url": base_url.rstrip("/"),
        "artifact_fingerprints": artifacts,
        "system_prompt": SYSTEM_PROMPT,
        "generation": {
            "temperature": TEMPERATURE,
            "top_p": TOP_P,
            "seed": SEED,
            "max_tokens": MAX_TOKENS,
        },
    }


def _ensure_manifest(path: Path, contract: dict[str, Any]) -> dict[str, Any]:
    """首次创建运行清单；续跑时要求所有影响结果的字段完全相同。"""
    if path.exists():
        current = json.loads(path.read_text(encoding="utf-8"))
        mismatches = {
            key: (current.get(key), value)
            for key, value in contract.items()
            if current.get(key) != value
        }
        if mismatches:
            details = "\n".join(
                f"  - {key}: 已有={old!r}，本次={new!r}"
                for key, (old, new) in mismatches.items()
            )
            raise ValueError(f"run_id 已存在但运行口径不同，禁止混跑：\n{details}")
        return current

    path.parent.mkdir(parents=True, exist_ok=True)
    manifest = {**contract, "created_at": _utc_now(), "status": "running", "counts": {}}
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return manifest


def _update_manifest(
    path: Path,
    manifest: dict[str, Any],
    counts: dict[str, dict[str, int]],
    *,
    complete: bool,
) -> None:
    manifest = {
        **manifest,
        "status": "complete" if complete else "incomplete",
        "updated_at": _utc_now(),
        "counts": counts,
    }
    path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _make_client(base_url: str, timeout: float) -> Any:
    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("缺少 openai 包，请在项目环境执行：uv sync") from exc
    return OpenAI(
        api_key=os.getenv("OPENAI_API_KEY", "EMPTY"),
        base_url=base_url,
        timeout=timeout,
        max_retries=2,
    )


def _check_served_models(client: Any, required: Iterable[str]) -> None:
    try:
        available = {item.id for item in client.models.list().data}
    except Exception as exc:
        raise RuntimeError(f"无法访问 vLLM /v1/models：{type(exc).__name__}: {exc}") from exc
    missing = sorted(set(required) - available)
    if missing:
        raise RuntimeError(
            f"vLLM 未注册所需模型：{missing}；当前 /v1/models={sorted(available)}"
        )


def _run_model(
    client: Any,
    *,
    rows: list[dict[str, Any]],
    model_id: str,
    served_model: str,
    fewshot: list[dict[str, str]] | None,
    raw_path: Path,
    failed_path: Path,
    workers: int,
) -> tuple[int, int]:
    expected_ids = {row["id"] for row in rows}
    completed = _read_completed(raw_path, expected_ids, model_id)
    pending = [row for row in rows if row["id"] not in completed]
    print(
        f"\n[{model_id}] 目标 {len(rows)}，已完成 {len(completed)}，"
        f"待生成 {len(pending)}，served_model={served_model}"
    )
    if not pending:
        return len(completed), 0

    output_lock = threading.Lock()
    failed = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _request_one,
                client,
                row=row,
                model_id=model_id,
                served_model=served_model,
                fewshot=fewshot,
            ): row
            for row in pending
        }
        done_now = 0
        for future in as_completed(futures):
            row = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # 失败留痕，其他题继续
                failed += 1
                _write_jsonl(
                    failed_path,
                    _failure_record(
                        row=row,
                        model_id=model_id,
                        served_model=served_model,
                        error=exc,
                    ),
                    output_lock,
                )
                print(f"[{model_id}] ❌ {row['id']}: {type(exc).__name__}: {exc}")
                continue
            _write_jsonl(raw_path, result, output_lock)
            done_now += 1
            if done_now % 10 == 0 or done_now == len(pending):
                print(f"[{model_id}] 本轮 {done_now}/{len(pending)}")

    total = len(_read_completed(raw_path, expected_ids, model_id))
    return total, failed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="阶段 5.1：六模型 vLLM 批量推理")
    parser.add_argument("--run-id", required=True, help="结果目录名，不允许包含 /")
    parser.add_argument("--base-url", default="http://127.0.0.1:8002/v1")
    parser.add_argument("--base-model", default="base", help="vLLM 注册的基座模型名")
    parser.add_argument(
        "--models",
        default=",".join(MODEL_IDS),
        help="逗号分隔的实验模型 ID；默认六个全部运行",
    )
    parser.add_argument("--eval-file", type=Path, default=_DEFAULT_EVAL_FILE)
    parser.add_argument("--fewshot-file", type=Path, default=_DEFAULT_FEWSHOT_FILE)
    parser.add_argument("--model-path", type=Path, default=_DEFAULT_MODEL_PATH)
    parser.add_argument("--checkpoint-root", type=Path, default=_DEFAULT_CHECKPOINT_ROOT)
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--request-timeout", type=float, default=180.0)
    parser.add_argument("--smoke", action="store_true", help="每题型 4 题，共 20 题")
    parser.add_argument("--check-only", action="store_true", help="只检查已有结果完整性")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if not args.run_id or "/" in args.run_id or args.run_id in {".", ".."}:
        print("❌ --run-id 必须是非空的单个目录名，不能包含 /", file=sys.stderr)
        return 2
    if args.workers < 1:
        print("❌ --workers 必须 >= 1", file=sys.stderr)
        return 2

    try:
        models = parse_models(args.models)
        eval_file = args.eval_file.resolve()
        rows = _load_jsonl(eval_file)
        _validate_evalset(rows)
        selected_rows = select_eval_rows(rows, args.smoke)

        fewshot: list[dict[str, str]] | None = None
        fewshot_file: Path | None = None
        fewshot_sha256: str | None = None
        if "base_fewshot" in models:
            fewshot_file = args.fewshot_file.resolve()
            fewshot = load_fewshot(fewshot_file)
            fewshot_sha256 = _sha256(fewshot_file)

        mapping = model_mapping(args.base_model)
        run_dir = _RESULTS_ROOT / args.run_id
        manifest_path = run_dir / "manifest.json"
        if args.check_only and not manifest_path.is_file():
            raise FileNotFoundError(f"check-only 找不到已有运行清单：{manifest_path}")
        artifacts = artifact_fingerprints(
            args.model_path.resolve(),
            args.checkpoint_root.resolve(),
            models,
        )
        contract = _manifest_contract(
            eval_file=eval_file,
            eval_sha256=_sha256(eval_file),
            fewshot_file=fewshot_file,
            fewshot_sha256=fewshot_sha256,
            selected_rows=selected_rows,
            smoke=args.smoke,
            models=models,
            mapping=mapping,
            base_url=args.base_url,
            artifacts=artifacts,
        )
        manifest = _ensure_manifest(manifest_path, contract)

        expected_ids = {row["id"] for row in selected_rows}
        counts: dict[str, dict[str, int]] = {}
        if not args.check_only:
            client = _make_client(args.base_url, args.request_timeout)
            _check_served_models(client, {mapping[model] for model in models})
            for model_id in models:
                complete_count, failed_count = _run_model(
                    client,
                    rows=selected_rows,
                    model_id=model_id,
                    served_model=mapping[model_id],
                    fewshot=fewshot,
                    raw_path=run_dir / "raw" / f"{model_id}.jsonl",
                    failed_path=run_dir / "failed" / f"{model_id}.jsonl",
                    workers=args.workers,
                )
                counts[model_id] = {
                    "expected": len(selected_rows),
                    "completed": complete_count,
                    "failed_this_run": failed_count,
                }

        # check-only 和正常运行最后都重新从文件计数，避免信任内存状态。
        complete = True
        for model_id in models:
            result = _read_completed(
                run_dir / "raw" / f"{model_id}.jsonl",
                expected_ids,
                model_id,
            )
            current = counts.setdefault(model_id, {})
            current["expected"] = len(selected_rows)
            current["completed"] = len(result)
            current.setdefault("failed_this_run", 0)
            current["missing"] = len(selected_rows) - len(result)
            if len(result) != len(selected_rows):
                complete = False
        _update_manifest(manifest_path, manifest, counts, complete=complete)

        print("\n完整性检查：")
        for model_id in models:
            count = counts[model_id]
            mark = "✅" if count["missing"] == 0 else "❌"
            print(
                f"  {mark} {model_id:<12} "
                f"{count['completed']}/{count['expected']}，缺 {count['missing']}"
            )
        print(f"\n运行清单：{manifest_path}")
        if not complete:
            print(
                "❌ 结果不完整；修复服务后用相同命令断点续跑。",
                file=sys.stderr,
            )
            return 1
        print(f"✅ 全部完成：{len(models)} × {len(selected_rows)}")
        return 0
    except (FileNotFoundError, ValueError, RuntimeError) as exc:
        print(f"❌ {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
