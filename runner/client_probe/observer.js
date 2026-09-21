// 注入 renderer 的观察器。所有时间戳用 performance.now()，
// 与 Python 侧的时钟无关——实测 Windows 比 WSL 快 7.85s，跨端相减必错。
((PROMPT_HEAD) => {
  const STAGE = ".yonclaw-main-stage";           // 首页和会话页都在
  const CHAT_ROOT = ".yonclaw-chat-scroll-region"; // 只有产生对话后才出现
  const LIST = ".yonclaw-chat-max-width";
  const PROSE = ".prose";          // 只有 assistant 气泡有，用户消息没有
  const COMPOSER = "[data-testid='yonclaw-composer-controls']";

  if (window.__probeStop) { window.__probeStop(); }

  const state = {
    t0: null, events: [], lastText: "", textUpdates: 0,
    firstContentDom: null, firstVisibleFrame: null, lastChange: null,
    assistantSeen: null, userSeen: null, baseline: null,
    generating: false, generationDone: null,
  };
  window.__probeState = state;

  const emit = (name, extra) => {
    const e = Object.assign({ name, t: performance.now() }, extra || {});
    state.events.push(e);
    if (window.__probeEvent) window.__probeEvent(JSON.stringify(e));
  };

  const list = () => document.querySelector(CHAT_ROOT + " " + LIST);
  const turns = () => { const l = list(); return l ? [...l.children] : []; };

  // 基线：记住安装时**已存在的那些元素**，之后只看不在其中的。
  // 早先用「索引」（turns().slice(n)）——换个会话就废：用户在新会话里发消息时
  // 列表只有 2 条，而基线是 26，slice 出来永远是空的，一个事件都采不到。实测踩过。
  // 记元素则两种情况都对：同会话追加时正确排除历史；切换会话时旧元素全不在了，
  // 新会话的每一条都算新增——正是想要的。
  const seen = new WeakSet();
  turns().forEach(t => seen.add(t));
  state.baseline = turns().length;

  const newTurns = () => turns().filter(t => !seen.has(t));

  // 界面在「等待」阶段会显示这些占位文案，它们不算已经看到回答内容。
  const PLACEHOLDERS = ["等待结果返回", "后台命令执行中", "正在生成", "刚刚"];
  const GENERATING = "正在生成";

  // ⚠️ 流式过程中**文本不在 `.prose` 里**：实测 `.prose` 一直停在 48 字，
  // 而整个气泡从 98 涨到 2284，结束时才把全文灌进 `.prose`。
  // 盯 `.prose` 测出来的是「整块渲染完成」，不是「首次可见」。所以这里取整个气泡的文本。
  // 按**整行**丢弃占位文案。早先按子串剥离，
  // 「后台命令执行中，正在等待结果返回… 已等待 00:01」会剩下
  // 「，正在… 已等待 00:01」这种残渣被当成内容，算出负的首字滞后。实测踩过。
  const NOISE = /(等待结果返回|后台命令执行中|已等待|正在生成|^刚刚$|^\d+\s*(秒|分钟|小时|天)前$|^\d{1,2}:\d{2}$)/;
  const bubbleText = (el) => (el.innerText || "")
    .split("\n")
    .map(x => x.trim())
    .filter(x => x && !NOISE.test(x))
    .join(" ")
    .trim();


  // 新增的第一条气泡永远是用户自己那条——它就是 T1，也是「用户感知的起点」。
  // 不排除的话它会被当成「首段内容」，算出负的首字滞后。实测踩过两次。
  // PROMPT_HEAD 作为二次保险；watch 模式下事先不知道 prompt，留空即可。
  const isUserBubble = (el, idx) =>
    idx === 0 || (PROMPT_HEAD && (el.innerText || "").includes(PROMPT_HEAD));

  const scan = () => {
    const all = newTurns();
    if (state.userSeen === null && all.length) {
      state.userSeen = performance.now();
      emit("user-message", { text: (all[0].innerText || "").trim().slice(0, 30) });
    }
    const ts = all.filter((t, i) => !isUserBubble(t, i));
    if (!ts.length) return;
    const last = ts[ts.length - 1];

    if (state.assistantSeen === null) {
      state.assistantSeen = performance.now();
      emit("assistant-created", { raw: (last.innerText || "").trim().slice(0, 20) });
    }

    const raw = (last.innerText || "");
    const generating = raw.includes(GENERATING);
    if (generating && !state.generating) { state.generating = true; emit("generating-on"); }
    if (!generating && state.generating) {
      state.generating = false;
      state.generationDone = performance.now();
      emit("generation-done");   // ← T5：确定性的完成信号，不靠静默窗口猜
    }

    const text = bubbleText(last);
    if (!text) return;
    if (state.firstContentDom === null) {
      state.firstContentDom = performance.now();
      emit("first-content-dom", { chars: text.length });
      // DOM 变了不等于像素上了屏；补一帧拿近似的可见时刻。
      // ⚠️ 窗口被遮挡/最小化时 rAF 会被节流甚至完全不触发，
      // 那种情况下这个值只能是 null，不能拿 DOM 时间顶替。
      requestAnimationFrame(() => {
        state.firstVisibleFrame = performance.now();
        emit("first-visible-frame");
      });
    }
    if (text !== state.lastText) {
      state.lastText = text;
      state.textUpdates += 1;
      state.lastChange = performance.now();
      emit("text-update", { chars: text.length, n: state.textUpdates });
    }
  };

  // 挂在 stage 上而不是 chatRoot 上：全新会话点开时 chatRoot 还不存在，
  // 挂 chatRoot 会直接装不上（实测报 no-chat-root）。
  const stage = document.querySelector(STAGE) || document.body;
  const obs = new MutationObserver(scan);
  obs.observe(stage, { subtree: true, childList: true, characterData: true });

  // Stop→Send 在输入框区域，通常不在 chatRoot 子树里，单独挂一个。
  const composer = document.querySelector(COMPOSER);
  let obs2 = null;
  if (composer) {
    obs2 = new MutationObserver(() => emit("composer-change", {
      label: (composer.innerText || "").trim().slice(0, 20)
    }));
    obs2.observe(composer, { subtree: true, childList: true, attributes: true });
  }

  window.__probeStop = () => { obs.disconnect(); if (obs2) obs2.disconnect(); };
  state.t0 = performance.now();
  emit("t0", {
    baseline: state.baseline,
    stage: stage === document.body ? "body(fallback)" : STAGE,
    chatRootPresent: !!document.querySelector(CHAT_ROOT),
    composerFound: !!composer,
    promptHead: PROMPT_HEAD,
  });
  return "ok";
})(__PROMPT_HEAD__)
