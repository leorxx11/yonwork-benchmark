#!/usr/bin/env bash
# 在 WSL2 (Ubuntu 24.04, systemd 已开) 里装 Docker Engine。
# 需要 sudo，跑一次就够；重复跑无害。
#
#   sudo bash scripts/install_docker_wsl.sh
#
# 为什么用 Ubuntu 自带源而不是 download.docker.com：
#   那个域名实测会间歇性 Connection reset（GFW 抽风，重试有时又好），
#   而 noble-updates/universe 里的 docker.io 已经是 29.1.3，跟官方源同代，
#   docker-compose-v2 也有 2.40.3，`docker compose` 子命令照常可用。
# 网络实测（2026-09-20，本机）：
#   archive.ubuntu.com   直连可达 → apt 不用走代理
#   registry-1.docker.io 直连不通 → dockerd 拉镜像必须走代理
set -euo pipefail

# 跟着当前 shell 的代理走（想覆盖就设 DOCKER_PULL_PROXY）。
# 注意 sudo 默认 env_reset 会清掉 http_proxy，想让它被读到得用 `sudo -E`。
PROXY="${DOCKER_PULL_PROXY:-${http_proxy:-${HTTP_PROXY:-http://127.0.0.1:7897}}}"
TARGET_USER="${SUDO_USER:-${USER:-root}}"

if [[ $EUID -ne 0 ]]; then
    echo "需要 root：sudo bash $0" >&2
    exit 1
fi

echo "==> 1/3 安装 docker.io 与 docker-compose-v2"
apt-get update -qq
apt-get install -y -qq docker.io docker-compose-v2

echo "==> 2/3 让 dockerd 拉镜像走代理（${PROXY}）"
# dockerd 是 systemd 服务，**不继承**任何 shell 环境变量，所以必须显式配给它。
# mirrored 网络模式只解决「打得到代理」，解决不了「daemon 知不知道有代理」。
# 实测没这段的话 registry-1.docker.io 直连返回 000，镜像拉不下来。
mkdir -p /etc/systemd/system/docker.service.d
cat > /etc/systemd/system/docker.service.d/http-proxy.conf <<EOF
[Service]
Environment="HTTP_PROXY=${PROXY}"
Environment="HTTPS_PROXY=${PROXY}"
Environment="NO_PROXY=localhost,127.0.0.1,::1"
EOF

systemctl daemon-reload
systemctl enable --now docker
systemctl restart docker  # 让代理配置生效

echo "==> 3/3 把 ${TARGET_USER} 加进 docker 组（免 sudo）"
usermod -aG docker "${TARGET_USER}"

echo
echo "Docker Server: $(docker version --format '{{.Server.Version}}')"
echo "组变更要新会话才生效；当前会话可用 'sg docker -c \"docker ps\"' 顶一下，"
echo "或者在 Windows 里跑 'wsl --shutdown' 重进。"
