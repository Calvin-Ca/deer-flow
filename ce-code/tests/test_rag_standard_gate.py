"""Agent 面规范口径闸 + standard 推断的纯函数单测（不碰索引/Milvus，本地可直跑）。

兼容 pytest 与 __main__ 直跑（本地无 pytest 时 `python tests/test_rag_standard_gate.py`）。
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import config  # noqa: E402


def test_default_allowlist_is_2013_only():
    os.environ.pop("CE_RAG_AGENT_STANDARDS", None)
    allowed = config.agent_allowed_standards()
    assert allowed == {"gb50500-2013", "gb50854-2013"}


def test_ensure_agent_standard_passes_2013():
    os.environ.pop("CE_RAG_AGENT_STANDARDS", None)
    config.ensure_agent_standard("gb50854-2013")
    config.ensure_agent_standard("GB50500-2013")  # 大小写不敏感


def test_ensure_agent_standard_rejects_2024_and_install():
    os.environ.pop("CE_RAG_AGENT_STANDARDS", None)
    for std in ("gb50854-2024", "gb50500-2024", "gb50856-2024", "gb50016"):
        try:
            config.ensure_agent_standard(std)
        except ValueError as exc:
            assert "深圳·2013" in str(exc)
        else:
            raise AssertionError(f"{std} 应被拒绝")


def test_env_override_opens_eval_standards():
    os.environ["CE_RAG_AGENT_STANDARDS"] = "gb50500-2013, gb50854-2013, gb50016"
    try:
        config.ensure_agent_standard("gb50016")  # 评测进程放开后可过
        assert "gb50016" in config.agent_allowed_standards()
    finally:
        os.environ.pop("CE_RAG_AGENT_STANDARDS", None)


def test_infer_standard_pricing_keywords_route_to_50500():
    for q in ("综合单价包括哪些费用", "措施项目费怎么取费", "暂列金额计入哪里", "增值税税金费率"):
        assert config.infer_standard(q) == "gb50500-2013", q


def test_infer_standard_default_is_50854():
    for q in ("现浇混凝土板的工程量怎么计算", "满堂脚手架计算规则", "独立基础的项目特征怎么描述", ""):
        assert config.infer_standard(q) == "gb50854-2013", q


if __name__ == "__main__":
    fns = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for fn in fns:
        fn()
        print(f"ok {fn.__name__}")
    print(f"全部 {len(fns)} 例通过")
