async function call(path, options = {}) {
  const res = await fetch(path, { headers: { "Content-Type": "application/json" }, ...options });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.detail || `HTTP ${res.status}`);
  return body;
}

export const api = {
  health: () => call("/api/health"),
  catalog: () => call("/api/catalog"),
  plan: (service_note, use_llm) =>
    call("/api/plan", { method: "POST", body: JSON.stringify({ service_note, use_llm }) }),
  patients: (name, birthdate) => {
    const q = new URLSearchParams({ name });
    if (birthdate) q.set("birthdate", birthdate);
    return call(`/api/patients?${q}`);
  },
  run: (payload) => call("/api/run", { method: "POST", body: JSON.stringify(payload) }),

  binaryUrl: (doc) => {
    const q = new URLSearchParams({ url: doc.binaryUrl, name: doc.file || doc.id });
    if (doc.contentType) q.set("type", doc.contentType);
    return `/api/binary?${q}`;
  },

  // POST the plan + run result, save the streamed zip, return the failed-download count.
  packet: async (plan, result) => {
    const res = await fetch("/api/packet", {
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
