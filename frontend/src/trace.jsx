import React, { useCallback, useEffect, useRef, useState } from "react";
import { api, appLog } from "./api.js";

const MAX_ENTRIES = 1000;

/**
 * The API trace for the current request: the browser's calls to this app (from api.js)
 * plus the Cerner calls the backend made (polled from /api/calls while `active`).
 */
export function useTrace(active) {
  const [appCalls, setAppCalls] = useState(appLog.get());
  const [fhirCalls, setFhirCalls] = useState([]);
  const cursor = useRef(null); // last /api/calls id seen
  const clearedAt = useRef(Date.now());

  useEffect(() => appLog.subscribe(() => setAppCalls(appLog.get())), []);

  const poll = useCallback(async () => {
    try {
      if (cursor.current === null) {
        // Skip whatever the server logged before this page opened.
        cursor.current = (await api.fhirCalls(0)).last;
        return;
      }
      const { calls, last } = await api.fhirCalls(cursor.current);
      cursor.current = last;
      const fresh = calls
        .filter((c) => Date.parse(c.at) >= clearedAt.current - 50)
        .map((c) => ({ ...c, id: `fhir-${c.id}`, source: "cerner" }));
      if (fresh.length) setFhirCalls((prev) => [...prev, ...fresh].slice(-MAX_ENTRIES));
    } catch {
      /* the trace is best-effort; never break the page over it */
    }
  }, []);

  useEffect(() => {
    poll();
  }, [poll]);

  useEffect(() => {
    if (active) {
      const timer = setInterval(poll, 1000);
      return () => clearInterval(timer);
    }
    // One last read once the request finishes, to catch its final calls.
    const timer = setTimeout(poll, 800);
    return () => clearTimeout(timer);
  }, [active, poll]);

  const clear = useCallback(() => {
    clearedAt.current = Date.now();
    appLog.clear();
    setFhirCalls([]);
  }, []);

  const entries = [...appCalls, ...fhirCalls].sort((a, b) => (a.at < b.at ? 1 : a.at > b.at ? -1 : 0));
  // For requests made outside the main flow (e.g. loading visits): read the server log now.
  const refresh = useCallback(() => setTimeout(poll, 300), [poll]);
  return { entries, clear, refresh };
}

function time(iso) {
  const d = new Date(iso);
  const pad = (n, w = 2) => String(n).padStart(w, "0");
  return `${pad(d.getHours())}:${pad(d.getMinutes())}:${pad(d.getSeconds())}.${pad(d.getMilliseconds(), 3)}`;
}

function isError(e) {
  return !e.note && (e.status == null || e.status >= 400);
}

function statusClass(e) {
  const { status } = e;
  if (e.note) return "";
  if (status == null) return "bad";
  if (status >= 400) return "bad";
  if (status >= 300) return "warn";
  return "ok";
}

export function TraceDock({ trace, open, onToggle, fhirBase }) {
  const [filter, setFilter] = useState("all");
  const [errorsOnly, setErrorsOnly] = useState(false);
  const { entries, clear } = trace;
  const shown = entries.filter((e) =>
    (filter === "all" || e.source === filter) && (!errorsOnly || isError(e)));
  const cerner = entries.filter((e) => e.source === "cerner");
  const errors = entries.filter(isError).length;
  const retries = cerner.filter((e) => e.attempt > 1).length;
  const base = (fhirBase || "").replace(/\/$/, "");

  return (
    <aside className={`trace ${open ? "open" : ""}`} aria-label="API trace">
      <div className="trace-bar">
        <button className="trace-toggle" onClick={onToggle} aria-expanded={open}>
          <span className="trace-caret">{open ? "▾" : "▴"}</span>
          <b>API trace</b>
          <span className="trace-counts">
            {entries.length} calls · {cerner.length} to Cerner
            {retries > 0 && ` · ${retries} retries`}
            {errors > 0 && <span className="bad"> · {errors} errors</span>}
          </span>
        </button>
        <div className="trace-actions">
          {open && (
            <>
              <div className="seg" role="group" aria-label="Filter calls">
                {[["all", "All"], ["app", "App"], ["cerner", "Cerner"]].map(([id, label]) => (
                  <button key={id} className={filter === id ? "active" : ""} onClick={() => setFilter(id)}>{label}</button>
                ))}
              </div>
              <label className="check small">
                <input type="checkbox" checked={errorsOnly} onChange={(e) => setErrorsOnly(e.target.checked)} />
                Errors only
              </label>
            </>
          )}
          <button className="ghost" onClick={clear} disabled={entries.length === 0}>Clear</button>
        </div>
      </div>

      {open && (
        <div className="trace-body">
          {shown.length === 0 ? (
            <p className="hint trace-empty">
              {entries.length === 0
                ? "No calls yet. Each request (Translate & Find Member, Fetch, Download packet) starts a fresh trace."
                : "No calls match this filter."}
            </p>
          ) : (
            <ol className="trace-list">
              {shown.map((e) => (
                <li key={e.id} className="trace-row">
                  <span className="mono muted">{time(e.at)}</span>
                  <span className={`src src-${e.source}`}>{e.source === "app" ? "App" : "Cerner"}</span>
                  <span className="mono method">{e.method}</span>
                  <span className={`mono status ${statusClass(e)}`}>{e.status ?? (e.note ? "→" : "ERR")}</span>
                  <span className="mono muted ms">{e.ms != null ? `${e.ms} ms` : ""}</span>
                  <span className="trace-url">
                    <code title={e.url}>
                      {e.source === "cerner" && base && e.url.startsWith(base)
                        ? <><span className="muted">{base}</span>{e.url.slice(base.length)}</>
                        : e.url}
                    </code>
                    {e.attempt > 1 && <span className="chip warn">retry {e.attempt - 1}</span>}
                    {e.accept && e.accept !== "application/fhir+json" && <span className="chip">Accept: {e.accept}</span>}
                    {e.note && <span className="chip">{e.note}</span>}
                    {e.error && <span className="bad small">{e.error}</span>}
                    {e.body && <span className="trace-req mono muted">{e.body}</span>}
                  </span>
                </li>
              ))}
            </ol>
          )}
        </div>
      )}
    </aside>
  );
}
