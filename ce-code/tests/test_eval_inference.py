"""阶段 5.1 推理编排的纯本地回归测试。

不启动模型、不依赖 openai，可直接运行：
    .venv/bin/python tests/test_eval_inference.py
"""

from __future__ import annotations

import json
import hashlib
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
    validate_fewshot_against_eval,
    validate_fewshot_rejections,
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


def _frozen_demo(
    index: int,
    *,
    source_dataset: str,
    sample_type: str,
    source_clauses: list[str] | None = None,
) -> dict:
    return {
        "sample_id": f"sample_{index}",
        "source_dataset": source_dataset,
        "source_file": f"data/processed/{source_dataset}/train.jsonl",
        "source_file_sha256": str(index) * 64,
        "sample_type": sample_type,
        "source_clauses": source_clauses or [],
        "selection_seed": 42,
        "candidate_count": 10,
        "selection_rejections_file": "configs/prompts/eval_fewshot_rejections.json",
        "selection_rejections_sha256": "a" * 64,
        "excluded_sample_ids": [],
        "user": f"u{index}",
        "assistant": f"a{index}",
    }


def _frozen_demos() -> list[dict]:
    return [
        _frozen_demo(
            1,
            source_dataset="group_c",
            sample_type="single_clause",
            source_clauses=["GB50010-2010_99.1.1"],
        ),
        _frozen_demo(
            2,
            source_dataset="group_d",
            sample_type="cross_clause",
            source_clauses=["GB50010-2010_99.2.1", "GB50010-2010_99.2.2"],
        ),
        _frozen_demo(
            3,
            source_dataset="group_d",
            sample_type="refusal",
            source_clauses=["GB50010-2010_99.3.1"],
        ),
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
        placeholders[0]["assistant"] = "【待确认】答案"
        path.write_text(json.dumps(placeholders, ensure_ascii=False), encoding="utf-8")
        try:
            load_fewshot(path)
        except ValueError as exc:
            assert "占位内容" in str(exc)
        else:
            raise AssertionError("待确认占位内容必须被拒绝")


def test_fewshot_provenance_is_loaded_and_checked_against_eval() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "fewshot.json"
        demos = _frozen_demos()
        path.write_text(json.dumps(demos, ensure_ascii=False), encoding="utf-8")
        loaded = load_fewshot(path)
        validate_fewshot_against_eval(loaded, _eval_rows())
        assert [item["sample_id"] for item in loaded] == [
            "sample_1",
            "sample_2",
            "sample_3",
        ]

        loaded[0]["source_clauses"] = ["GB50010-2010_1.0.1"]
        eval_rows = _eval_rows()
        eval_rows[0]["gold_clauses"] = ["GB50010-2010_1.0.1"]
        try:
            validate_fewshot_against_eval(loaded, eval_rows)
        except ValueError as exc:
            assert "金标条文重合" in str(exc)
        else:
            raise AssertionError("few-shot 与评测金标条文重合时必须停止")


def test_fewshot_rejections_must_match_current_config() -> None:
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        prompt_path = root / "fewshot.json"
        rejections_path = root / "rejections.json"
        rejections_path.write_text(
            json.dumps(
                {
                    "version": 1,
                    "rejections": [
                        {
                            "sample_id": "rejected_cross",
                            "source_dataset": "group_d",
                            "sample_type": "cross_clause",
                            "reason": "测试否决",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        rejection_hash = hashlib.sha256(rejections_path.read_bytes()).hexdigest()
        demos = _frozen_demos()
        for demo in demos:
            demo["selection_rejections_sha256"] = rejection_hash
        demos[1]["excluded_sample_ids"] = ["rejected_cross"]
        prompt_path.write_text(
            json.dumps(demos, ensure_ascii=False),
            encoding="utf-8",
        )

        loaded = load_fewshot(prompt_path)
        assert validate_fewshot_rejections(loaded, rejections_path) == rejection_hash

        loaded[1]["sample_id"] = "rejected_cross"
        try:
            validate_fewshot_rejections(loaded, rejections_path)
        except ValueError as exc:
            assert "本身已被人工否决" in str(exc)
        else:
            raise AssertionError("已被人工否决的示例必须停止评测")


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
