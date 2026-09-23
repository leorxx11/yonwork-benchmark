# YonWork 产品缺陷清单

> 本职工作产出，比自动化工具本身更值钱。代码注释里的「产品缺陷 #N」指这里的编号，
> **编号只增不改**（原 CLAUDE.md 第六节「六-N」）。新发现的缺陷追加到末尾。
>
> 上报状态：**5 条均未上报**。走公司内部渠道，先确认是否为测试构建有意放宽。
> #5 有完整复现数据，最好先报；它和「API 发起的轮次界面不渲染」
> （[开发日志](history/dev-log-2026-09.md) 七-1.1）很可能是同一处规范化不一致的两个表现，上报时一起说。

前两条是调查阶段查到的，后三条是搭 runner 和跑批的过程中撞出来的。

1. **高危：本地服务默认对局域网开放且免鉴权。**
   Host API 默认 `BIND=0.0.0.0`、`AUTH_MODE=trusted`（跳过全部 token 校验）；
   CDP TCP 代理默认 `0.0.0.0:9222`（应用日志自己写着「任何机器都可驱动本应用，风险极高」）。
   实测无任何凭据调 `/api/auth-runtime/session/status` 拿到完整登录态
   （accessToken、userId、tenantId、用户名）。叠加后 = 同网段任何人可无凭据调用全部 364 条路由。
   **本机已于 2026-09-21 缓解**（过程见[开发日志](history/dev-log-2026-09.md) 七-0.1），实测改完两个 `0.0.0.0` 消失、
   无 token 调该端点返回 401，而 CDP 和整条自动化链路零改动照常工作：
   `YONCLAW_HOST_API_BIND=127.0.0.1`、`YONCLAW_HOST_API_AUTH_MODE=token`、`YONCLAW_CDP_PROXY_BIND=127.0.0.1`。
   **但产品默认值没变，这条缺陷本身依然要上报。**
2. **`yonworkctl` 路径不匹配，官方 CLI 完全不可用。**
   主进程写 `%APPDATA%\yonwork\host-api-runtime.json`，CLI 读 `%APPDATA%\yonclaw\`，
   导致应用在跑时任何命令都返回「YonWork is not running」+ 退出码 7。
   说明该 CLI 在这个构建上从未被端到端验证过。
3. **端上 `/api/usage/recent-token-history` 在漏记。**（2026-09-20 实测）
   改过模型配置之后，连续 6 轮完成的对话一条都没进这个端点，
   而同样这些轮在会话 JSONL 和 NewAPI 后台**都有记录，且两者数字完全一致**
   （16,179 / 16,188 / 16,435）。两个互相独立的来源对得上，产品自己的端点是那个异常值。
   看 `/reconcile/<suite_id>` 页，一眼能看出来。
4. **`modelSelection` 字段名写错时静默回落默认模型。**
   只认 `{"providerAccountId", "modelId"}`，写别的形状**不报错**：
   HTTP 200、答案正常，实际跑的却是智能体默认模型。
   会话日志里有实证（`probe-mode` / `probe-prov` 两轮跑的是 `deepseek-v4-flash`）。
   已加 `model-match` 断言兜底，判 `Invalid`。
5. **`sessionKey` 大小写不一致，一轮对话被拆成两条会话。**（2026-09-21 实测）
   `sessionKey` 里带大写字母时，应用会存出两条会话元数据：
   原样大小写那条只有标题（`displayName`），**没有 `sessionId`、没有对话内容**；
   全小写那条有 `sessionId` 和完整对话，**但界面里点不到**。
   症状是「用户消息和 Agent 回答不在一个界面」。
   统计（`/api/sessions/list-metadata`）：含大写的 key **10 个里 9 个分裂**，
   全小写的 4 个**一个都没分裂**；改成全小写后新跑的轮次恢复成单条。
   说明写会话元数据和建会话绑定这两条路径对 key 做了不同的规范化。
   我们这边已经规避（`session_key_for` 统一小写），但**产品自己应该修**——
   任何用混合大小写 sessionKey 的调用方都会踩到，而且不报错。

⚠️ [调查报告](history/yonwork-automation-report.md) 的凭据**已于 2026-09-20 脱敏**
（host-api token / accessToken / gateway token / 用户名 → `<REDACTED:…>`）。
别往回填真实值。
