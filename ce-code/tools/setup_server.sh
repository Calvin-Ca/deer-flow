#!/usr/bin/env bash
# 服务器一次性环境准备脚本。
# 在远程 Linux 服务器上执行：bash tools/setup_server.sh
#
# 前置：
#   - 服务器已有 Python 3.12+（用 uv 自动管理也行）
#   - 服务器已配好 GitHub SSH key
#
# 注：阶段0 PDF 解析只调远程 MinerU API（见 DEV.md），本机不装 MinerU CLI。

set -euo pipefail

cd "$(dirname "$0")/.."  # 进入 ce-code 根目录

# ---------- 1. 装 uv（如果还没装） ----------
if ! command -v uv >/dev/null 2>&1; then
  echo "[setup] 安装 uv..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  # shellcheck disable=SC1090
  source "$HOME/.cargo/env" 2>/dev/null || source "$HOME/.local/bin/env" 2>/dev/null || true
fi

echo "[setup] uv 版本：$(uv --version)"

# ---------- 2. 创建虚拟环境 + 装基础依赖 ----------
echo "[setup] 创建 .venv 并同步 pyproject.toml 依赖..."
uv sync

# ---------- 3. 验证 ----------
echo "[setup] 验证 Python 环境..."
uv run python -c "import sys; print(f'Python {sys.version}')"

echo
echo "[setup] ✓ 完成。下一步："
echo "  1. 把规范 PDF 放到 data/raw/"
echo "  2. 跑：uv run python -m parser mineru --pdf data/raw/<your.pdf>"
