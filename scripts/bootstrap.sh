#!/usr/bin/env bash
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$repo_root"

for command_name in docker openssl; do
  if ! command -v "$command_name" >/dev/null 2>&1; then
    echo "缺少命令：$command_name" >&2
    exit 1
  fi
done
docker compose version >/dev/null

read_env_value() {
  local source_path="$1"
  local key="$2"
  if [[ -f "$source_path" ]]; then
    sed -n "s/^${key}=//p" "$source_path" | tail -1
  fi
}

container_compose_project() {
  docker inspect --format '{{ index .Config.Labels "com.docker.compose.project" }}' \
    "$1" 2>/dev/null || true
}

read_legacy_container_env() {
  local container_name="$1"
  local expected_project="$2"
  local key="$3"
  if [[ "$(container_compose_project "$container_name")" != "$expected_project" ]]; then
    return
  fi
  docker inspect --format '{{range .Config.Env}}{{println .}}{{end}}' "$container_name" \
    | sed -n "s/^${key}=//p" | tail -1
}

env_path="$repo_root/.env"
if [[ ! -f "$env_path" ]]; then
  yonwork_data="${YONWORK_DATA_DIR:-}"
  if [[ -z "$yonwork_data" ]]; then
    mapfile -t candidates < <(
      find /mnt/c/Users -mindepth 4 -maxdepth 4 -type d \
        -path '*/AppData/Roaming/yonwork' 2>/dev/null | sort
    )
    if [[ ${#candidates[@]} -eq 0 ]]; then
      echo "找不到 Windows 的 %APPDATA%\\yonwork 目录；确认 YonWork 已至少启动过一次。" >&2
      echo "也可以先设置 YONWORK_DATA_DIR 再重新运行。" >&2
      exit 1
    fi
    if [[ ${#candidates[@]} -gt 1 ]]; then
      echo "找到多个 YonWork 数据目录，请先设置 YONWORK_DATA_DIR：" >&2
      printf '  %s\n' "${candidates[@]}" >&2
      exit 1
    fi
    yonwork_data="${candidates[0]}"
  fi

  if [[ ! -d "$yonwork_data" ]]; then
    echo "YONWORK_DATA_DIR 不存在：$yonwork_data" >&2
    exit 1
  fi

  windows_user_root="$(cd "$yonwork_data/../../.." && pwd)"
  fixture_dir="$windows_user_root/Documents/yonwork-benchmark"
  fixture_name='西游记[lunarora.com].txt'
  mkdir -p "$fixture_dir"
  if [[ ! -e "$fixture_dir/$fixture_name" ]]; then
    cp "$repo_root/cases/fixtures/$fixture_name" "$fixture_dir/$fixture_name"
  fi

  if command -v wslpath >/dev/null 2>&1; then
    fixture_windows="$(wslpath -w "$fixture_dir/$fixture_name")"
  else
    fixture_windows="$fixture_dir/$fixture_name"
  fi

  legacy_db_env="$repo_root/infra/.env"
  bench_mysql_root_password="$(read_env_value "$legacy_db_env" MYSQL_ROOT_PASSWORD)"
  bench_mysql_database="$(read_env_value "$legacy_db_env" MYSQL_DATABASE)"
  bench_mysql_user="$(read_env_value "$legacy_db_env" MYSQL_USER)"
  bench_mysql_password="$(read_env_value "$legacy_db_env" MYSQL_PASSWORD)"
  bench_db_port="$(read_env_value "$legacy_db_env" BENCH_DB_PORT)"
  bench_mysql_root_password="${bench_mysql_root_password:-$(read_legacy_container_env bench-mysql infra MYSQL_ROOT_PASSWORD)}"
  bench_mysql_database="${bench_mysql_database:-$(read_legacy_container_env bench-mysql infra MYSQL_DATABASE)}"
  bench_mysql_user="${bench_mysql_user:-$(read_legacy_container_env bench-mysql infra MYSQL_USER)}"
  bench_mysql_password="${bench_mysql_password:-$(read_legacy_container_env bench-mysql infra MYSQL_PASSWORD)}"
  if [[ -d "$repo_root/infra/data/mysql" &&
        ( -z "$bench_mysql_root_password" || -z "$bench_mysql_password" ) ]]; then
    echo "检测到已有 MySQL 数据，但 $legacy_db_env 中缺少旧密码；为避免锁死旧数据，已停止。" >&2
    exit 1
  fi

  legacy_newapi_credentials="$repo_root/newapi/credentials.env"
  legacy_newapi_env="$repo_root/newapi/.env"
  bench_newapi_user_id="$(read_env_value "$legacy_newapi_credentials" NEWAPI_USER_ID)"
  bench_newapi_token="$(read_env_value "$legacy_newapi_credentials" NEWAPI_ACCESS_TOKEN)"
  bench_session_secret="$(read_env_value "$legacy_newapi_env" SESSION_SECRET)"
  bench_session_secret="${bench_session_secret:-$(read_legacy_container_env new-api newapi SESSION_SECRET)}"

  bench_mysql_root_password="${bench_mysql_root_password:-$(openssl rand -hex 24)}"
  bench_mysql_database="${bench_mysql_database:-benchmark}"
  bench_mysql_user="${bench_mysql_user:-bench}"
  bench_mysql_password="${bench_mysql_password:-$(openssl rand -hex 24)}"
  bench_db_port="${bench_db_port:-3307}"
  bench_newapi_user_id="${bench_newapi_user_id:-1}"
  bench_session_secret="${bench_session_secret:-$(openssl rand -hex 32)}"

  umask 077
  cat >"$env_path" <<EOF
HOST_UID=$(id -u)
HOST_GID=$(id -g)
MYSQL_ROOT_PASSWORD=$bench_mysql_root_password
MYSQL_DATABASE=$bench_mysql_database
MYSQL_USER=$bench_mysql_user
MYSQL_PASSWORD=$bench_mysql_password
BENCH_DB_PORT=$bench_db_port
BENCH_WEB_PORT=8000
SESSION_SECRET=$bench_session_secret
NEWAPI_PORT=3000
NEWAPI_USER_ID=$bench_newapi_user_id
NEWAPI_ACCESS_TOKEN=$bench_newapi_token
YONWORK_DATA_DIR=$yonwork_data
YONWORK_XIYOUJI_PATH=$fixture_windows
EOF
  chmod 600 "$env_path"
  echo "已生成 $env_path，并把长文本 fixture 放到 Windows Documents。"
else
  echo "沿用现有 $env_path。"
fi

for required_key in MYSQL_ROOT_PASSWORD MYSQL_PASSWORD SESSION_SECRET YONWORK_DATA_DIR; do
  if [[ -z "$(read_env_value "$env_path" "$required_key")" ]]; then
    echo "$env_path 缺少 $required_key；删除该文件让脚本重新生成，或补齐后重试。" >&2
    exit 1
  fi
done

configured_yonwork_data="$(read_env_value "$env_path" YONWORK_DATA_DIR)"
if [[ ! -d "$configured_yonwork_data" ]]; then
  echo "YONWORK_DATA_DIR 不存在：$configured_yonwork_data" >&2
  exit 1
fi

# bind mount 的宿主目录必须先由当前用户创建，否则 Docker 可能创建成 root 所有，
# 非 root worker 随后无法写 results。
mkdir -p "$repo_root/results" "$repo_root/infra/data" \
  "$repo_root/newapi/data" "$repo_root/newapi/logs"
if [[ ! -w "$repo_root/results" ]]; then
  echo "结果目录不可写：$repo_root/results" >&2
  exit 1
fi

docker compose config --quiet
docker compose pull mysql new-api
docker compose build web

stop_legacy_compose() {
  local container_name="$1"
  local expected_project="$2"
  local compose_path="$3"
  local actual_project
  actual_project="$(container_compose_project "$container_name")"
  if [[ -z "$actual_project" ]]; then
    return
  fi
  if [[ "$actual_project" != "$expected_project" ]]; then
    echo "容器名 $container_name 已被其他项目占用（Compose project=$actual_project），请先处理。" >&2
    exit 1
  fi
  echo "正在无损接管旧版 $expected_project 服务（bind mount 数据会保留）……"
  docker compose --env-file "$env_path" -p "$expected_project" -f "$compose_path" down
}

# 老版本把 MySQL 和 NewAPI 分成两个 Compose 项目，会占用相同端口。
# 先完成镜像下载/构建，再停止旧容器，尽量缩短迁移中断时间。
stop_legacy_compose bench-mysql infra "$repo_root/infra/docker-compose.yml"
stop_legacy_compose new-api newapi "$repo_root/newapi/docker-compose.yml"

docker compose up -d --wait --wait-timeout 180
docker compose ps

web_port="$(read_env_value "$env_path" BENCH_WEB_PORT)"
echo
echo "基准测试控制台：http://127.0.0.1:${web_port:-8000}"
echo "首次使用 NewAPI 时，仍需在 http://127.0.0.1:3000 完成账号/渠道配置；"
echo "生成系统访问令牌后填入 .env 的 NEWAPI_ACCESS_TOKEN，再运行 docker compose up -d。"
