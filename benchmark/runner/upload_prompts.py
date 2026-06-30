"""把自包含 prompt 推进 Langfuse Prompt Management（供 Prompt Experiments 用）。

Prompt Experiments 只能跑「单 prompt → 一次 LLM 调用」，**跑不了完整 agent**（无工具
调用 / MCP / 路由）。所以这里纳管的是**自包含、单跳**的 prompt——当前是「意图分类」
（``benchmark/prompts/intent_classify.txt``，``{{query}}`` → 意图标签），可在 Langfuse
UI 里换 prompt 版本 / 换模型横向比，并对金标的期望意图打分，全程无需 agent。

纳管后在 UI：Prompts 里能看到该 prompt 的版本；Datasets→某数据集→New experiment 时
可选它 + model connection 直接跑。再发新版本只需改本地 txt 重跑本脚本（labels 带
``production`` → 新版本即生效，旧版本保留可回溯）。

幂等：同名 ``create_prompt`` 会**新增一个版本**（Langfuse prompt 以版本号累积，不是覆盖）；
内容没变时重复跑也会多一个同内容版本，按需跑即可。

运行（服务器上）：
    uv run --project backend python benchmark/runner/upload_prompts.py
可选 --only intent 仅传意图分类 prompt（当前只接了它）。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from _lf import require_langfuse  # noqa: E402

# 项目根 = benchmark/runner/ 的上两级。
_ROOT = Path(__file__).resolve().parents[2]

INTENT_PROMPT_NAME = "intent-classify"
INTENT_PROMPT_TXT = _ROOT / "benchmark" / "prompts" / "intent_classify.txt"


def upload_intent(client) -> str:
    """把意图分类 prompt 纳管进 Langfuse Prompt Management。

    功能：见模块 docstring；从本地 txt 读 prompt 文本，``create_prompt`` 推一个
        新版本并打 ``production`` 标签使其成为默认生效版本。
    参数：client —— Langfuse 客户端。
    返回：纳管后的 prompt 名（供日志/上层使用）。
    """
    if not INTENT_PROMPT_TXT.exists():
        raise SystemExit(f"找不到 prompt 文本：{INTENT_PROMPT_TXT}")

    text = INTENT_PROMPT_TXT.read_text(encoding="utf-8")
    client.create_prompt(
        name=INTENT_PROMPT_NAME,
        type="text",
        prompt=text,
        labels=["production"],  # 标 production → 该版本成为 UI/SDK 默认取用版本
        config={"temperature": 0},  # 分类任务用贪心，model connection 在 UI 配
    )
    client.flush()
    print(f"✓ 已纳管 prompt «{INTENT_PROMPT_NAME}»（变量 {{{{query}}}}），来源 {INTENT_PROMPT_TXT.name}")
    return INTENT_PROMPT_NAME


def main() -> int:
    """按 --only 选择纳管哪些 prompt。

    功能：见模块 docstring。
    参数：无（命令行 --only）。
    返回：进程退出码。
    """
    parser = argparse.ArgumentParser(description="把自包含 prompt 推进 Langfuse Prompt Management")
    parser.add_argument("--only", choices=["intent"], default=None, help="只传指定 prompt（默认全部已接入的）")
    args = parser.parse_args()

    client = require_langfuse()
    if args.only in (None, "intent"):
        upload_intent(client)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
