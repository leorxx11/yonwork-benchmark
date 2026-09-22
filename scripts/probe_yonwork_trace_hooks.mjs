// 独立 Node 进程验证已安装 OpenClaw 的 hook 与请求 traceparent 是否一致。
// 不在正在运行的 YonWork 注册插件；模型是本进程内 stub，无网络请求。
// Windows: node.exe --input-type=module -e <本文件内容> "D:\yonwork\resources\openclaw\dist"
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { join } from "node:path";
import { pathToFileURL } from "node:url";

const dist = process.argv[1] || "D:\\yonwork\\resources\\openclaw\\dist";
const resolvedSources = [];
async function resolveModule(prefix, exports) {
  let files = readdirSync(dist).filter(p => p.startsWith(prefix + "-") && p.endsWith(".js"));
  // 同一构建可能包含两个副本；必须沿已加载包装函数的真实 import 选择。
  if (files.length > 1) files = files.filter(p => resolvedSources.some(s => s.includes(`"./${p}"`)));
  assert.equal(files.length, 1, `expected one module: ${prefix}`);
  const path = join(dist, files[0]);
  const source = readFileSync(path, "utf8");
  resolvedSources.push(source);
  const module = await import(pathToFileURL(path));
  return Object.fromEntries(exports.map(name => {
    const alias = source.match(new RegExp(`\\b${name} as (\\w+)`))?.[1];
    assert(alias && typeof module[alias] === "function", `missing export: ${name}`);
    return [name, module[alias]];
  }));
}

const diagnostic = await resolveModule("diagnostic-events", [
  "createDiagnosticTraceContext", "formatDiagnosticTraceparent", "setDiagnosticsEnabledForProcess",
]);
const model = await resolveModule("attempt.model-diagnostic-events", ["wrapStreamFnWithDiagnosticModelCallEvents"]);
const hooks = await resolveModule("hook-runner-global", ["initializeGlobalHookRunner", "resetGlobalHookRunner"]);
const seen = [], requests = [];
hooks.initializeGlobalHookRunner({
  hooks: [], plugins: [{ id: "correlation-probe", status: "loaded" }],
  typedHooks: ["model_call_started", "model_call_ended"].map(hookName => ({
    hookName, pluginId: "correlation-probe",
    handler: (event, ctx) => seen.push({
      hookName, runId: event.runId, contextRunId: ctx.runId, callId: event.callId,
      traceId: ctx.trace?.traceId, spanId: ctx.trace?.spanId,
      traceparent: diagnostic.formatDiagnosticTraceparent(ctx.trace),
      outcome: event.outcome,
    }),
  })),
});

try {
  for (const enabled of [true, false]) {
    diagnostic.setDiagnosticsEnabledForProcess(enabled);
    for (const run of ["a", "b"]) {
      const runId = `probe-${enabled}-${run}`;
      let sequence = 0;
      const root = diagnostic.createDiagnosticTraceContext({});
      const wrapped = model.wrapStreamFnWithDiagnosticModelCallEvents((_model, _ctx, options) => {
        requests.push({ runId, callId: `${runId}-${sequence}`, traceparent: options.headers.traceparent });
        if (sequence === 2) throw new Error("controlled stub failure");
        return {};
      }, { runId, sessionKey: `agent:main:${runId}`, provider: "probe", model: "probe",
           trace: root, nextCallId: () => `${runId}-${++sequence}` });
      wrapped({ id: "probe" }, { messages: [] }, {});
      assert.throws(() => wrapped({ id: "probe" }, { messages: [] }, {}), /controlled stub failure/);
    }
  }
  // bounded fire-and-forget hooks 不保证在请求发出之前完成。
  for (let i = 0; seen.length < requests.length * 2 && i < 100; i++) {
    await new Promise(resolve => setTimeout(resolve, 10));
  }
  assert.equal(requests.length, 8);
  assert.equal(seen.length, 16);
  for (const request of requests) {
    const mapped = seen.filter(event => event.callId === request.callId);
    assert.equal(mapped.length, 2);
    assert(mapped.every(event => event.runId === request.runId && event.contextRunId === request.runId
      && event.traceparent === request.traceparent));
  }
  assert.equal(new Set(requests.map(request => request.traceparent)).size, 8);
  console.log(JSON.stringify({
    isolated_component_probe: true, live_product_plugin_installed: false,
    requests: requests.length, hook_events: seen.length,
    exact_run_and_trace_matches: true, unique_call_spans: true,
    success_and_error_covered: true, diagnostics_enabled_and_disabled_covered: true,
  }));
} finally {
  hooks.resetGlobalHookRunner();
}
