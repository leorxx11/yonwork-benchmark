#!/usr/bin/env bash
# 在宿主机（WSL）上原生跑 Worker，替代 compose 里那个容器 Worker。
#
# 为什么需要这个：WorkBuddy 驱动每轮要起一个 Windows 进程（WorkBuddy.exe），
# 容器里既没有 WSL interop 也看不到 /mnt/d，所以**容器 Worker 永远跑不了 WorkBuddy**。
# YonWork 走的是 Host API，容器里能跑——但要做 YonWork × WorkBuddy 的横向对比，
# 两个产品必须由同一个 Worker 串行跑完，那就只能是宿主机这个。
#
# 仍然只允许一个 Worker：MySQL 咨询锁（job_store.exclusive_worker_lock）会拦住
# 后启动的那个。并发跑批会让 NewAPI 用量按时间窗张冠李戴，这是正确性前提不是运维约定。
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

python_bin="$repo_root/.venv/bin/python"
if [[ ! -x "$python_bin" ]]; then
  echo "找不到 $python_bin；先建虚拟环境并装 runner/requirements.txt" >&2
  exit 1
fi

# 代理会把 127.0.0.1 的请求吞掉并返回空，看起来像「服务没起来」（CLAUDE.md 一-坑 1）。
# no_proxy 用 127.* 通配不管用，只能整个关掉。
export no_proxy='*' NO_PROXY='*'

if [[ ! -f "$repo_root/.env" ]]; then
  echo "缺少 .env；先跑 ./scripts/bootstrap.sh" >&2
  exit 1
fi

# WorkBuddy 驱动需要能起 Windows 程序。提前查，免得排队的任务领了再一条条失败。
if ! compgen -G "/proc/sys/fs/binfmt_misc/WSLInterop*" >/dev/null; then
  echo "警告：这台机器没有 WSL interop，WorkBuddy 的任务会失败（YonWork 不受影响）" >&2
fi

# 容器 Worker 还活着的话，锁在它手里，这个进程会直接退出。
# 与其让人对着锁的报错猜，不如在这里说清楚该敲哪条命令。
if command -v docker >/dev/null 2>&1; then
  if docker compose ps --status running --services 2>/dev/null | grep -qx worker; then
    cat >&2 <<'HINT'
容器 Worker 正在跑，它持有 MySQL 咨询锁，宿主机 Worker 起不来。
先停掉它：

    docker compose stop worker

⚠️ 之后别用裸 `docker compose up -d`——worker 是 restart: unless-stopped，
   会被重新拉起来又把锁抢走。只起 Web 用：

    docker compose up -d web

跑完想切回容器 Worker：先 Ctrl-C 结束这个进程，再 docker compose start worker
HINT
    exit 2
  fi
fi

echo "宿主机 Worker 启动中（Ctrl-C 停止；当前这一轮会先收尾）"
exec "$python_bin" -m runner.worker "$@"
