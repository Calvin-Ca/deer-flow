#!/usr/bin/env bash
# 服务器一次性环境准备脚本。
# 在远程 Linux GPU 服务器上执行：bash pipeline/setup_server.sh
#
# 前置：
#   - 服务器已有 Python 3.12+（用 uv 自动管理也行）
#   - 服务器已有 CUDA 11.8 / 12.x（具体看 MinerU 当前版本要求）
#   - 服务器已配好 GitHub SSH key

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

# ---------- 3. 装 MinerU ----------
# MinerU 是 PDF 解析的核心。版本名/包名近 1 年内变化过两次，
# 这里给两条候选命令，先试新的（mineru），失败再试旧的（magic-pdf）。
# 装完后跑一次 mineru --help 验证。
echo "[setup] 安装 MinerU..."
if uv pip install -U "mineru[core]" 2>/dev/null; then
  echo "[setup]   → 使用新版包名 mineru"
elif uv pip install -U "magic-pdf[full]" 2>/dev/null; then
  echo "[setup]   → 使用旧版包名 magic-pdf"
else
  echo "[setup] ✗ MinerU 安装失败，请手动确认当前包名："
  echo "         https://github.com/opendatalab/MinerU"
  exit 1
fi

# ---------- 4. 验证 ----------
echo "[setup] 验证 Python 环境..."
uv run python -c "import sys; print(f'Python {sys.version}')"

# 验证 GPU 可见
uv run python -c "
import subprocess
r = subprocess.run(['nvidia-smi', '-L'], capture_output=True, text=True)
print('--- GPU ---')
print(r.stdout if r.returncode == 0 else 'nvidia-smi 不可用（无 GPU 或未装驱动）')
" || true

echo
echo "[setup] ✓ 完成。下一步："
echo "  1. 把规范 PDF 放到 data/raw/"
echo "  2. 跑：uv run python pipeline/01_parse_pdf.py --pdf data/raw/<your.pdf>"
