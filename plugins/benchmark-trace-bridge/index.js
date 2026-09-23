// benchmark-trace-bridge：把 OpenClaw 模型调用 hook 里的 (traceId, spanId) → runId
// 交给采集代理，补上 YonWork 1.0.10 起丢掉的逐请求归属。
//
// 只观察：handler 立刻返回，不等投递，不改请求，不阻塞模型调用。
// 只发白名单元数据；提示词、回答、鉴权信息一概不碰，所以不需要 allowConversationAccess
// （那个开关只管 llm_input / llm_output 等，model_call_* 不在其中）。
// 投递失败只落本地日志，不重试：绑定缺了，请求就如实留成未归属。
import { appendFile, mkdir } from "node:fs/promises";
import { request } from "node:http";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

export const PLUGIN_ID = "benchmark-trace-bridge";
export const HOOKS = ["model_call_started", "model_call_ended"];
const PLUGIN_DIR = dirname(fileURLToPath(import.meta.url));

export function resolveOptions(config) {
  const c = config && typeof config === "object" && !Array.isArray(config) ? config : {};
  const timeoutMs = Number(c.timeoutMs);
  return {
    collectorUrl: String(c.collectorUrl || "http://127.0.0.1:3312").replace(/\/+$/, ""),
    token: String(c.collectorToken || ""),
    logDir: String(c.logDir || join(PLUGIN_DIR, "logs")),
    timeoutMs: timeoutMs > 0 ? timeoutMs : 2000,
  };
}

export function toBinding(hook, event, ctx) {
  const trace = ctx?.trace ?? {};
  return {
    hook,
    run_id: event?.runId ?? ctx?.runId,
    call_id: event?.callId,
    session_key: event?.sessionKey ?? ctx?.sessionKey,
    trace_id: trace.traceId,
    span_id: trace.spanId,
    ...(hook === "model_call_ended" && typeof event?.outcome === "string"
      ? { outcome: event.outcome }
      : {}),
  };
}

// 用 node:http 而不是 fetch：YonWork 网关的 preload 会替换全局 fetch。
export function post(options, binding) {
  if (!options.token) return Promise.resolve("skipped:no-token");
  return new Promise((resolve) => {
    const body = JSON.stringify({ bindings: [binding] });
    let url;
    try {
      url = new URL(options.collectorUrl + "/_control/trace-bindings");
    } catch {
      resolve("error:bad-url");
      return;
    }
    const req = request(
      {
        hostname: url.hostname,
        port: url.port,
        path: url.pathname,
        method: "POST",
        agent: false,
        timeout: options.timeoutMs,
        headers: {
          "Content-Type": "application/json",
          "Content-Length": Buffer.byteLength(body),
          Authorization: `Bearer ${options.token}`,
        },
      },
      (res) => {
        let raw = "";
        res.setEncoding("utf8");
        res.on("data", (chunk) => (raw += chunk));
        res.on("end", () => {
          if (res.statusCode !== 200) return resolve(`http-${res.statusCode}`);
          try {
            resolve(String(JSON.parse(raw).results?.[0] ?? "no-receipt"));
          } catch {
            resolve("no-receipt");
          }
        });
      },
    );
    req.on("timeout", () => req.destroy(new Error("timeout")));
    req.on("error", (error) => resolve(`error:${error.code || error.message}`));
    req.end(body);
  });
}

// 本地证据：每个 hook 事件一行，带采集代理的回执。验收时拿它直接核对
// event.runId == BenchmarkId，不必再靠源码推断。
async function record(options, binding, receipt) {
  const at = new Date();
  const line = JSON.stringify({ at: at.toISOString(), ...binding, receipt }) + "\n";
  await mkdir(options.logDir, { recursive: true });
  await appendFile(join(options.logDir, `bindings-${at.toISOString().slice(0, 10)}.jsonl`), line, "utf8");
}

export async function deliver(options, hook, event, ctx) {
  const binding = toBinding(hook, event, ctx);
  const receipt = binding.run_id && binding.trace_id && binding.span_id
    ? await post(options, binding)
    : "skipped:incomplete-event";
  try {
    await record(options, binding, receipt);
  } catch {
    // 日志写不了也不能影响被测产品。
  }
  return receipt;
}

export default {
  id: PLUGIN_ID,
  name: "Benchmark Trace Bridge",
  description: "Reports model-call trace spans to the benchmark collector for per-request attribution",
  register(api) {
    if (api?.registrationMode !== undefined && api.registrationMode !== "full") return;
    const options = resolveOptions(api?.pluginConfig);
    if (!options.token) api?.logger?.warn?.(`${PLUGIN_ID}: collectorToken 未配置，只记本地日志`);
    for (const hook of HOOKS) {
      api.on(hook, (event, ctx) => {
        void deliver(options, hook, event, ctx);
      });
    }
  },
};
