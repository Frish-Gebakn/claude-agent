// MyDeskAI "Send logs" receiver.
//
//   POST /upload   from the app: {"app", "version", "gz": base64(gzip(report))}
//                  -> {"ok": true, "id": "K7Q2XM"}
//   GET  /list     developer only (Authorization: Bearer READ_TOKEN)
//   GET  /get?key= developer only: the report as text
//   POST /delete?key=  developer only, after the report has been saved locally
//
// Anyone can upload - the address is in the open-source app - so an upload is
// capped in size and in how many one address may send an hour. Nobody can read
// without READ_TOKEN, a Worker secret that is never in the source.

const MAX_BODY = 3 * 1024 * 1024;        // the base64 gzip as sent
const MAX_TEXT = 8 * 1024 * 1024;        // the report once unpacked
const PER_HOUR = 12;                     // uploads per IP address per hour
const KEEP_SECONDS = 90 * 24 * 3600;     // a report nobody fetched expires
const ID_CHARS = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789";   // no 0/O, 1/I

const json = (data, status = 200) =>
  new Response(JSON.stringify(data), {
    status,
    headers: { "content-type": "application/json; charset=utf-8", "cache-control": "no-store" },
  });

function newId() {
  const b = crypto.getRandomValues(new Uint8Array(6));
  return [...b].map((x) => ID_CHARS[x % ID_CHARS.length]).join("");
}

function stamp(d = new Date()) {
  return d.toISOString().replace(/[-:]/g, "").replace("T", "-").slice(0, 15);   // 20261125-143005
}

async function sameText(a, b) {
  // Compare digests so the time taken does not depend on where they differ.
  const enc = new TextEncoder();
  const [x, y] = await Promise.all([
    crypto.subtle.digest("SHA-256", enc.encode(a)),
    crypto.subtle.digest("SHA-256", enc.encode(b)),
  ]);
  const p = new Uint8Array(x), q = new Uint8Array(y);
  let diff = 0;
  for (let i = 0; i < p.length; i++) diff |= p[i] ^ q[i];
  return diff === 0;
}

async function authorised(req, env) {
  const h = req.headers.get("authorization") || "";
  const m = h.match(/^Bearer\s+(.+)$/i);
  // trim(): a secret piped in from a Windows shell can arrive with "\r\n" on it.
  const want = (env.READ_TOKEN || "").trim();
  return !!(want && m && (await sameText(m[1].trim(), want)));
}

async function gunzip(bytes) {
  const stream = new Blob([bytes]).stream().pipeThrough(new DecompressionStream("gzip"));
  const reader = stream.getReader();
  const parts = [];
  let size = 0;
  for (;;) {
    const { done, value } = await reader.read();
    if (done) break;
    size += value.length;
    if (size > MAX_TEXT) { await reader.cancel(); throw new Error("too large"); }
    parts.push(value);
  }
  const out = new Uint8Array(size);
  let at = 0;
  for (const p of parts) { out.set(p, at); at += p.length; }
  return new TextDecoder("utf-8", { fatal: false }).decode(out);
}

async function overLimit(env, ip) {
  const key = `rl:${ip}:${Math.floor(Date.now() / 3600000)}`;
  const n = parseInt((await env.LOGS.get(key)) || "0", 10);
  if (n >= PER_HOUR) return true;
  await env.LOGS.put(key, String(n + 1), { expirationTtl: 3700 });
  return false;
}

async function upload(req, env) {
  const len = parseInt(req.headers.get("content-length") || "0", 10);
  if (len > MAX_BODY) return json({ ok: false, error: "the log is too large" }, 413);
  const raw = await req.arrayBuffer();
  if (raw.byteLength > MAX_BODY) return json({ ok: false, error: "the log is too large" }, 413);

  let body;
  try { body = JSON.parse(new TextDecoder().decode(raw)); }
  catch { return json({ ok: false, error: "bad request" }, 400); }
  if (!body || typeof body.gz !== "string" || !body.gz) return json({ ok: false, error: "bad request" }, 400);

  const ip = req.headers.get("cf-connecting-ip") || "unknown";
  if (await overLimit(env, ip)) return json({ ok: false, error: "too many logs from here this hour, try later" }, 429);

  let text;
  try {
    const bin = Uint8Array.from(atob(body.gz), (c) => c.charCodeAt(0));
    text = await gunzip(bin);
  } catch (e) {
    return json({ ok: false, error: e.message === "too large" ? "the log is too large" : "could not unpack the log" }, 400);
  }

  const id = newId();
  const key = `log:${stamp()}-${id}`;
  const meta = {
    id,
    app: String(body.app || "").slice(0, 40),
    version: String(body.version || "").slice(0, 20),
    country: (req.cf && req.cf.country) || "",
    size: text.length,
    at: new Date().toISOString(),
  };
  await env.LOGS.put(key, text, { metadata: meta, expirationTtl: KEEP_SECONDS });
  return json({ ok: true, id });
}

export default {
  async fetch(req, env) {
    const url = new URL(req.url);
    const path = url.pathname.replace(/\/+$/, "") || "/";

    if (path === "/upload" && req.method === "POST") return upload(req, env);
    if (path === "/" && req.method === "GET") return new Response("MyDeskAI log receiver\n");

    if (["/list", "/get", "/delete"].includes(path)) {
      if (!(await authorised(req, env))) return json({ ok: false, error: "not allowed" }, 401);
      if (path === "/list" && req.method === "GET") {
        const r = await env.LOGS.list({ prefix: "log:", cursor: url.searchParams.get("cursor") || undefined });
        return json({ ok: true, keys: r.keys.map((k) => ({ key: k.name, ...(k.metadata || {}) })),
                      cursor: r.list_complete ? null : r.cursor });
      }
      const key = url.searchParams.get("key") || "";
      if (!key.startsWith("log:")) return json({ ok: false, error: "bad key" }, 400);
      if (path === "/get" && req.method === "GET") {
        const text = await env.LOGS.get(key);
        if (text === null) return json({ ok: false, error: "not found" }, 404);
        return new Response(text, { headers: { "content-type": "text/plain; charset=utf-8", "cache-control": "no-store" } });
      }
      if (path === "/delete" && req.method === "POST") {
        await env.LOGS.delete(key);
        return json({ ok: true });
      }
    }
    return json({ ok: false, error: "not found" }, 404);
  },
};
