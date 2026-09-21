"use strict";

// 连续失败这么多次才放弃轮询。任务动辄跑几十分钟，
// 一次抖动就停掉的话，页面会停在一个永远不再更新的状态上。
const POLL_MAX_FAILURES = 5;

async function replaceFromUrl(target, url) {
  const response = await fetch(url, {headers: {"X-Requested-With": "fetch"}});
  if (!response.ok) {
    throw new Error(`HTTP ${response.status}`);
  }
  target.innerHTML = await response.text();
  return response;
}

// 提示写在目标容器**外面**。容器里装着进度、报告链接和「停止任务」按钮，
// 把它整个换成一行错误文字，等于在最需要停止任务的时候把按钮拿走了。
function pollNotice(target) {
  let notice = target.previousElementSibling;
  if (!notice || !notice.classList.contains("poll-notice")) {
    notice = document.createElement("div");
    notice.className = "poll-notice notice error";
    notice.setAttribute("role", "status");
    target.parentNode.insertBefore(notice, target);
  }
  return notice;
}

document.addEventListener("DOMContentLoaded", () => {
  document.querySelectorAll("[data-load-url]").forEach((button) => {
    button.addEventListener("click", async () => {
      const target = document.querySelector(button.dataset.target);
      if (!target) return;
      button.disabled = true;
      try {
        await replaceFromUrl(target, button.dataset.loadUrl);
      } catch (error) {
        target.textContent = `加载失败：${error.message}`;
      } finally {
        button.disabled = false;
      }
    });
  });

  document.querySelectorAll("[data-poll-url]").forEach((target) => {
    const interval = Math.max(500, Number(target.dataset.pollInterval || 2000));
    let timer = null;
    let failures = 0;

    const stopPolling = () => {
      if (timer !== null) {
        clearInterval(timer);
        timer = null;
      }
    };

    const poll = async () => {
      try {
        const response = await replaceFromUrl(target, target.dataset.pollUrl);
        failures = 0;
        pollNotice(target).hidden = true;
        if (response.headers.get("X-Benchmark-Poll") === "stop") {
          stopPolling();
        }
      } catch (error) {
        failures += 1;
        const notice = pollNotice(target);
        notice.hidden = false;
        if (failures >= POLL_MAX_FAILURES) {
          stopPolling();
          notice.textContent =
            `状态已停止刷新（连续失败 ${failures} 次：${error.message}）。` +
            "下方内容是最后一次成功获取的结果，刷新页面可重试。";
        } else {
          notice.textContent =
            `状态刷新失败 ${failures}/${POLL_MAX_FAILURES}：${error.message}（仍在重试）`;
        }
      }
    };

    timer = setInterval(poll, interval);
    poll();
  });
});
