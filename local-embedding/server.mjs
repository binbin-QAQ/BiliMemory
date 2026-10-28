import http from 'node:http';
import path from 'node:path';
import { pipeline, env } from '@huggingface/transformers';

const HOST = process.env.EMBEDDING_HOST || '127.0.0.1';
const PORT = Number(process.env.EMBEDDING_PORT || 11435);
const MODEL = process.env.EMBEDDING_MODEL || 'Xenova/bge-small-zh-v1.5';
const servedModel = process.env.EMBEDDING_SERVED_NAME || 'bge-small-zh-v1.5';

env.cacheDir = path.join(process.cwd(), '.cache');
env.allowRemoteModels = true;
env.remoteHost = process.env.HF_ENDPOINT || 'https://huggingface.co';

let extractor;
let queue = Promise.resolve();
function exclusive(task) {
  const run = queue.then(task, task);
  queue = run.catch(() => {});
  return run;
}

function json(res, status, body) {
  const data = Buffer.from(JSON.stringify(body));
  res.writeHead(status, {'Content-Type':'application/json; charset=utf-8','Content-Length':data.length});
  res.end(data);
}

async function readJson(req) {
  let size = 0; const chunks = [];
  for await (const chunk of req) {
    size += chunk.length;
    if (size > 10 * 1024 * 1024) throw new Error('request too large');
    chunks.push(chunk);
  }
  return JSON.parse(Buffer.concat(chunks).toString('utf8') || '{}');
}

function normalizeInput(input) {
  if (typeof input === 'string') return [input];
  if (Array.isArray(input) && input.every(x => typeof x === 'string')) return input;
  throw new Error('input must be a string or an array of strings');
}

async function embed(input) {
  const texts = normalizeInput(input);
  if (!texts.length) return [];
  return exclusive(async () => {
    const output = await extractor(texts, { pooling: 'mean', normalize: true });
    const values = output.tolist();
    return Array.isArray(values[0]) ? values : [values];
  });
}

const server = http.createServer(async (req, res) => {
  try {
    const url = new URL(req.url, `http://${req.headers.host || HOST}`);
    if (req.method === 'GET' && (url.pathname === '/health' || url.pathname === '/')) {
      return json(res, 200, {status:'ok', model:servedModel, loaded:Boolean(extractor)});
    }
    if (req.method === 'GET' && (url.pathname === '/v1/models' || url.pathname === '/models')) {
      return json(res, 200, {object:'list', data:[{id:servedModel, object:'model', owned_by:'local'}]});
    }
    if (req.method === 'POST' && (url.pathname === '/v1/embeddings' || url.pathname === '/embeddings')) {
      const body = await readJson(req);
      const vectors = await embed(body.input);
      return json(res, 200, {
        object: 'list',
        data: vectors.map((embedding, index) => ({object:'embedding', index, embedding})),
        model: body.model || servedModel,
        usage: {prompt_tokens:0, total_tokens:0}
      });
    }
    return json(res, 404, {error:{message:'not found', type:'invalid_request_error'}});
  } catch (error) {
    console.error(error);
    return json(res, 500, {error:{message:String(error?.message || error), type:'server_error'}});
  }
});

console.log(`Loading embedding model ${MODEL}...`);
extractor = await pipeline('feature-extraction', MODEL, { dtype: 'q8' });
console.log(`Embedding model ready: ${servedModel}`);
server.listen(PORT, HOST, () => console.log(`Local embeddings listening on http://${HOST}:${PORT}/v1`));
