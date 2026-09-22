# NewAPI 本地实例

基准测试的**后台侧**数据源：和端上 `/api/usage/recent-token-history` 按同一 `runId` 对账，
用来抓「后台与端上不一致、关心跳仍扣费」那类问题（CLAUDE.md 七-4）。

```bash
cd newapi
cp .env.example .env
# 用 `openssl rand -hex 32` 生成随机值并填入 .env 的 SESSION_SECRET
docker compose up -d      # 起
docker compose logs -f    # 看日志
docker compose down       # 停（数据留在 ./data）
```

- 地址 `http://127.0.0.1:3000`，**只绑 loopback**，局域网访问不到。
  mirrored 模式下 Windows 侧访问照常，`scripts/newapi_stats.ps1` 的默认 BaseUrl 就是它。
- 存储 SQLite，落在 `./data/`（root 属主，容器里写的）。要上 MySQL 见 compose 里的注释。
- 账号密码在 `credentials.env`（chmod 600，已 gitignore）。

统一代理超时的处理见 [排查记录](../docs/newapi-stall.md)。本机 DeepSeek 渠道需设
`setting.http_protocol=http1`，配合 Compose 的 `RELAY_MAX_IDLE_CONNS_PER_HOST=-1`
关闭上游空闲连接复用；换机新建渠道时也要设置。跑批前用
`.venv/bin/python scripts/probe_newapi.py --stream`（仓库根目录执行）做真实模型探活。

## 拿系统访问令牌

`scripts/newapi_stats.ps1` 靠它调 `/api/log/self`。当前版本生成令牌要过一道安全验证（要重输密码），
命令行走不通，去界面点：**个人设置 → 生成系统访问令牌**，然后

```powershell
$env:NEWAPI_ACCESS_TOKEN = "<刚生成的令牌>"
./scripts/newapi_stats.ps1 -StartTime "2026-09-20 21:00:00" -EndTime "2026-09-20 21:10:00"
```

顺手填进 `credentials.env` 留档。脚本已经改成只从环境变量读，不再把令牌写死在文件里。

## 装 Docker

`scripts/install_docker_wsl.sh`（需要 sudo，跑一次）。两个要点：

1. 用 Ubuntu 自带源的 `docker.io`（29.1.3）而不是 `download.docker.com`——那个域名会间歇性
   Connection reset。
2. **dockerd 是 systemd 服务，不继承 shell 的 `http_proxy`**，必须给它配 drop-in，
   否则 `registry-1.docker.io` 直连返回 000，镜像拉不下来。mirrored 只解决「打得到代理」。
