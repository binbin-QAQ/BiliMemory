const port = process.argv[2] || "9327";
const baseUrl = `http://127.0.0.1:${port}`;
const connectDeadline = Date.now() + 120_000;
const readyDeadline = Date.now() + 180_000;
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

let page;
while (Date.now() < connectDeadline) {
  try {
    const pages = await (await fetch(`${baseUrl}/json`, { cache: "no-store" })).json();
    page = pages.find((item) => item.type === "page" && item.title.includes("MaiBot") && item.url.includes("app.asar"));
    if (page) break;
  } catch {
    // The Electron window may still be creating its renderer.
  }
  await sleep(1000);
}
if (!page) throw new Error("MaiBot 窗口未提供本地启动接口，请关闭 MaiBot 后重新运行一键启动。");

const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});

let nextId = 0;
function call(method, params = {}, timeoutMs = 300_000) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => reject(new Error(`启动器接口超时: ${method}`)), timeoutMs);
    const handler = (event) => {
      const message = JSON.parse(event.data);
      if (message.id !== id) return;
      ws.removeEventListener("message", handler);
      clearTimeout(timer);
      if (message.error) reject(new Error(JSON.stringify(message.error)));
      else resolve(message.result);
    };
    ws.addEventListener("message", handler);
    ws.send(JSON.stringify({ id, method, params }));
  });
}

async function evaluate(expression) {
  const result = await call("Runtime.evaluate", {
    expression,
    awaitPromise: true,
    returnByValue: true,
    userGesture: true
  });
  if (result.exceptionDetails) {
    throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  }
  return result.result?.value;
}

function summarize(snapshot) {
  return (snapshot?.services ?? []).map((service) => ({
    id: service.id,
    name: service.name,
    status: service.status,
    health: service.health,
    pid: service.pid,
    error: service.error,
    detail: service.detail
  }));
}

try {
  const bridge = await evaluate("typeof window.maibotDesktop?.services?.startAll");
  if (bridge !== "function") throw new Error("MaiBot 启动器桥接不可用");

  const before = await evaluate("window.maibotDesktop.getSnapshot()");
  const alreadyReady = (before?.services ?? []).filter((item) => item.id === "maibot" || item.id === "napcat")
    .every((item) => item.status === "running" && item.health === "ready");
  if (!alreadyReady) {
    await evaluate("window.maibotDesktop.services.startAll()");
  }

  let last;
  while (Date.now() < readyDeadline) {
    last = await evaluate("window.maibotDesktop.getSnapshot()");
    const managed = (last?.services ?? []).filter((item) => item.id === "maibot" || item.id === "napcat");
    const failed = managed.find((item) => item.status === "error");
    if (failed) throw new Error(`${failed.name || failed.id} 启动失败: ${failed.error || failed.detail || "未知错误"}`);
    if (managed.length === 2 && managed.every((item) => item.status === "running" && item.health === "ready")) {
      console.log(JSON.stringify(summarize(last), null, 2));
      process.exitCode = 0;
      break;
    }
    await sleep(1500);
  }
  if (process.exitCode !== 0 && !last) throw new Error("未读取到 MaiBot 服务状态");
  if (process.exitCode !== 0) {
    console.log(JSON.stringify(summarize(last), null, 2));
    throw new Error("MaiBot 服务健康检查超时");
  }
} finally {
  ws.close();
}
