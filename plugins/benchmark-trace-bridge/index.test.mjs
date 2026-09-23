// node --test plugins/benchmark-trace-bridge/
import assert from "node:assert/strict";
import { mkdtemp, readFile, readdir } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { test } from "node:test";

import plugin, { deliver, resolveOptions, toBinding } from "./index.js";

const TRACE = { traceId: "4bf92f3577b34da6a3ce929d0e0e4736", spanId: "00f067aa0ba902b7" };
const EVENT = {
  runId: "web-abc-C1-r1-xyz", callId: "web-abc-C1-r1-xyz:model:1",
  sessionKey: "agent:main:web-abc-c1-r1-xyz", provider: "p", model: "m",
};

async function collector(reply = (body) => ({ results: body.bindings.map(() => "bound") })) {
  const seen = [];
  const server = createServer((req, res) => {
    let raw = "";
    req.on("data", (c) => (raw += c));
    req.on("end", () => {
      const body = JSON.parse(raw);
      seen.push({ path: req.url, auth: req.headers.authorization, body });
      res.writeHead(200, { "Content-Type": "application/json" });
      res.end(JSON.stringify(reply(body)));
    });
  });
  await new Promise((r) => server.listen(0, "127.0.0.1", r));
  return { seen, server, url: `http://127.0.0.1:${server.address().port}` };
}

async function lines(dir) {
  const files = await readdir(dir);
  const text = await readFile(join(dir, files[0]), "utf8");
  return text.trim().split("\n").map((l) => JSON.parse(l));
}

test("binding 只带白名单字段，ended 带 outcome", () => {
  const started = toBinding("model_call_started", { ...EVENT, messages: ["secret"] }, { trace: TRACE });
  assert.deepEqual(Object.keys(started).sort(),
    ["call_id", "hook", "run_id", "session_key", "span_id", "trace_id"]);
  const ended = toBinding("model_call_ended", { ...EVENT, outcome: "completed" }, { trace: TRACE });
  assert.equal(ended.outcome, "completed");
});

test("投递到 /_control/trace-bindings，带鉴权，回执落本地日志", async () => {
  const { seen, server, url } = await collector();
  const logDir = await mkdtemp(join(tmpdir(), "bridge-"));
  const options = resolveOptions({ collectorUrl: url + "/", collectorToken: "tok", logDir });
  const receipt = await deliver(options, "model_call_started", EVENT, { trace: TRACE });
  server.close();
  assert.equal(receipt, "bound");
  assert.equal(seen[0].path, "/_control/trace-bindings");
  assert.equal(seen[0].auth, "Bearer tok");
  assert.deepEqual(seen[0].body.bindings[0], {
    hook: "model_call_started", run_id: EVENT.runId, call_id: EVENT.callId,
    session_key: EVENT.sessionKey, trace_id: TRACE.traceId, span_id: TRACE.spanId,
  });
  const [line] = await lines(logDir);
  assert.equal(line.receipt, "bound");
  assert.equal(line.run_id, EVENT.runId);
});

test("采集代理不可达不抛错，只记失败回执", async () => {
  const logDir = await mkdtemp(join(tmpdir(), "bridge-"));
  const options = resolveOptions({ collectorUrl: "http://127.0.0.1:1", collectorToken: "tok", logDir });
  const receipt = await deliver(options, "model_call_started", EVENT, { trace: TRACE });
  assert.match(receipt, /^error:/);
  assert.match((await lines(logDir))[0].receipt, /^error:/);
});

test("没配 token 不发请求；缺 trace 不发请求", async () => {
  const { seen, server, url } = await collector();
  const logDir = await mkdtemp(join(tmpdir(), "bridge-"));
  assert.equal(await deliver(resolveOptions({ collectorUrl: url, logDir }),
    "model_call_started", EVENT, { trace: TRACE }), "skipped:no-token");
  assert.equal(await deliver(resolveOptions({ collectorUrl: url, collectorToken: "t", logDir }),
    "model_call_started", EVENT, {}), "skipped:incomplete-event");
  server.close();
  assert.equal(seen.length, 0);
});

test("register 订阅两个 model_call hook，handler 同步返回", () => {
  const hooks = {};
  plugin.register({ registrationMode: "full", pluginConfig: { logDir: tmpdir() },
    logger: { warn() {} }, on: (name, fn) => (hooks[name] = fn) });
  assert.deepEqual(Object.keys(hooks).sort(), ["model_call_ended", "model_call_started"]);
  assert.equal(hooks.model_call_started(EVENT, { trace: TRACE }), undefined);
});

test("非 full 注册模式不订阅", () => {
  const hooks = {};
  plugin.register({ registrationMode: "setup-only", on: (name, fn) => (hooks[name] = fn) });
  assert.equal(Object.keys(hooks).length, 0);
});
