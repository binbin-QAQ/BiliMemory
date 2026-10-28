export const name = "bilimemory";
export const inject = ["tools"];

const DEFAULT_ADMIN_URL = "http://127.0.0.1:8765";
const LOOPBACK_HOSTS = new Set(["127.0.0.1", "localhost", "::1"]);

function resolveAdminUrl(config = {}) {
  const raw = process.env.BILIMEMORY_ADMIN_URL || config.adminUrl || DEFAULT_ADMIN_URL;
  const url = new URL(raw);
  if (!["http:", "https:"].includes(url.protocol)) throw new Error("BiliMemory admin URL must use http or https");
  if (!LOOPBACK_HOSTS.has(url.hostname)) throw new Error("BiliMemory admin URL must point to a loopback host");
  return url.href.replace(/\/$/, "");
}

async function requestJson(config, pathname, init = {}) {
  const baseUrl = resolveAdminUrl(config);
  const response = await fetch(baseUrl + pathname, {
    ...init,
    headers: {
      Accept: "application/json",
      ...(init.body === undefined ? {} : { "Content-Type": "application/json" }),
      ...(init.headers || {})
    },
    signal: AbortSignal.timeout(init.timeoutMs || 30000)
  });
  const raw = await response.text();
  let payload = {};
  if (raw) {
    try {
      payload = JSON.parse(raw);
    } catch {
      payload = { text: raw.slice(0, 4000) };
    }
  }
  if (!response.ok) {
    const detail = payload?.error || payload?.message || payload?.text || response.statusText;
    throw new Error(`BiliMemory HTTP ${response.status}: ${String(detail).slice(0, 2000)}`);
  }
  return payload;
}

function postJson(config, pathname, body = {}, timeoutMs = 30000) {
  return requestJson(config, pathname, {
    method: "POST",
    body: JSON.stringify(body),
    timeoutMs
  });
}

function toJsonSchema(spec) {
  if (Array.isArray(spec)) return spec.map(toJsonSchema);
  if (spec === null || typeof spec !== "object") return spec;
  const result = {};
  for (const [key, value] of Object.entries(spec)) {
    if (key === "required") continue;
    if (key === "properties") {
      result.properties = {};
      const required = [];
      for (const [name, property] of Object.entries(value || {})) {
        if (property?.required === true) required.push(name);
        result.properties[name] = toJsonSchema(property);
      }
      if (propertyRequired(required)) result.required = required;
    } else if (key === "items" || key === "oneOf") {
      result[key] = toJsonSchema(value);
    } else {
      result[key] = value;
    }
  }
  if (result.properties !== undefined && result.type === undefined) result.type = "object";
  return result;
}

function propertyRequired(required) {
  return required.length > 0;
}

function parametersToSchema(spec = {}) {
  const properties = {};
  const required = [];
  for (const [name, property] of Object.entries(spec)) {
    if (property?.required === true) required.push(name);
    properties[name] = toJsonSchema(property);
  }
  return {
    type: "object",
    additionalProperties: false,
    properties,
    ...(required.length > 0 ? { required } : {})
  };
}

function makeTool(options) {
  const parameters = parametersToSchema(options.parameters);
  return {
    ...options,
    parameters,
    output: {
      schema: toJsonSchema(options.output.schema),
      render: options.output.render
    }
  };
}

function output(title) {
  return {
    schema: {
      type: "object",
      additionalProperties: true,
      properties: {
        ok: { type: "boolean", required: true, description: "Whether the operation succeeded." }
      }
    },
    render: (_args, value) => [
      {
        type: "text",
        text: `${title}\n${JSON.stringify(value, null, 2)}`
      }
    ]
  };
}

function describeError(error) {
  return error instanceof Error ? error.message : String(error);
}

function compactStatus(data) {
  const maibot = data?.maibot || {};
  const tasks = Array.isArray(data?.tasks) ? data.tasks : [];
  return {
    ok: true,
    backend: data?.server || null,
    bilibili: {
      profileExists: Boolean(data?.bilibili?.profileExists),
      storageStateExists: Boolean(data?.bilibili?.storageStateExists)
    },
    embeddingOnline: Boolean(data?.embeddingOnline),
    maibot: {
      ok: Boolean(maibot.ok),
      runtimeReady: Boolean(maibot.runtimeReady),
      degraded: Boolean(maibot.degraded),
      retrievalMode: maibot.retrievalMode || "",
      activeCount: Number(maibot.activeCount || 0),
      unavailable: maibot.unavailable || []
    },
    activeTask: data?.activeTask || null,
    recentTasks: tasks.slice(0, 6).map((task) => ({
      id: task.id,
      kind: task.kind,
      label: task.label,
      status: task.status,
      startedAt: task.startedAt,
      endedAt: task.endedAt
    })),
    bilibiliAllowlistCount: Number(data?.allowlist?.accounts?.length || 0),
    qqAllowlistCount: Number(data?.qqAllowlist?.users?.length || 0),
    dm: {
      running: Boolean(data?.dm?.running),
      loginOk: Boolean(data?.dm?.login?.ok)
    }
  };
}

export function apply(ctx, config = {}) {
  ctx.tools.register(makeTool({
    name: "bilimemory_status",
    description: "Read BiliMemory backend status: Bilibili login state, local embedding, MaiBot memory health, current task, recent tasks, whitelist counts, and Bilibili DM status. This tool is read-only.",
    parameters: {},
    output: output("BiliMemory status"),
    async execute() {
      return compactStatus(await requestJson(config, "/api/status", { timeoutMs: 45000 }));
    },
    presentCall: () => ({ card: "generic", title: "Check BiliMemory status", kind: "read", rawInput: {} })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_doctor",
    description: "Check whether the local BiliMemory environment is ready for this DSH plugin. Reports backend, Bilibili login, embedding, MaiBot memory, and QQ DM readiness with actionable hints.",
    parameters: {},
    output: output("BiliMemory environment check"),
    async execute() {
      const adminUrl = resolveAdminUrl(config);
      try {
        const status = compactStatus(await requestJson(config, "/api/status", { timeoutMs: 45000 }));
        const checks = [
          { name: "admin-api", ok: true, detail: adminUrl },
          { name: "bilibili-login", ok: status.bilibili.profileExists && status.bilibili.storageStateExists, hint: "Run bilibili_tool.py login once." },
          { name: "local-embedding", ok: status.embeddingOnline, hint: "Start local-embedding/server.mjs on 127.0.0.1:11435." },
          { name: "maibot-memory", ok: status.maibot.ok && status.maibot.runtimeReady && !status.maibot.degraded, hint: "Start MaiBot and ensure A-Memorix is ready." },
          { name: "bilibili-dm", ok: status.dm.running && status.dm.loginOk, hint: "Optional: start the Bilibili DM service." }
        ];
        return { ok: checks.filter((item) => item.name !== "bilibili-dm").every((item) => item.ok), adminUrl, checks, status };
      } catch (error) {
        return {
          ok: false,
          adminUrl,
          error: describeError(error),
          requiredEnvironment: [
            "Windows 10/11 x64",
            "Node.js 22+ for DSH and the local embedding service",
            "Python 3.12 with requirements.txt installed",
            "BiliMemory admin API running at 127.0.0.1:8765",
            "MaiBot OneKey and NapCat for QQ integration",
            "Playwright-compatible browser and Bilibili login",
            "FFmpeg for media processing"
          ]
        };
      }
    },
    presentCall: () => ({ card: "generic", title: "Check BiliMemory environment", kind: "read", rawInput: {} })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_collect",
    description: "Start a BiliMemory collection task. Supports home feed, keyword/tag search, a Bilibili section URL, or direct BV/b23 URLs. The task runs in the local BiliMemory backend and returns a task id.",
    parameters: {
      mode: {
        type: "string",
        required: true,
        enum: ["home", "tag", "section", "direct"],
        description: "Collection mode."
      },
      max: {
        type: "integer",
        description: "Maximum videos to process, 1-20. Defaults to the backend setting."
      },
      value: {
        type: "string",
        description: "Tag/keyword for tag mode, or a Bilibili section URL for section mode."
      },
      urls: {
        type: "array",
        items: { type: "string" },
        description: "Bilibili BV/b23 URLs for direct mode."
      },
      importToMemory: {
        type: "boolean",
        description: "Whether to import confirmed video summaries into long-term memory. Defaults to true."
      }
    },
    output: output("BiliMemory collection task"),
    async execute(args) {
      const mode = args.mode;
      const max = Math.max(1, Math.min(20, Number(args.max || 5)));
      const body = { mode, max, import: args.importToMemory !== false };
      if (mode === "tag" || mode === "section") {
        if (!args.value?.trim()) throw new Error(`mode=${mode} requires a value`);
        body.value = args.value.trim();
      }
      if (mode === "direct") {
        if (!Array.isArray(args.urls) || args.urls.length === 0) throw new Error("mode=direct requires at least one URL");
        body.urls = args.urls;
      }
      const result = await postJson(config, "/api/actions/collect", body, 60000);
      return { ok: true, task: result.task || null };
    },
    presentCall: (args) => ({ card: "generic", title: `Collect BiliMemory videos (${args.mode})`, kind: "other", rawInput: args })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_search",
    description: "Search Bilibili through BiliMemory and resolve matching videos. Returns a backend task id; use bilimemory_task to read progress and results.",
    parameters: {
      query: { type: "string", required: true, description: "Natural-language or Bilibili search query." },
      max: { type: "integer", description: "Maximum returned videos, 1-5." }
    },
    output: output("BiliMemory search task"),
    async execute(args) {
      const query = String(args.query || "").trim();
      if (!query) throw new Error("query is required");
      const result = await postJson(config, "/api/actions/resolve", {
        query: query.slice(0, 100),
        max: Math.max(1, Math.min(5, Number(args.max || 3)))
      }, 60000);
      return { ok: true, task: result.task || null };
    },
    presentCall: (args) => ({ card: "generic", title: `Search BiliMemory: ${args.query}`, kind: "search", rawInput: args })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_task",
    description: "Read one BiliMemory task by id, including its status, exit code, and complete local log.",
    parameters: {
      taskId: { type: "string", required: true, description: "Task id returned by collect/search/login." }
    },
    output: output("BiliMemory task"),
    async execute(args) {
      const taskId = String(args.taskId || "").trim();
      if (!/^[A-Za-z0-9-]{1,100}$/.test(taskId)) throw new Error("invalid task id");
      const task = await requestJson(config, `/api/tasks/${encodeURIComponent(taskId)}`, { timeoutMs: 30000 });
      return { ok: true, task };
    },
    presentCall: (args) => ({ card: "generic", title: `Read BiliMemory task ${args.taskId}`, kind: "read", rawInput: args })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_login",
    description: "Start the local Bilibili browser login task. A browser window may open; finish login there and then use bilimemory_task with the returned task id.",
    parameters: {},
    output: output("BiliMemory login task"),
    async execute() {
      const result = await postJson(config, "/api/actions/login", {}, 30000);
      return { ok: true, task: result.task || null };
    },
    presentCall: () => ({ card: "generic", title: "Start Bilibili login", kind: "other", rawInput: {} })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_stop",
    description: "Request cancellation of the currently running BiliMemory collection/search task.",
    parameters: {},
    output: output("Stop BiliMemory task"),
    async execute() {
      const result = await postJson(config, "/api/actions/stop", {}, 15000);
      return { ok: true, result };
    },
    presentCall: () => ({ card: "generic", title: "Stop BiliMemory task", kind: "other", rawInput: {} })
  }));
  ctx.tools.register(makeTool({
    name: "bilimemory_qq_status",
    description: "Read the QQ link watcher status: process and PID, ports 8766 and 7998, configured groups/owner presence, search policy, and QQ user allowlist. Voice permissions are not part of this plugin.",
    parameters: {},
    output: output("BiliMemory QQ status"),
    async execute() {
      return { ok: true, status: await requestJson(config, "/api/qq/status", { timeoutMs: 20000 }) };
    },
    presentCall: () => ({ card: "generic", title: "Check BiliMemory QQ status", kind: "read", rawInput: {} })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_qq_allowlist",
    description: "List, add, or remove users from the local QQ feature allowlist. Only links and search permissions are supported; voice and cover permissions were removed.",
    parameters: {
      action: { type: "string", required: true, enum: ["list", "add", "remove"], description: "Allowlist operation." },
      qq: { type: "string", description: "QQ number for add/remove." },
      label: { type: "string", description: "Optional display label." },
      permissions: { type: "array", items: { type: "string", enum: ["links", "search"] }, description: "Permissions for add; defaults to links and search." }
    },
    output: output("BiliMemory QQ allowlist"),
    async execute(args) {
      const current = await requestJson(config, "/api/qq-allowlist", { timeoutMs: 15000 });
      const sanitizeUser = (user) => ({
        ...user,
        permissions: (Array.isArray(user.permissions) ? user.permissions : []).filter((item) => item === "links" || item === "search")
      });
      if (args.action === "list") return { ok: true, allowlist: { ...current, users: (Array.isArray(current.users) ? current.users : []).map(sanitizeUser) } };
      const qq = String(args.qq || "").trim();
      if (!/^\d{3,30}$/.test(qq)) throw new Error("valid qq is required for add/remove");
      let users = (Array.isArray(current.users) ? current.users : []).map(sanitizeUser);
      if (args.action === "remove") {
        users = users.filter((user) => String(user.qq) !== qq);
      } else {
        const permissions = [...new Set((Array.isArray(args.permissions) ? args.permissions : ["links", "search"]).filter((item) => item === "links" || item === "search"))];
        users = users.filter((user) => String(user.qq) !== qq);
        users.push({ qq, label: String(args.label || "QQ user").trim().slice(0, 40), permissions });
      }
      const result = await postJson(config, "/api/qq-allowlist", { enabled: true, users }, 20000);
      return { ok: true, allowlist: result.qqAllowlist || { enabled: true, users } };
    },
    presentCall: (args) => ({ card: "generic", title: `QQ allowlist: ${args.action}`, kind: "other", rawInput: args })
  }));

  ctx.tools.register(makeTool({
    name: "bilimemory_qq_restart",
    description: "Restart the local QQ link watcher. Requires MaiBot OneKey and NapCat to be running and the QQ account to be configured.",
    parameters: {},
    output: output("Restart BiliMemory QQ watcher"),
    async execute() {
      const result = await postJson(config, "/api/actions/qq/restart", {}, 30000);
      return { ok: true, result };
    },
    presentCall: () => ({ card: "generic", title: "Restart QQ link watcher", kind: "other", rawInput: {} })
  }));

}
