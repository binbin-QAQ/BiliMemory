const port = process.argv[2] || "9327";
const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
let page;
for (let i = 0; i < 10 && !page; i += 1) {
  try {
    const pages = await (await fetch(`http://127.0.0.1:${port}/json`, { cache: "no-store" })).json();
    page = pages.find((item) => item.type === "page" && item.title.includes("MaiBot") && item.url.includes("app.asar"));
  } catch {
    // The app may already be closed.
  }
  if (!page) await sleep(500);
}
if (!page) {
  console.log("MaiBot launcher is not running.");
  process.exit(0);
}
const ws = new WebSocket(page.webSocketDebuggerUrl);
await new Promise((resolve, reject) => {
  ws.addEventListener("open", resolve, { once: true });
  ws.addEventListener("error", reject, { once: true });
});
let nextId = 0;
function call(method, params = {}, timeoutMs = 120_000) {
  return new Promise((resolve, reject) => {
    const id = ++nextId;
    const timer = setTimeout(() => reject(new Error(`stop timeout: ${method}`)), timeoutMs);
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
try {
  const result = await call("Runtime.evaluate", {
    expression: "window.maibotDesktop.services.stopAll().then(() => { setTimeout(() => void window.maibotDesktop.launcher.quit(), 250); return true; })",
    awaitPromise: true,
    returnByValue: true,
    userGesture: true
  });
  if (result.exceptionDetails) throw new Error(result.exceptionDetails.exception?.description || result.exceptionDetails.text);
  console.log("MaiBot and NapCat stopped.");
} finally {
  ws.close();
}
