/**
 * The contribution collector.
 *
 * Accepts one callwitness.contribution.v1 document, validates it against the
 * same key list the client enforces, and writes it to KV. Nothing else.
 *
 * The rule that shapes every line: this endpoint must never become a place
 * where more arrives than was promised. The client audits the payload before
 * sending; this audits it again on arrival, because a client is something a
 * person can modify and a promise made to contributors is not.
 *
 * Unknown keys are rejected, not stripped. Stripping would let a future client
 * quietly send a field nobody agreed to and have it silently disappear --
 * which looks like safety and is actually a silent failure. A rejection is
 * visible to whoever shipped the bug.
 *
 * What is never stored: the contributor's IP, their user agent beyond the
 * version, any header, and any timing that could be correlated back. Cloudflare
 * hands us CF-Connecting-IP on every request; we do not read it, and there is
 * no line below that could.
 */

const SCHEMA = "callwitness.contribution.v1";
const MAX_BYTES = 262144; // 256 KiB. A real payload is a few KB; this is slack.

const PAYLOAD_KEYS = ["schema", "install", "version", "platform", "python",
                      "window", "servers"];
const SERVER_KEYS = ["package", "declared_bytes", "tool_count", "tools"];
const TOOL_KEYS = ["tool", "calls", "errors", "returned_bytes",
                   "argument_bytes", "latency_ms"];

const UUID = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
const PLATFORMS = ["windows", "macos", "linux"];

/** Keys present on `object` that `allowed` does not list. */
function extraKeys(object, allowed, path) {
  if (object === null || typeof object !== "object" || Array.isArray(object)) {
    return [`${path} is not an object`];
  }
  return Object.keys(object)
    .filter((key) => !allowed.includes(key))
    .map((key) => `${path}.${key}`);
}

function isCount(value) {
  return Number.isInteger(value) && value >= 0 && value <= 1e12;
}

/** Every reason this document is not acceptable. All of them, not the first. */
function problems(payload) {
  const found = [];
  found.push(...extraKeys(payload, PAYLOAD_KEYS, "payload"));
  if (found.length) return found;

  if (payload.schema !== SCHEMA) found.push(`schema is not ${SCHEMA}`);
  if (typeof payload.install !== "string" || !UUID.test(payload.install)) {
    // A non-UUID install id would mean either a modified client or an attempt
    // to key contributions on something identifying. Neither is stored.
    found.push("install is not a uuid");
  }
  if (typeof payload.version !== "string" || payload.version.length > 32) {
    found.push("version is not a short string");
  }
  if (!PLATFORMS.includes(payload.platform)) found.push("platform is unknown");
  if (typeof payload.python !== "string" || payload.python.length > 8) {
    found.push("python is not a short string");
  }

  const window = payload.window;
  if (!window || typeof window.from !== "string" || typeof window.to !== "string") {
    found.push("window is malformed");
  }

  if (!Array.isArray(payload.servers)) {
    found.push("servers is not an array");
    return found;
  }
  if (payload.servers.length > 500) found.push("too many servers");

  for (const [i, server] of payload.servers.entries()) {
    found.push(...extraKeys(server, SERVER_KEYS, `servers[${i}]`));
    if (typeof server.package !== "string" || server.package.length > 200) {
      found.push(`servers[${i}].package is not a short string`);
    }
    if (!isCount(server.declared_bytes) || !isCount(server.tool_count)) {
      found.push(`servers[${i}] has a bad count`);
    }
    if (!Array.isArray(server.tools)) {
      found.push(`servers[${i}].tools is not an array`);
      continue;
    }
    if (server.tools.length > 500) found.push(`servers[${i}] has too many tools`);

    for (const [j, tool] of server.tools.entries()) {
      const at = `servers[${i}].tools[${j}]`;
      found.push(...extraKeys(tool, TOOL_KEYS, at));
      if (typeof tool.tool !== "string" || tool.tool.length > 200) {
        found.push(`${at}.tool is not a short string`);
      }
      if (!isCount(tool.calls) || !isCount(tool.errors)) {
        found.push(`${at} has a bad count`);
      }
      for (const group of ["returned_bytes", "argument_bytes", "latency_ms"]) {
        const stats = tool[group];
        if (!stats || typeof stats !== "object") {
          found.push(`${at}.${group} is missing`);
          continue;
        }
        for (const [name, value] of Object.entries(stats)) {
          // Only numbers live here. A string in a statistics object is the
          // shape a leak would take.
          if (!isCount(value)) found.push(`${at}.${group}.${name} is not a count`);
        }
      }
    }
  }
  return found;
}

function json(status, body) {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "content-type": "application/json" },
  });
}

export default {
  async fetch(request, env) {
    const url = new URL(request.url);

    if (request.method === "GET" && url.pathname === "/") {
      // Says what this is, for anyone who finds the endpoint in the source.
      return json(200, {
        service: "callwitness contribution collector",
        accepts: SCHEMA,
        method: "POST /v1/contributions",
        stores: "the posted document, unchanged, and nothing else",
        never_stores: ["ip address", "headers", "user agent"],
        source: "https://github.com/AditiChaudharyy14/callwitness",
        spec: "https://github.com/AditiChaudharyy14/callwitness/blob/main/docs/CONTRIBUTE.md",
      });
    }

    if (url.pathname !== "/v1/contributions") return json(404, { error: "not found" });
    if (request.method !== "POST") return json(405, { error: "POST only" });

    const declared = Number(request.headers.get("content-length") || 0);
    if (declared > MAX_BYTES) return json(413, { error: "too large" });

    let raw;
    try {
      raw = await request.text();
    } catch (_) {
      return json(400, { error: "unreadable body" });
    }
    if (raw.length > MAX_BYTES) return json(413, { error: "too large" });

    let payload;
    try {
      payload = JSON.parse(raw);
    } catch (_) {
      return json(400, { error: "not json" });
    }

    const bad = problems(payload);
    if (bad.length) {
      // Named, not summarised: whoever shipped the client that sent this needs
      // to know exactly which field was refused.
      return json(422, { error: "rejected", problems: bad.slice(0, 20) });
    }

    // Keyed by install id so a removal request can be honoured, and by day so
    // the store stays browsable. The random suffix keeps two sends on one day
    // from overwriting each other.
    // Workers KV rather than R2: R2 asks for a payment method, KV does not, and
    // the free tier's 1,000 writes a day is far more than a contribution stream
    // will produce for a long time. Move to R2 when that stops being true.
    const day = new Date().toISOString().slice(0, 10);
    const key = `raw/${day}/${payload.install}/${crypto.randomUUID()}`;
    try {
      await env.CONTRIBUTIONS.put(key, raw, {
        // Listed without reading the value, so a removal request can find every
        // key for one install without loading anyone's data.
        metadata: { install: payload.install, day, bytes: raw.length },
      });
    } catch (_) {
      return json(503, { error: "could not store it; nothing was kept" });
    }

    return json(202, { stored: true, bytes: raw.length });
  },
};