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

// 新建测试页的模式行。JS 只增删行，取值仍由表单自己完成：
// 两个重复字段（mode_product / mode_model）按下标配对，服务端 zip 起来。
// 所以即使这段脚本没跑起来，第一行照样能正常提交一个模式。
function setupModeRows() {
  const container = document.getElementById("mode-rows");
  const addButton = document.getElementById("mode-add");
  if (!container || !addButton) return;

  const refresh = () => {
    const rows = container.querySelectorAll(".mode-row");
    // 只剩一行时不给删——删空了提交上去是「一个模式都没有」，
    // 与其让服务端报错，不如在这里就不可能发生。
    rows.forEach((row) => {
      const button = row.querySelector(".mode-remove");
      if (button) button.disabled = rows.length <= 1;
    });
  };

  addButton.addEventListener("click", () => {
    const last = container.querySelector(".mode-row:last-child");
    if (!last) return;
    const copy = last.cloneNode(true);
    // cloneNode 复制的是 HTML 属性而不是当前选中状态，
    // 所以要把用户刚选的值手工搬过去，否则新行永远是第一个选项。
    const sources = last.querySelectorAll("select");
    copy.querySelectorAll("select").forEach((select, index) => {
      select.value = sources[index].value;
    });
    container.appendChild(copy);
    refresh();
    copy.querySelector("select").focus();
  });

  container.addEventListener("click", (event) => {
    const button = event.target.closest(".mode-remove");
    if (!button) return;
    button.closest(".mode-row").remove();
    refresh();
  });

  refresh();
}

document.addEventListener("DOMContentLoaded", () => {
  setupModeRows();

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
