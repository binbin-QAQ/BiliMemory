import http from 'node:http';
import fs from 'node:fs';
import fsp from 'node:fs/promises';
import path from 'node:path';
import net from 'node:net';
import crypto from 'node:crypto';
import { spawn } from 'node:child_process';
import { fileURLToPath } from 'node:url';

const DIR = path.dirname(fileURLToPath(import.meta.url));
const ROOT = path.resolve(DIR, '..');
const PROJECT_ROOT = path.resolve(ROOT, '..');
const PUBLIC = path.join(DIR, 'public');
const OUTPUTS = path.join(PROJECT_ROOT, 'outputs');
const USER_HOME = process.env.USERPROFILE || process.env.HOME || path.resolve('.');
const DESKTOPS = [path.join(USER_HOME, 'OneDrive', 'Desktop'), path.join(USER_HOME, 'Desktop')].filter((value, index, all) => all.indexOf(value) === index && fs.existsSync(value));
const CONFIG = path.join(ROOT, 'config.json');
const ALLOWLIST = path.join(ROOT, 'mention_allowlist.json');
const DATA = path.join(ROOT, 'data');
const QQ_ALLOWLIST = path.join(DATA, 'qq_user_allowlist.json');
const SUMMARIES = path.join(DATA, 'summaries');
const TOOL = path.join(ROOT, 'bilibili_tool.py');
const DM_SERVICE = path.join(ROOT, 'bili_dm_service.py');
const DM_STATUS = path.join(DATA, 'bili_dm_status.json');
const DM_PERSONA = path.join(ROOT, 'persona.md');
const DM_PID = path.join(DATA, 'bili_dm.pid');
const QQ_PID = path.join(DATA, 'qq_watcher.pid');
const QQ_WATCHER = path.join(ROOT, 'qq_link_watcher.py');
const EMBED_DIR = path.join(ROOT, '..', 'local-embedding');
function discoverMaiBotRoot() {
  if (process.env.MAIBOT_ROOT) return path.resolve(process.env.MAIBOT_ROOT);
  const launcherRoot = process.env.APPDATA ? path.join(process.env.APPDATA, 'MaiBotOneKeyDesktop') : '';
  if (launcherRoot && fs.existsSync(launcherRoot)) {
    const candidates = fs.readdirSync(launcherRoot, { withFileTypes: true })
      .filter(entry => entry.isDirectory())
      .map(entry => path.join(launcherRoot, entry.name, 'modules', 'MaiBot'))
      .filter(candidate => fs.existsSync(candidate))
      .sort((left, right) => fs.statSync(right).mtimeMs - fs.statSync(left).mtimeMs);
    if (candidates.length) return candidates[0];
  }
  return path.join(USER_HOME, 'MaiBot');
}
function discoverMaiBotPython(root) {
  if (process.env.MAIBOT_PYTHON) return process.env.MAIBOT_PYTHON;
  const environments = path.join(root, '.onekey', 'environments');
  if (fs.existsSync(environments)) {
    const candidates = fs.readdirSync(environments, { withFileTypes: true })
      .filter(entry => entry.isDirectory())
      .map(entry => path.join(environments, entry.name, 'python.exe'))
      .filter(candidate => fs.existsSync(candidate));
    if (candidates.length) return candidates[0];
  }
  return 'python';
}
const MAIBOT_ROOT = discoverMaiBotRoot();
const PYTHON = discoverMaiBotPython(MAIBOT_ROOT);
const WEBUI_CFG = path.join(MAIBOT_ROOT, 'data', 'webui.json');
const WEBUI = 'http://127.0.0.1:8001';
const HOST = '127.0.0.1';
const START_PORT = Number(process.env.ADMIN_PORT || 8765);
const startedAt = Date.now();
const tasks = [];
let activeTask = null;
let port = START_PORT;
let memoryCache = { at: 0, value: null };
let memoryInFlight = null;

function readJson(file, fallback) { try { return JSON.parse(fs.readFileSync(file, 'utf8').replace(/^\uFEFF/, '')); } catch { return fallback; } }
async function writeJson(file, value) { await fsp.writeFile(file, JSON.stringify(value, null, 2), 'utf8'); }
function newestDirectories(root, limit = 50) {
  try {
    return fs.readdirSync(root, { withFileTypes: true })
      .filter(entry => entry.isDirectory())
      .map(entry => {
        const directory = path.join(root, entry.name);
        return { directory, mtimeMs: fs.statSync(directory).mtimeMs };
      })
      .sort((a, b) => b.mtimeMs - a.mtimeMs)
      .slice(0, limit);
  } catch { return []; }
}

function sanitize(text) { return String(text || '').replace(/sk-[A-Za-z0-9_-]{10,}/g, 'sk-***').replace(/(SESSDATA=)[^;\s]+/gi, '$1***').replace(/(access_token["'\s:=]+)[^"'\s,}]+/gi, '$1***'); }
function int(value, min, max, fallback) { const parsed = Number.parseInt(value, 10); return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback; }
function num(value, min, max, fallback) { const parsed = Number(value); return Number.isFinite(parsed) ? Math.min(max, Math.max(min, parsed)) : fallback; }
function taskView(task, withLog = false) { return { id: task.id, kind: task.kind, label: task.label, status: task.status, startedAt: task.startedAt, endedAt: task.endedAt, exitCode: task.exitCode, ...(withLog ? { log: task.log } : {}) }; }
function appendTaskLog(task, value) { task.log = sanitize(task.log + value); if (task.log.length > 250000) task.log = task.log.slice(-250000); task.updatedAt = Date.now(); fs.appendFile(path.join(DIR, 'logs', `${task.id}.log`), sanitize(value), () => {}); }
function createTask(kind, label, args, script = TOOL) {
  if (activeTask?.status === 'running') return null;
  const task = { id: crypto.randomUUID(), kind, label, args, status: 'running', startedAt: Date.now(), endedAt: null, exitCode: null, log: '', updatedAt: Date.now() };
  tasks.unshift(task); if (tasks.length > 40) tasks.length = 40; activeTask = task;
  const child = spawn(PYTHON, [script, ...args], { cwd: ROOT, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
  task.child = child;
  child.stdout.on('data', data => appendTaskLog(task, data.toString('utf8')));
  child.stderr.on('data', data => appendTaskLog(task, data.toString('utf8')));
  child.on('error', error => appendTaskLog(task, `\n[spawn error] ${error.message}\n`));
  child.on('close', code => { task.status = code === 0 ? 'completed' : 'failed'; task.exitCode = code; task.endedAt = Date.now(); task.updatedAt = Date.now(); activeTask = null; });
  return taskView(task);
}
function runServiceJson(args, timeout = 120000) {
  return new Promise((resolve, reject) => {
    const child = spawn(PYTHON, [DM_SERVICE, ...args], { cwd: ROOT, windowsHide: true, env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
    let stdout = ''; let stderr = ''; let settled = false;
    const timer = setTimeout(() => { if (!settled) { settled = true; child.kill(); reject(new Error('DM service timed out')); } }, timeout);
    child.stdout.on('data', data => { stdout += data.toString('utf8'); });
    child.stderr.on('data', data => { stderr += data.toString('utf8'); });
    child.on('error', error => { if (!settled) { settled = true; clearTimeout(timer); reject(error); } });
    child.on('close', code => {
      if (settled) return; settled = true; clearTimeout(timer);
      if (code !== 0) return reject(new Error(sanitize((stderr || stdout || `exit ${code}`).slice(-4000))));
      try { resolve(JSON.parse(stdout)); } catch { reject(new Error('DM service returned invalid JSON: ' + sanitize(stdout.slice(-1000)))); }
    });
  });
}

function processAlive(pid) {
  if (!Number.isInteger(pid) || pid <= 0) return false;
  try { process.kill(pid, 0); return true; } catch { return false; }
}
function tcpCheck(host, targetPort, timeout = 1200) {
  return new Promise(resolve => { const socket = net.connect({ host, port: targetPort }); let done = false; const finish = value => { if (done) return; done = true; socket.destroy(); resolve(value); }; socket.setTimeout(timeout); socket.once('connect', () => finish(true)); socket.once('timeout', () => finish(false)); socket.once('error', () => finish(false)); });
}
async function authWebUi() {
  const cfg = readJson(WEBUI_CFG, {}); if (!cfg.access_token) throw new Error('MaiBot WebUI token missing');
  const response = await fetch(WEBUI + '/api/webui/auth/verify', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ token: cfg.access_token }), signal: AbortSignal.timeout(5000) });
  if (!response.ok) throw new Error(`MaiBot auth ${response.status}`);
  const values = response.headers.getSetCookie?.() || [response.headers.get('set-cookie') || ''];
  return { cookie: values.map(value => value.split(';')[0]).filter(Boolean).join('; ') };
}
async function memorySnapshotUncached() {
  try {
    const { cookie } = await authWebUi(); const headers = { Cookie: cookie };
    const [a, b, c] = await Promise.all([
      fetch(WEBUI + '/api/webui/memory/runtime/self-check', { headers, signal: AbortSignal.timeout(7000) }),
      fetch(WEBUI + '/api/webui/memory/v5/status', { headers, signal: AbortSignal.timeout(7000) }),
      fetch(WEBUI + '/api/webui/memory/import/chat-targets', { headers, signal: AbortSignal.timeout(7000) })
    ]);
    const check = await a.json(); const status = await b.json(); const chats = await c.json();
    return { ok: Boolean(check.report?.ok), runtimeReady: Boolean(check.runtime_ready), degraded: Boolean(check.degraded), retrievalMode: check.retrieval_mode || '', vectorDimension: Number(check.report?.vector_store_dimension || 0), configuredDimension: Number(check.report?.configured_dimension || 0), channels: check.available_channels || [], unavailable: check.unavailable_channels || [], activeCount: Number(status.active_count || 0), inactiveCount: Number(status.inactive_count || 0), pinnedCount: Number(status.pinned_count || 0), chats: chats.data || [], checkedAt: Date.now() };
  } catch (error) { return { ok: false, runtimeReady: false, degraded: true, error: String(error.message || error), chats: [] }; }
}
async function memorySnapshot() {
  if (memoryCache.value && Date.now() - memoryCache.at < 45000) return memoryCache.value;
  if (memoryInFlight) return memoryInFlight;
  memoryInFlight = memorySnapshotUncached()
    .then(value => { memoryCache = { at: Date.now(), value }; return value; })
    .finally(() => { memoryInFlight = null; });
  return memoryInFlight;
}
async function statusPayload() {
  const config = readJson(CONFIG, {}); const allowlist = readJson(ALLOWLIST, { accounts: [] });
  const [embeddingOnline, memory] = await Promise.all([tcpCheck('127.0.0.1', 11435), memorySnapshot()]);
  return { server: { host: HOST, port, uptimeSeconds: Math.floor((Date.now() - startedAt) / 1000) }, bilibili: { profileExists: fs.existsSync(path.join(ROOT, 'browser-profile')), storageStateExists: fs.existsSync(path.join(ROOT, 'storage_state.json')) }, embeddingOnline, maibot: memory, config, allowlist, qqAllowlist: readJson(QQ_ALLOWLIST, { enabled: true, users: [] }), activeTask: activeTask ? taskView(activeTask, true) : null, tasks: tasks.slice(0, 12).map(task => taskView(task)), dm: readJson(DM_STATUS, { running: false }) };
}
function sendJson(res, status, body) { const data = Buffer.from(JSON.stringify(body)); res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Content-Length': data.length, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(data); }
async function readBody(req) { let size = 0; const chunks = []; for await (const chunk of req) { size += chunk.length; if (size > 1048576) throw new Error('Request too large'); chunks.push(chunk); } return chunks.length ? JSON.parse(Buffer.concat(chunks).toString('utf8')) : {}; }
function validateConfig(input, current) {
  const next = { ...current };
  if ('max_videos_per_run' in input) next.max_videos_per_run = int(input.max_videos_per_run, 1, 20, current.max_videos_per_run);
  if ('home_scrolls' in input) next.home_scrolls = int(input.home_scrolls, 1, 30, current.home_scrolls);
  if ('delay_min_seconds' in input) next.delay_min_seconds = num(input.delay_min_seconds, 1, 120, current.delay_min_seconds);
  if ('delay_max_seconds' in input) next.delay_max_seconds = num(input.delay_max_seconds, next.delay_min_seconds || 1, 180, current.delay_max_seconds);
  if (next.delay_max_seconds < next.delay_min_seconds) next.delay_max_seconds = next.delay_min_seconds;
  if ('headless' in input) next.headless = Boolean(input.headless);
  if ('description_only_if_no_subtitle' in input) next.description_only_if_no_subtitle = Boolean(input.description_only_if_no_subtitle);
  if ('memory_scope' in input) next.memory_scope = input.memory_scope === 'global' ? 'global' : 'chat';
  if ('chat_id' in input) next.chat_id = String(input.chat_id || '').trim().slice(0, 180);
  if ('summary_model' in input && ['deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-pro'].includes(input.summary_model)) next.summary_model = input.summary_model;
  if (Array.isArray(input.tag_filter_keywords)) next.tag_filter_keywords = input.tag_filter_keywords.map(value => String(value).trim()).filter(Boolean).slice(0, 60);
  if ('interest_profile' in input) next.interest_profile = String(input.interest_profile || '').trim().slice(0, 3000);
  if (Array.isArray(input.avoid_topics)) next.avoid_topics = input.avoid_topics.map(value => String(value).trim()).filter(Boolean).slice(0, 60);
  if (Array.isArray(input.excluded_tags)) next.excluded_tags = input.excluded_tags.map(value => String(value).trim()).filter(Boolean).slice(0, 40);
  if ('min_relevance_score' in input) next.min_relevance_score = num(input.min_relevance_score, 0, 100, current.min_relevance_score);
  if ('early_check_chars' in input) next.early_check_chars = int(input.early_check_chars, 500, 20000, current.early_check_chars);
  if ('early_min_score' in input) next.early_min_score = num(input.early_min_score, 0, 100, current.early_min_score);
  if ('triage_model' in input && ['deepseek-flash', 'deepseek-v4-flash', 'deepseek-v4-pro'].includes(input.triage_model)) next.triage_model = input.triage_model;
  if (input.bili_dm && typeof input.bili_dm === 'object') {
    const currentDm = next.bili_dm || {};
    const inputDm = input.bili_dm;
    next.bili_dm = { ...currentDm };
    if ('owner_uid' in inputDm && /^\d{3,30}$/.test(String(inputDm.owner_uid || ''))) next.bili_dm.owner_uid = String(inputDm.owner_uid);
    if ('poll_seconds' in inputDm) next.bili_dm.poll_seconds = int(inputDm.poll_seconds, 20, 3600, currentDm.poll_seconds || 60);
    if ('reply_to_owner_only' in inputDm) next.bili_dm.reply_to_owner_only = true;
    if ('agents_file' in inputDm) next.bili_dm.agents_file = String(inputDm.agents_file || '').trim().slice(0, 500);
    if ('persona_prompt' in inputDm) next.bili_dm.persona_prompt = String(inputDm.persona_prompt || '').trim().slice(0, 12000);
    if ('enabled' in inputDm) next.bili_dm.enabled = Boolean(inputDm.enabled);
  }
  return next;
}
function validateQQAllowlist(input, current) {
  const allowedPermissions = new Set(['links', 'search']);
  const seen = new Set();
  const users = (Array.isArray(input.users) ? input.users : []).map(item => ({
    qq: String(item.qq || '').trim(),
    label: String(item.label || '用户').trim().slice(0, 40) || '用户',
    permissions: (Array.isArray(item.permissions) ? item.permissions : [])
      .map(value => String(value).trim())
      .filter(value => allowedPermissions.has(value)),
  })).filter(item => {
    if (!/^\d{3,30}$/.test(item.qq) || seen.has(item.qq)) return false;
    seen.add(item.qq);
    return true;
  }).slice(0, 100);
  return { ...current, enabled: input.enabled !== false, users };
}

function validateAllowlist(input, current) {
  const botUid = String(current.bot_account_uid || ''); const seen = new Set();
  const accounts = (Array.isArray(input.accounts) ? input.accounts : []).map(item => ({ platform: 'bilibili', uid: String(item.uid || '').trim(), label: String(item.label || 'owner').trim().slice(0, 40) || 'owner' })).filter(item => { if (!/^\d{3,30}$/.test(item.uid) || item.uid === botUid || seen.has(item.uid)) return false; seen.add(item.uid); return true; }).slice(0, 30);
  return { ...current, enabled: true, mode: 'draft_only', allow_others: false, bot_account_uid: botUid, accounts };
}
function startEmbedding() { const out = fs.openSync(path.join(EMBED_DIR, 'service.stdout.log'), 'a'); const err = fs.openSync(path.join(EMBED_DIR, 'service.stderr.log'), 'a'); const child = spawn(process.execPath, ['server.mjs'], { cwd: EMBED_DIR, detached: true, windowsHide: true, env: { ...process.env, HF_ENDPOINT: 'https://hf-mirror.com', HF_HUB_DISABLE_TELEMETRY: '1' }, stdio: ['ignore', out, err] }); child.unref(); return { pid: child.pid }; }
function openPath(name) { const paths = { data: DATA, summaries: SUMMARIES, outputs: OUTPUTS, maibot: MAIBOT_ROOT, adminLogs: path.join(DIR, 'logs') }; if (!paths[name]) throw new Error('Unknown path'); fs.mkdirSync(paths[name], { recursive: true }); const child = spawn('explorer.exe', [paths[name]], { detached: true, stdio: 'ignore', windowsHide: false }); child.unref(); }
const MIME = { '.html': 'text/html; charset=utf-8', '.js': 'text/javascript; charset=utf-8', '.css': 'text/css; charset=utf-8', '.json': 'application/json; charset=utf-8', '.svg': 'image/svg+xml', '.png': 'image/png', '.ico': 'image/x-icon' };
async function staticFile(res, pathname) {
  if (pathname === '/favicon.ico') { res.writeHead(204, { 'Cache-Control': 'no-store' }); res.end(); return; }
  const candidate = pathname === '/' ? path.join(PUBLIC, 'index.html') : path.join(PUBLIC, pathname.replace(/^\/+/, '')); const resolved = path.resolve(candidate);
  if (!resolved.startsWith(path.resolve(PUBLIC))) return sendJson(res, 403, { error: 'forbidden' });
  try { const data = await fsp.readFile(resolved); res.writeHead(200, { 'Content-Type': MIME[path.extname(resolved).toLowerCase()] || 'application/octet-stream', 'Content-Length': data.length, 'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff' }); res.end(data); } catch { sendJson(res, 404, { error: 'not found' }); }
}
async function api(req, res, url) {
  const p = url.pathname;
  if (req.method === 'GET' && p === '/api/status') return sendJson(res, 200, await statusPayload());
  if (req.method === 'GET' && p === '/api/config') return sendJson(res, 200, readJson(CONFIG, {}));
  if (req.method === 'GET' && p === '/api/allowlist') return sendJson(res, 200, readJson(ALLOWLIST, { accounts: [] }));
  if (req.method === 'GET' && p.startsWith('/api/tasks/')) { const id = p.split('/').pop(); const task = tasks.find(item => item.id === id); return task ? sendJson(res, 200, taskView(task, true)) : sendJson(res, 404, { error: 'task not found' }); }
  if (req.method === 'POST' && p === '/api/config') { const current = readJson(CONFIG, {}); const next = validateConfig(await readBody(req), current); await writeJson(CONFIG, next); return sendJson(res, 200, { success: true, config: next }); }
  if (req.method === 'POST' && p === '/api/allowlist') { const current = readJson(ALLOWLIST, { accounts: [] }); const next = validateAllowlist(await readBody(req), current); await writeJson(ALLOWLIST, next); return sendJson(res, 200, { success: true, allowlist: next }); }
  if (req.method === 'GET' && p === '/api/qq-allowlist') return sendJson(res, 200, readJson(QQ_ALLOWLIST, { enabled: true, users: [] }));
  if (req.method === 'POST' && p === '/api/qq-allowlist') { const current = readJson(QQ_ALLOWLIST, { enabled: true, users: [] }); const next = validateQQAllowlist(await readBody(req), current); await writeJson(QQ_ALLOWLIST, next); return sendJson(res, 200, { success: true, qqAllowlist: next }); }
  if (req.method === 'POST' && p === '/api/actions/login') { const task = createTask('login', 'B站扫码登录', ['login']); return task ? sendJson(res, 202, { success: true, task }) : sendJson(res, 409, { error: '已有任务正在运行' }); }
  if (req.method === 'POST' && p === '/api/actions/status') { const task = createTask('status', 'B站与记忆检查', ['status']); return task ? sendJson(res, 202, { success: true, task }) : sendJson(res, 409, { error: '已有任务正在运行' }); }
  if (req.method === 'GET' && p === '/api/media/file') {
    const relative = String(url.searchParams.get('path') || '').replace(/\\/g, '/');
    const target = path.resolve(DATA, relative);
    const insideData = target === DATA || target.startsWith(DATA + path.sep);
    const extension = path.extname(target).toLowerCase();
    if (!insideData || !['.mp3', '.wav'].includes(extension) || !fs.existsSync(target)) return sendJson(res, 404, { error: 'audio not found' });
    const stat = fs.statSync(target);
    res.writeHead(200, { 'Content-Type': extension === '.mp3' ? 'audio/mpeg' : 'audio/wav', 'Content-Length': stat.size, 'Cache-Control': 'no-store' });
    fs.createReadStream(target).pipe(res);
    return;
  }
  if (req.method === 'POST' && p === '/api/actions/resolve') {
    const body = await readBody(req); const query = String(body.query || '').trim().slice(0, 100); const max = int(body.max, 1, 5, 1);
    if (!query) return sendJson(res, 400, { error: 'missing query' });
    const task = createTask('resolve', 'resolve BV search', ['resolve', '--query', query, '--max', String(max)]);
    return task ? sendJson(res, 202, { success: true, task }) : sendJson(res, 409, { error: 'task already running' });
  }
  if (req.method === 'POST' && p === '/api/actions/chats') { const task = createTask('chats', '读取聊天流', ['chats']); return task ? sendJson(res, 202, { success: true, task }) : sendJson(res, 409, { error: '已有任务正在运行' }); }
  if (req.method === 'POST' && p === '/api/actions/collect') {
    const body = await readBody(req); const config = readJson(CONFIG, {}); const mode = String(body.mode || 'home'); const max = int(body.max, 1, 20, config.max_videos_per_run || 5); const args = ['collect', '--max', String(max)]; let collectLabel = `\u91c7\u96c6 ${mode}`; if (body.import !== false) args.push('--import'); if (body.force === true) args.push('--force'); if (body.ignore_preference === true) args.push('--ignore-preference'); if (body.review_level === 'detailed') args.push('--review-level', 'detailed'); if (body.publish_comment === true) args.push('--publish-comment'); if (body.publish_comment === false || body.suppress_comment === true) args.push('--suppress-comment');
    if (mode === 'section') { const value = String(body.value || '').trim(); if (!/^https?:\/\/(www\.)?bilibili\.com\//i.test(value)) return sendJson(res, 400, { error: '分区链接必须是 bilibili.com 地址' }); args.push('--section-url', value); }
    else if (mode === 'tag') { const value = String(body.value || '').trim().slice(0, 60); if (!value) return sendJson(res, 400, { error: '\u8bf7\u8f93\u5165\u6807\u7b7e\u6216\u5173\u952e\u8bcd' }); collectLabel = `\u91c7\u96c6 ${value} \u00b7 \u76ee\u6807 ${max} \u4e2a`; args.push('--tag', value, '--search-keyword', value); }
    else if (mode === 'direct') { const values = Array.isArray(body.urls) ? body.urls : String(body.urls || '').split(/\s+/); for (const value of values.map(item => String(item).trim()).filter(Boolean).slice(0, max)) { const bvid = value.match(/BV[0-9A-Za-z]{10,}/)?.[0]; if (bvid) { const page = Number.parseInt(value.match(/[?&]p=(\d+)/)?.[1] || '1', 10); args.push('--url', `https://www.bilibili.com/video/${bvid}/` + (page > 1 ? `?p=${page}` : '')); } else if (/^https?:\/\/(?:b23\.tv|bili2233\.cn)\//i.test(value)) args.push('--url', value); } if (!args.includes('--url')) return sendJson(res, 400, { error: '没有有效的 BV 链接' }); }
    else if (mode !== 'home') return sendJson(res, 400, { error: '未知采集模式' });
    const task = createTask('collect', collectLabel, args);
    return task ? sendJson(res, 202, { success: true, task }) : sendJson(res, 409, { error: '\u5df2\u6709\u4efb\u52a1\u6b63\u5728\u8fd0\u884c' });
  }
  if (req.method === 'POST' && p === '/api/actions/stop') { if (!activeTask?.child) return sendJson(res, 409, { error: '没有正在运行的任务' }); activeTask.child.kill(); return sendJson(res, 200, { success: true }); }
  if (req.method === 'POST' && p === '/api/actions/embedding/start') { if (await tcpCheck('127.0.0.1', 11435, 400)) return sendJson(res, 200, { success: true, alreadyRunning: true }); return sendJson(res, 200, { success: true, ...startEmbedding() }); }
  if (req.method === 'POST' && p === '/api/actions/memory/rebuild') { const { cookie } = await authWebUi(); const response = await fetch(WEBUI + '/api/webui/memory/runtime/vectors/rebuild', { method: 'POST', headers: { 'Content-Type': 'application/json', Cookie: cookie }, body: JSON.stringify({ dry_run: false, batch_size: 64, include_relations: true }), signal: AbortSignal.timeout(600000) }); const data = await response.json(); return sendJson(res, response.status, data); }
  if (req.method === 'GET' && p === '/api/dm/status') return sendJson(res, 200, { status: readJson(DM_STATUS, { running: false }), config: readJson(CONFIG, {}).bili_dm || {} });
  if (req.method === 'GET' && p === '/api/dm/tasks') { const filter = ['all', 'picked', 'skipped', 'delivered'].includes(url.searchParams.get('filter')) ? url.searchParams.get('filter') : 'all'; try { const tasks = await runServiceJson(['--tasks-json', '--filter', filter], 30000); return sendJson(res, 200, { tasks }); } catch (error) { return sendJson(res, 500, { error: String(error.message || error) }); } }
  if (req.method === 'GET' && p === '/api/dm/emotes') { try { const emotes = await runServiceJson(['--emotes-json'], 30000); return sendJson(res, 200, { emotes }); } catch (error) { return sendJson(res, 500, { error: String(error.message || error) }); } }
  if (req.method === 'POST' && p === '/api/dm/check') { try { const report = await runServiceJson(['--check'], 120000); return sendJson(res, 200, { success: true, report }); } catch (error) { return sendJson(res, 500, { error: String(error.message || error) }); } }
  if (req.method === 'POST' && p === '/api/actions/dm/restart') {
    try {
      const rawPid = fs.existsSync(DM_PID) ? fs.readFileSync(DM_PID, 'utf8').trim() : '';
      const oldPid = Number.parseInt(rawPid, 10);
      if (Number.isFinite(oldPid) && oldPid > 0) {
        try { process.kill(oldPid); } catch {}
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      const child = spawn(PYTHON, [DM_SERVICE, '--run'], { cwd: ROOT, detached: true, windowsHide: true, stdio: 'ignore', env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
      child.unref();
      return sendJson(res, 200, { success: true, pid: child.pid });
    } catch (error) { return sendJson(res, 500, { error: String(error.message || error) }); }
  }
  if (req.method === 'POST' && p === '/api/actions/dm/start') { const child = spawn(PYTHON, [DM_SERVICE, '--run'], { cwd: ROOT, detached: true, windowsHide: true, stdio: 'ignore', env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } }); child.unref(); return sendJson(res, 200, { success: true, pid: child.pid }); }
  if (req.method === 'POST' && p === '/api/actions/qq/restart') {
    try {
      const rawPid = fs.existsSync(QQ_PID) ? fs.readFileSync(QQ_PID, 'utf8').trim() : '';
      const oldPid = Number.parseInt(rawPid, 10);
      if (Number.isFinite(oldPid) && oldPid > 0) {
        try { process.kill(oldPid); } catch {}
        await new Promise(resolve => setTimeout(resolve, 1500));
      }
      const child = spawn(PYTHON, [QQ_WATCHER], { cwd: ROOT, detached: true, windowsHide: true, stdio: 'ignore', env: { ...process.env, PYTHONIOENCODING: 'utf-8', PYTHONUTF8: '1' } });
      child.unref();
      await fsp.writeFile(QQ_PID, String(child.pid), 'utf8');
      return sendJson(res, 200, { success: true, pid: child.pid });
    } catch (error) { return sendJson(res, 500, { error: String(error.message || error) }); }
  }
  if (req.method === 'GET' && p === '/api/dm/persona') { let persona = ''; try { persona = fs.readFileSync(DM_PERSONA, 'utf8'); } catch {} return sendJson(res, 200, { persona }); }
  if (req.method === 'POST' && p === '/api/dm/persona') { const body = await readBody(req); const persona = String(body.persona || '').trim().slice(0, 20000); await fsp.writeFile(DM_PERSONA, persona + (persona ? '\n' : ''), 'utf8'); return sendJson(res, 200, { success: true, persona }); }
  if (req.method === 'POST' && p === '/api/actions/install-shortcuts') {
    const sources = [
      [path.join(OUTPUTS, '\u91cd\u542fB\u7ad9AI\u670d\u52a1.lnk'), '\u91cd\u542fB\u7ad9AI\u670d\u52a1.lnk'],
      [path.join(OUTPUTS, 'B\u7ad9AI\u7ba1\u7406\u9762\u677f.lnk'), 'B\u7ad9AI\u7ba1\u7406\u9762\u677f.lnk'],
    ];
    const copied = [];
    for (const desktop of DESKTOPS) {
      for (const [source, name] of sources) {
        if (!fs.existsSync(source)) continue;
        const destination = path.join(desktop, name);
        fs.copyFileSync(source, destination);
        copied.push(destination);
      }
    }
    return sendJson(res, 200, { success: copied.length > 0, copied, desktops: DESKTOPS });
  }
  if (req.method === 'POST' && p === '/api/actions/open-path') { openPath(String((await readBody(req)).name || '')); return sendJson(res, 200, { success: true }); }
  if (req.method === 'GET' && p === '/api/qq/status') {
    const config = readJson(CONFIG, {});
    const policy = config.qq_link_policy || {};
    const allowlist = readJson(QQ_ALLOWLIST, { enabled: true, users: [] });
    const rawPid = fs.existsSync(QQ_PID) ? fs.readFileSync(QQ_PID, 'utf8').trim() : '';
    const pid = Number.parseInt(rawPid, 10);
    const [watcherPort, napcatPort] = await Promise.all([
      tcpCheck('127.0.0.1', 8766, 600),
      tcpCheck('127.0.0.1', 7998, 600)
    ]);
    return sendJson(res, 200, {
      ok: watcherPort && napcatPort,
      process: { pid: Number.isFinite(pid) ? pid : null, alive: processAlive(pid) },
      ports: { watcher: watcherPort, napcatOneBot: napcatPort },
      policy: {
        enabled: Boolean(policy.enabled),
        allowedGroups: Array.isArray(policy.allowed_groups) ? policy.allowed_groups.map(String) : [],
        ownerConfigured: Boolean(policy.owner_qq || process.env.BILIMEMORY_OWNER_QQ),
        allowAllGroupMembers: Boolean(policy.allow_all_group_members),
        searchEnabled: Boolean(policy.search_enabled),
        searchAllowAllGroupMembers: Boolean(policy.search_allow_all_group_members),
        linkOnlyMessages: Boolean(policy.require_link_only_message)
      },
      allowlist: { enabled: allowlist.enabled !== false, users: (Array.isArray(allowlist.users) ? allowlist.users : []).map(user => ({ ...user, permissions: (Array.isArray(user.permissions) ? user.permissions : []).filter(permission => permission === 'links' || permission === 'search') })) }
    });
  }
  return sendJson(res, 404, { error: 'API not found' });
}
const server = http.createServer(async (req, res) => { try { const url = new URL(req.url, `http://${req.headers.host || HOST}`); if (url.pathname.startsWith('/api/')) return await api(req, res, url); return await staticFile(res, url.pathname); } catch (error) { console.error(error); if (!res.headersSent) sendJson(res, 500, { error: String(error.message || error) }); else res.end(); } });
async function freePort(start) { for (let candidate = start; candidate < start + 30; candidate++) if (!(await tcpCheck(HOST, candidate, 250))) return candidate; throw new Error('No free admin port'); }
if (await tcpCheck(HOST, START_PORT, 400)) {
  try {
    const existing = await fetch(`http://${HOST}:${START_PORT}/api/status`, { signal: AbortSignal.timeout(2500) });
    if (existing.ok && String(existing.headers.get('content-type') || '').includes('application/json')) {
      console.log(`Bilibili Memory Admin already running: http://${HOST}:${START_PORT}`);
      if (process.env.ADMIN_NO_OPEN !== '1') {
        const child = spawn('cmd.exe', ['/c', 'start', '', `http://${HOST}:${START_PORT}`], { detached: true, stdio: 'ignore', windowsHide: false });
        child.unref();
      }
      process.exit(0);
    }
  } catch {}
}
port = await freePort(START_PORT);
server.listen(port, HOST, () => { console.log(`Bilibili Memory Admin: http://${HOST}:${port}`); console.log(`Project: ${ROOT}`); if (process.env.ADMIN_NO_OPEN !== '1') setTimeout(() => { const child = spawn('cmd.exe', ['/c', 'start', '', `http://${HOST}:${port}`], { detached: true, stdio: 'ignore', windowsHide: false }); child.unref(); }, 350); });
process.on('SIGINT', () => { activeTask?.child?.kill(); server.close(() => process.exit(0)); });
