#!/usr/bin/env bash
#
# ce-serve.sh — 内网试用：一键拉起 CE 全栈服务（tmux 常驻 + 健康自检）
#
# 服务与启动顺序（有依赖先后）：
#   ① :8100 ce-rag    ce-code       uv run python -m service.rag_api
#   ② :8102 ce-db     ce-code       uv run python -m service.db_api
#   ③ :8101 ce-task   ce-services    uv run python main.py
#   ④ :8001 gateway   backend        make gateway（uvicorn，无 --reload，单进程）
#   ⑤ :3000 前端      frontend       pnpm start（生产模式，需先 pnpm build）
#
# 为什么 tmux 而非 nohup：nohup 在本项目服务器上遇到过 Exit 125 静默失败；tmux 每服务一个 window，
#   可 `tmux attach -t ce` 实时看日志、单独排障，`--stop` 一键全停。
#
# 不含的东西（属基础设施，须另行在跑，见 TODO「P1 依赖服务确认在跑」）：
#   PG ce_cost :5433 / Milvus :19530 / embed :8097 / vLLM Qwen3-8B :8099 / agent 基座 qwen-plus(DashScope)。
#   ce-rag/ce-db 健康不过，多半是这些下游依赖没起——按提示 `tmux attach -t ce` 看对应窗口报错。
#
# 用法：
#   scripts/ce-serve.sh              # 按序起四服务 + 逐个 /health 自检
#   scripts/ce-serve.sh --build      # 起之前先 `pnpm build` 前端（生产模式必需，首次/前端有改动时用）
#   scripts/ce-serve.sh --restart    # 先停旧 session 再起
#   scripts/ce-serve.sh --stop       # 停全部（kill tmux session ce）
#
# 注：首次或依赖变更后，需各自 `uv sync`（ce-code / ce-services）+ 前端 `pnpm install`，本脚本不代劳。

set -u
REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"
SESSION=ce

stop() {
  if tmux kill-session -t "$SESSION" 2>/dev/null; then echo "已停 tmux session '$SESSION'"; else echo "无运行中的 '$SESSION'"; fi
}

# 轮询健康：$1=名称 $2=URL $3=超时秒。2xx/3xx 视为就绪（前端根路径会 307 跳转登录）。
wait_health() {
  local name=$1 url=$2 timeout=${3:-60} i=0 code=000
  printf "  等 %-9s %s " "$name" "$url"
  while (( i < timeout )); do
    code=$(curl -s -o /dev/null -w '%{http_code}' "$url" 2>/dev/null || true)
    if [[ "$code" =~ ^[23][0-9][0-9]$ ]]; then echo "就绪 ($code)"; return 0; fi
    printf "."; sleep 1; i=$((i+1))
  done
  echo " 超时（${timeout}s，最后 code=$code）"
  echo "  → 排障：tmux attach -t $SESSION（切窗口 Ctrl-b n / Ctrl-b p，脱离 Ctrl-b d）"
  return 1
}

# 在 tmux session 里开一个窗口跑常驻命令（exec 让进程取代 shell，--stop 时干净退出）。
start_win() {
  local win=$1 cmd=$2
  tmux new-window -t "$SESSION" -n "$win"
  tmux send-keys -t "$SESSION:$win" "$cmd" C-m
}

case "${1:-}" in
  --stop) stop; exit 0 ;;
  --restart) stop ;;
  --build) echo "== 前端 pnpm build（生产模式）=="; ( cd frontend && pnpm build ) || { echo "前端 build 失败，终止"; exit 1; } ;;
esac

if tmux has-session -t "$SESSION" 2>/dev/null; then
  echo "session '$SESSION' 已在运行；先 'scripts/ce-serve.sh --restart' 或 '--stop'"; exit 1
fi

echo "== 起 CE 服务（tmux session '$SESSION'）=="

# ① ce-rag :8100（先起，任务层依赖它）
tmux new-session -d -s "$SESSION" -n ce-rag
tmux send-keys -t "$SESSION:ce-rag" 'cd ce-code && exec uv run python -m service.rag_api' C-m
wait_health ce-rag http://localhost:8100/health 90 || { echo "ce-rag 未就绪（多半下游依赖 Milvus/embed 没起），终止"; exit 1; }

# ② ce-db :8102
start_win ce-db 'cd ce-code && exec uv run python -m service.db_api'
wait_health ce-db http://localhost:8102/health 90 || { echo "ce-db 未就绪（多半下游依赖 PG 没起），终止"; exit 1; }

# ③ 任务服务 :8101
start_win tasks 'cd ce-services && exec uv run python main.py'
wait_health tasks http://localhost:8101/health 60 || { echo "任务服务未就绪，终止"; exit 1; }

# ④ gateway :8001
start_win gateway 'cd backend && exec make gateway'
wait_health gateway http://localhost:8001/health 60 || { echo "gateway 未就绪，终止"; exit 1; }

# ⑤ 前端 :3000（生产模式；若报错先跑 --build）
start_win frontend 'cd frontend && exec pnpm start'
wait_health frontend http://localhost:3000 90 || { echo "前端未就绪（是否忘了 pnpm build？用 --build 重跑），终止"; exit 1; }

echo
echo "✅ 服务全部就绪：ce-rag :8100 / ce-db :8102 / ce-task :8101 / gateway :8001 / 前端 :3000"
echo "   看日志：tmux attach -t $SESSION   停全部：scripts/ce-serve.sh --stop"
