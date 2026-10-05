#!/usr/bin/env node
// Drive the live Dashboard via CDP. --dump-dom hangs on EventSource(/api/events).

import { spawn } from "node:child_process";
import { writeFileSync } from "node:fs";
import { createServer } from "node:net";

const [pageUrl, screenshotPath, domPath, chromeBin, profileDir] = process.argv.slice(2);
if (!pageUrl || !screenshotPath || !domPath || !chromeBin || !profileDir) {
  console.error("usage: capture-dashboard.mjs URL SCREENSHOT DOM CHROME_BIN PROFILE");
  process.exit(2);
}

function freePort() {
  return new Promise((resolve, reject) => {
    const server = createServer();
    server.listen(0, "127.0.0.1", () => {
      const address = server.address();
      const port = typeof address === "object" && address ? address.port : 0;
      server.close((error) => (error ? reject(error) : resolve(port)));
    });
    server.on("error", reject);
  });
}

async function waitJson(url, timeoutMs) {
  const deadline = Date.now() + timeoutMs;
  let lastError = "devtools_unreached";
  while (Date.now() < deadline) {
    try {
      const response = await fetch(url);
      if (response.ok) {
        return await response.json();
      }
      lastError = `http_${response.status}`;
    } catch (error) {
      lastError = error instanceof Error ? error.message : String(error);
    }
    await new Promise((resolve) => setTimeout(resolve, 100));
  }
  throw new Error(lastError);
}

function openCdp(wsUrl) {
  const socket = new WebSocket(wsUrl);
  let nextId = 0;
  const pending = new Map();
  socket.addEventListener("message", (event) => {
    const message = JSON.parse(String(event.data));
    if (message.id == null || !pending.has(message.id)) {
      return;
    }
    const { resolve, reject } = pending.get(message.id);
    pending.delete(message.id);
    if (message.error) {
      reject(new Error(message.error.message || "cdp_error"));
      return;
    }
    resolve(message.result);
  });
  const ready = new Promise((resolve, reject) => {
    socket.addEventListener("open", resolve, { once: true });
    socket.addEventListener("error", () => reject(new Error("cdp_socket_error")), {
      once: true,
    });
  });
  return {
    ready,
    send(method, params = {}) {
      const id = ++nextId;
      return new Promise((resolve, reject) => {
        pending.set(id, { resolve, reject });
        socket.send(JSON.stringify({ id, method, params }));
      });
    },
    close() {
      socket.close();
    },
  };
}

const port = await freePort();
const chrome = spawn(
  chromeBin,
  [
    "--headless=new",
    "--no-sandbox",
    "--disable-gpu",
    "--disable-dev-shm-usage",
    "--hide-scrollbars",
    "--no-first-run",
    "--no-default-browser-check",
    "--disable-background-networking",
    `--user-data-dir=${profileDir}`,
    `--remote-debugging-address=127.0.0.1`,
    `--remote-debugging-port=${port}`,
    "--window-size=1440,1400",
    pageUrl,
  ],
  { stdio: ["ignore", "pipe", "pipe"] },
);

let stderr = "";
chrome.stderr.on("data", (chunk) => {
  stderr += String(chunk);
});

const shutdown = () => {
  if (chrome.exitCode == null && chrome.signalCode == null) {
    chrome.kill("SIGTERM");
  }
};
process.on("exit", shutdown);

try {
  const targets = await waitJson(`http://127.0.0.1:${port}/json/list`, 8000);
  const page = (Array.isArray(targets) ? targets : []).find((item) => item.type === "page");
  if (!page?.webSocketDebuggerUrl) {
    throw new Error("devtools_page_missing");
  }
  const session = openCdp(page.webSocketDebuggerUrl);
  await session.ready;
  await session.send("Runtime.enable");
  await session.send("Page.enable");

  const deadline = Date.now() + 15000;
  let html = "";
  while (Date.now() < deadline) {
    const { result } = await session.send("Runtime.evaluate", {
      expression: `(() => ({
        pending: document.querySelector("#metric-pending")?.textContent ?? "",
        hasCard: Boolean(document.querySelector("article.job-card[data-job-id]")),
        html: document.documentElement.outerHTML
      }))()`,
      returnByValue: true,
    });
    const value = result?.value;
    if (value?.hasCard && value.pending && value.pending !== "—") {
      html = value.html;
      break;
    }
    await new Promise((resolve) => setTimeout(resolve, 250));
  }
  if (!html) {
    throw new Error("dashboard_cards_not_rendered");
  }

  const shot = await session.send("Page.captureScreenshot", {
    format: "png",
    fromSurface: true,
  });
  if (!shot?.data) {
    throw new Error("screenshot_empty");
  }
  writeFileSync(screenshotPath, Buffer.from(shot.data, "base64"));
  writeFileSync(domPath, `<!doctype html>\n${html}\n`);
  session.close();
  console.log(
    JSON.stringify(
      {
        url: pageUrl,
        screenshot: screenshotPath,
        dom: domPath,
        chrome: chromeBin,
        debugPort: port,
      },
      null,
      2,
    ),
  );
} catch (error) {
  console.error(stderr.slice(-2000));
  console.error(error instanceof Error ? error.message : error);
  process.exitCode = 1;
} finally {
  shutdown();
  await new Promise((resolve) => {
    if (chrome.exitCode != null || chrome.signalCode != null) {
      resolve();
      return;
    }
    const timer = setTimeout(() => {
      chrome.kill("SIGKILL");
      resolve();
    }, 2000);
    chrome.once("exit", () => {
      clearTimeout(timer);
      resolve();
    });
  });
}
