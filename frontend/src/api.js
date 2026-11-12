// ---- call log: every request the browser makes to this app, for the API calls window ----
const listeners = new Set();
let appCalls = [];
let seq = 0;

export const appLog = {
  get: () => appCalls,
  subscribe(fn) {
    listeners.add(fn);
    return () => listeners.delete(fn);
  },
  clear() {
    appCalls = [];
    listeners.forEach((fn) => fn());
  },
  record(entry) {
    appCalls = [...appCalls.slice(-499), { id: `app-${++seq}`, source: "app", at: new Date().toISOString(), ...entry }];
    listeners.forEach((fn) => fn());
  },
};

async function tracked(path, init = {}) {
  const method = init.method || "GET";
  const started = performance.now();
  const ms = () => Math.round(performance.now() - started);
  try {
    const res = await fetch(path, init);
    appLog.record({ method, url: path, status: res.status, ms: ms(), body: init.body ? summarize(init.body) : undefined });
    return res;
  } catch (e) {
    appLog.record({ method, url: path, status: null, ms: ms(), error: e.message });
    throw e;
  }
}

// Keep the logged request body short: the plan and result objects can be large.
function summarize(body) {
  try {
    const obj = JSON.parse(body);
    const out = {};
    for (const [k, v] of Object.entries(obj)) {
      if (k === "plan") out.plan = `${v.documents?.length ?? 0} document codes, ${v.observations?.length ?? 0} result codes`;
      else if (k === "result") out.result = `${v.rows?.length ?? 0} rows`;
      else if (typeof v === "string" && v.length > 80) out[k] = `${v.slice(0, 80)}…`;
      else out[k] = v;
    }
    return JSON.stringify(out);
  } catch {
    return undefined;
  }
}

async function call(path, options = {}) {
  const res = await tracked(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

export const api = {
  // Background packet download: start, poll per-file status, then fetch the zip.
  packetStart: (plan, result) =>
    call("/api/packet/jobs", { method: "POST", body: JSON.stringify({ plan, result }) }),
  packetStatus: async (job) => {
    // Polled every second, so not logged in the trace.
    const res = await fetch(`/api/packet/jobs/${job}`);
    const body = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
    return body;
  },
  packetSave: (job) => {
    const url = `/api/packet/jobs/${job}/zip`;
    appLog.record({ method: "GET", url, status: null, ms: null, error: undefined, note: "browser download" });
    const a = Object.assign(document.createElement("a"), { href: url, download: "" });
    document.body.appendChild(a);
    a.click();
    a.remove();
  },

  // Cerner calls logged by the backend after id `after`. Not itself logged.
  fhirCalls: async (after) => {
    const res = await fetch(`/api/calls?after=${after}`);
    if (!res.ok) throw new Error(`HTTP ${res.status}`);
    return res.json();
  },

  health: () => call("/api/health"),
  catalog: () => call("/api/catalog"),
  prompt: (service_note, model) =>
    call("/api/prompt", { method: "POST", body: JSON.stringify({ service_note, use_llm: true, model }) }),
  models: () => call("/api/models"),
  encounters: (patient, start, end, ids = []) => {
    const q = new URLSearchParams({ patient });
    if (start) q.set("start", start);
    if (end) q.set("end", end);
    if (ids.length) q.set("ids", ids.join(","));
    return call(`/api/encounters?${q}`);
  },
  encounterDocs: (patient, encounterId) =>
    call(`/api/encounters/${encodeURIComponent(encounterId)}/documents?${new URLSearchParams({ patient })}`),
  plan: (service_note, use_llm, model) =>
    call("/api/plan", { method: "POST", body: JSON.stringify({ service_note, use_llm, model }) }),
  patients: (name, birthdate) => {
    const q = new URLSearchParams({ name });
    if (birthdate) q.set("birthdate", birthdate);
    return call(`/api/patients?${q}`);
  },
  run: (payload) => call("/api/run", { method: "POST", body: JSON.stringify(payload) }),

  // Stream /api/run/stream (NDJSON) and call onEvent for each progress event.
  runStream: async (payload, onEvent, signal) => {
    const res = await tracked("/api/run/stream", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
      signal,
    });
    if (!res.ok || !res.body) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const reader = res.body.getReader();
    const decoder = new TextDecoder();
    let buffer = "";
    for (;;) {
      const { value, done } = await reader.read();
      buffer += decoder.decode(value || new Uint8Array(), { stream: !done });
      const lines = buffer.split("\n");
      buffer = lines.pop();
      for (const line of lines) {
        if (!line.trim()) continue;
        const event = JSON.parse(line);
        if (event.type === "error") throw new Error(event.detail);
        onEvent(event);
      }
      if (done) break;
    }
  },

  binaryUrl: (doc) => {
    const q = new URLSearchParams({ url: doc.binaryUrl, name: doc.file || doc.id });
    if (doc.contentType) q.set("type", doc.contentType);
    return `/api/binary?${q}`;
  },

  // POST the plan + run result, save the streamed zip, return the failed-download count.
  packet: async (plan, result) => {
    const res = await tracked("/api/packet", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ plan, result }),
    });
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    const name = /filename="([^"]+)"/.exec(res.headers.get("Content-Disposition") || "")?.[1] || "packet.zip";
    const url = URL.createObjectURL(await res.blob());
    const a = Object.assign(document.createElement("a"), { href: url, download: name });
    document.body.appendChild(a);
    a.click();
    a.remove();
    setTimeout(() => URL.revokeObjectURL(url), 1000);
    return Number(res.headers.get("X-Download-Errors") || 0);
  },
};
