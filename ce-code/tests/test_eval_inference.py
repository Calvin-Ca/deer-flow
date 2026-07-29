"""阶段 5.1 推理编排的纯本地回归测试。

不启动模型、不依赖 openai，可直接运行：
    .venv/bin/python tests/test_eval_inference.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from src.eval.run_inference import (  # noqa: E402
    EVAL_TYPES,
    MAX_TOKENS,
    MODEL_IDS,
    SEED,
    SMOKE_PER_TYPE,
    TEMPERATURE,
    TOP_P,
    _ensure_manifest,
    _load_jsonl,
    _read_completed,
    artifact_fingerprints,
    build_messages,
    load_fewshot,
    parse_models,
    select_eval_rows,
)


def _eval_rows(per_type: int = 5) -> list[dict]:
    return [
        {
            "id": f"{kind}_{index}",
            "type": kind,
            "question": f"{kind} 问题 {index}",
        }
        for index in range(per_type)
        for kind in EVAL_TYPES
    ]


def test_smoke_selects_four_per_type_in_original_order() -> None:
    rows = _eval_rows()
    selected = select_eval_rows(rows, smoke=True)
    assert len(selected) == len(EVAL_TYPES) * SMOKE_PER_TYPE == 20
    assert [row["id"] for row in selected] == [row["id"] for row in rows[:20]]
    for kind in EVAL_TYPES:
        assert sum(row["type"] == kind for row in selected) == SMOKE_PER_TYPE


def test_all_generation_parameters_are_frozen() -> None:
    assert TEMPERATURE == 0.0
    assert TOP_P == 1.0
    assert SEED == 42
    assert MAX_TOKENS == 1024


def test_base_fewshot_adds_exactly_three_turns() -> None:
    demos = [
        {"user": f"u{index}", "assistant": f"a{index}"}
        for index in range(3)
    ]
    messages = build_messages("target", model_id="base_fewshot", fewshot=demos)
    assert [message["role"] for message in messages] == [
        "system",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
        "assistant",
        "user",
    ]
    assert messages[-1]["content"] == "target"

    plain = build_messages("target", model_id="group_d", fewshot=demos)
    assert [message["role"] for message in plain] == ["system", "user"]


def test_fewshot_rejects_placeholder_or_wrong_count() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fewshot.json"
        path.write_text(json.dumps([{"user": "u", "assistant": "a"}]), encoding="utf-8")
        try:
            load_fewshot(path)
        except ValueError as exc:
            assert "恰好 3" in str(exc)
        else:
            raise AssertionError("不是三条的 few-shot 文件必须被拒绝")

        placeholders = [
            {"user": f"u{index}", "assistant": f"a{index}"}
            for index in range(3)
        ]
        placeholders[1]["assistant"] = "【待确认】答案"
        path.write_text(json.dumps(placeholders, ensure_ascii=False), encoding="utf-8")
        try:
            load_fewshot(path)
        except ValueError as exc:
            assert "占位内容" in str(exc)
        else:
            raise AssertionError("待确认占位内容必须被拒绝")


def test_jsonl_and_resume_reject_duplicate_ids() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "rows.jsonl"
        line = json.dumps({"id": "same", "answer": "ok"}, ensure_ascii=False)
        path.write_text(line + "\n" + line + "\n", encoding="utf-8")
        try:
            _load_jsonl(path)
        except ValueError as exc:
            assert "重复 id" in str(exc)
        else:
            raise AssertionError("输入 JSONL 重复 id 必须被拒绝")

        try:
            _read_completed(path, {"same"})
        except ValueError as exc:
            assert "重复 id" in str(exc)
        else:
            raise AssertionError("结果 JSONL 重复 id 必须被拒绝")


def test_manifest_refuses_mixed_contract() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "manifest.json"
        original = {"eval_sha256": "aaa", "models": list(MODEL_IDS)}
        _ensure_manifest(path, original)
        try:
            _ensure_manifest(path, {"eval_sha256": "bbb", "models": list(MODEL_IDS)})
        except ValueError as exc:
            assert "禁止混跑" in str(exc)
        else:
            raise AssertionError("同 run_id 改评测集后必须停止")


def test_artifact_fingerprints_cover_adapter_weights() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        model = root / "model"
        adapter = root / "checkpoints/group_a"
        model.mkdir()
        adapter.mkdir(parents=True)
        (model / "config.json").write_text("config-v1", encoding="utf-8")
        (model / "model.safetensors.index.json").write_text("index-v1", encoding="utf-8")
        (adapter / "adapter_config.json").write_text("adapter-config-v1", encoding="utf-8")
        weights = adapter / "adapter_model.safetensors"
        weights.write_bytes(b"weights-v1")

        before = artifact_fingerprints(model, root / "checkpoints", ("base", "group_a"))
        weights.write_bytes(b"weights-v2")
        after = artifact_fingerprints(model, root / "checkpoints", ("base", "group_a"))

        assert before["base"]["files"] == after["base"]["files"]
        assert (
            before["adapters"]["group_a"]["files"]["adapter_model.safetensors"]
            != after["adapters"]["group_a"]["files"]["adapter_model.safetensors"]
        )


def test_parse_models_rejects_unknown_and_duplicate() -> None:
    assert parse_models("base,group_a") == ("base", "group_a")
    for value in ("base,base", "base,unknown"):
        try:
            parse_models(value)
        except ValueError:
            pass
        else:
            raise AssertionError(f"非法模型列表必须被拒绝：{value}")


if __name__ == "__main__":
    tests = [
        value
        for name, value in sorted(globals().items())
        if name.startswith("test_") and callable(value)
    ]
    for test in tests:
        test()
        print(f"ok {test.__name__}")
    print(f"全部 {len(tests)} 例通过")
