import React, { useEffect, useRef, useState } from "react";
import { api } from "./api.js";
import { TraceDock, useTrace } from "./trace.jsx";
import { DateInput, toUS } from "./dates.jsx";

// Real chart-chase service notes.
const SAMPLES = {
  TRC: "TRC - REQUIRED - Member demographic sheet: Name and date of birth, 2026 Evidence of discharge medication reconciliations, medication lists and medication reviews, 2026 Hospital discharge summaries, 2026 Hospital discharge instructions, 2026 Documentation of notification of inpatient admissions or evidence of admission orders, 2026 Specialist consultation reports, 2026 Progress notes and telehealth's. Require inpatient and outpatient records. If you are not sure you have this information within the record, please send all progress notes for 2026 with notation of the elements you do not have in the record (i.e.: Discharge medications not reconciled, or hospital stay not reported to this office.) Note: Inpatient notes should be only from one facility per record.",
  CBP: "CBP - REQUIRED - Member demographic sheet: Name and date of birth. Last blood pressure reading in 2025 – may be from a vital signs flowsheet or a progress note. Please note: Provider name must be included on documentation. Telehealth and patient reported BP is acceptable. Exception: If the blood pressure reading was taken on the same day as a diagnostic test/procedure that required a change in diet or medication regimen (for example: colonoscopy), please send the blood pressure reading from the appointment just prior to that date of service. A blood pressure reading on the same day as a fasting blood test is acceptable. If you are not sure you have this qualified information within the record, please send all the progress notes for 2025 and all captured blood pressures, for review noting that you do not believe you have a qualifying BP.",
  BPD: "BPD - REQUIRED - Member demographic sheet: Name and date of birth. Last blood pressure reading in 2025 – may be from a vital signs flowsheet or a progress note. Please note: Provider name must be included on documentation. Telehealth and patient reported BP is acceptable. Exception: If the blood pressure reading was taken during as acute inpatient stay or an ED visit or on the same day as a diagnostic test/procedure that required a change in diet or medication regimen (for example: colonoscopy or stress test), please send the blood pressure reading from the appointment just prior to that date of service. A blood pressure reading on the same day as a fasting blood test is acceptable. If you are not sure you have this qualified information within the record, please send all the progress notes for 2025 and all captured blood pressures, for review noting that you do not believe you have a qualifying BP.",
  GSD: "GSD - REQUIRED - Member demographic sheet: Name and date of birth. Latest 2025 hemoglobin A1c (HbA1c), GMI value from 2025 lab result or progress note. Please note: 2025 Diabetes flow sheets or 2025 Progress notes. Member history information: reviewed and signed. Lab results or lab flowsheets (e.g., HbA1c). Referrals, specialist or consultation notes. Hospital laboratory records, Progress notes. If you are not sure you have this information within the record, please send all the required listed documents you have from 2025 for review with notation of the elements you do not have in the record.",
  "All records": "All medical records from 1/1/2025 through 2026 YTD including Member Biographical Information Sheet",
};

// Test patients that have data, per FHIR server. All made-up sandbox/mock data.
// `name` + `dob` are verified to find the member by name search too.
const DEMO_PATIENTS = {
  sandbox: [
    { id: "12724066", name: "Nancy Patchtest", dob: "1990-01-02", hint: "Full record: ~990 progress notes, H&P, consults, discharge summaries, BP. Best for TRC / All records." },
    { id: "12742400", name: "Tim Peters", dob: "1990-09-15", hint: "Progress notes + BP. Quick CBP demo. By name it matches 2 members: pick the right one." },
    { id: "12744891", name: "Wilma Smart", dob: "1950-03-20", hint: "Progress notes + BP, older member. By name it matches 7 members." },
    { id: "12744580", name: "Smart John", dob: "1990-09-16", hint: "Progress notes, no BP: results come back missing." },
    { id: "12752183", name: "Joe Homenick", dob: "1977-08-25", hint: "BP readings but no progress notes: document codes come back missing." },
    { id: "12744734", name: "Kelli Peters", dob: "1964-04-04", hint: "Almost empty record: shows the missing-items sheet." },
    { id: "12803698", name: "Joe Briggs", dob: "2001-01-01", hint: "14 inpatient stays and 40 BP readings: TRC and the encounter view. By name it matches 2 members." },
    { id: "12744733", name: "Jon Peters", dob: "1955-05-05", hint: "Notes and a few BP readings, older member: CBP / BPD." },
    { id: "14741431", name: "Joek Jackson", dob: "1988-09-30", hint: "One BP reading and no notes: the notes come back missing." },
    { id: "12766709", name: "FHIR CERNER, Joe", dob: "2012-05-17", hint: "Child with 3 inpatient stays. Two-word last name: type it as “FHIR CERNER, Joe”." },
  ],
  mock: [
    { id: "12724066", name: "Jane Doe", dob: "1958-04-02", hint: "Offline mock: 8 notes and 3 BP readings, all in 2026. Keep the date window on." },
  ],
};

function demoPatientsFor(fhirBase = "") {
  if (fhirBase.includes("/r4/mock")) return DEMO_PATIENTS.mock;
  if (fhirBase.includes("fhir-open.cerner.com")) return DEMO_PATIENTS.sandbox;
  return [];
}

// The /api/run result plus the "other" records the reviewer picked, as one extra row.
function extraDocs(run) {
  // Records outside the plan rows: Other records plus notes fetched per visit, without repeats.
  const inRows = new Set(run.result.rows.flatMap((r) => r.docs.map((d) => d.id)));
  const byId = new Map();
  for (const d of [...(run.result.other || []).flatMap((g) => g.docs), ...Object.values(run.extraDocs || {})]) {
    if (!inRows.has(d.id) && !byId.has(d.id)) byId.set(d.id, d);
  }
  return [...byId.values()];
}

function packetResult(run) {
  const picked = new Set(run.otherSelected || []);
  const docs = extraDocs(run).filter((d) => picked.has(d.id));
  if (!docs.length) return run.result;
  const extra = { code: "other", label: "Other records (added)", reason: "added by the reviewer", count: docs.length, error: null, docs, extra: true };
  return { ...run.result, rows: [...run.result.rows, extra] };
}

const checkedOnly = (items) => items.filter((i) => i.on).map(({ on, ...rest }) => rest);

export default function App() {
  const [health, setHealth] = useState(null);
  const [catalog, setCatalog] = useState({ documents: [], observations: [] });
  const [note, setNote] = useState(SAMPLES.TRC);
  const [useLlm, setUseLlm] = useState(true);
  const [plan, setPlan] = useState(null);
  const [name, setName] = useState("");
  const [dob, setDob] = useState("");
  const [patients, setPatients] = useState(null);
  const [patientId, setPatientId] = useState("");
  const [applyDates, setApplyDates] = useState(true);
  const [titleFallback, setTitleFallback] = useState(false);
  const [discover, setDiscover] = useState(false); // list every other record the patient has (off by default; on for "all records")
  const [includeOther, setIncludeOther] = useState(false); // put those in the packet too ("all records")
  const [run, setRun] = useState(null); // { id, plan as sent, result, progress, done }
  const [packet, setPacket] = useState(null); // { runId, job, state, files, counts, failed, error }
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");
  const [traceOpen, setTraceOpen] = useState(false);
  const [formKey, setFormKey] = useState(0); // bumps on Start over to reset the date fields
  const [showSuggest, setShowSuggest] = useState(false);
  const [prompt, setPrompt] = useState(null); // shown when not null
  const [promptError, setPromptError] = useState("");
  const runAbort = useRef(null);

  const packetRunning = packet?.state === "running";
  const trace = useTrace(!!busy || packetRunning);

  useEffect(() => {
    api.health()
      .then((h) => {
        setHealth(h);
        setUseLlm(h.llm);
      })
      .catch((e) => setError(`Backend not reachable: ${e.message}`));
    api.catalog().then(setCatalog).catch(() => {});
  }, []);

  // Poll the background packet job until it finishes, then save the zip.
  useEffect(() => {
    if (!packetRunning) return undefined;
    const timer = setInterval(async () => {
      try {
        const s = await api.packetStatus(packet.job);
        setPacket((p) => (p && p.job === s.job ? { ...p, ...s } : p));
        if (s.state === "ready") {
          api.packetSave(s.job);
          setInfo(s.failed
            ? `Packet saved. ${s.failed} file(s) failed to download; see download-errors.txt in the zip.`
            : `Packet saved: ${s.zipName}`);
        } else if (s.state === "failed") {
          setError(`Packet failed: ${s.error}`);
        }
      } catch (e) {
        setPacket((p) => (p ? { ...p, state: "failed", error: e.message } : p));
        setError(`Packet failed: ${e.message}`);
      }
    }, 1000);
    return () => clearInterval(timer);
  }, [packetRunning, packet?.job]);

  // Each request starts with a fresh trace and clean banners.
  async function step(label, fn) {
    trace.clear();
    setError("");
    setInfo("");
    setBusy(label);
    try {
      await fn();
    } catch (e) {
      if (e.name !== "AbortError") setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const isPatientId = /^\d+$/.test(name.trim());
  const needsDob = name.trim() !== "" && !isPatientId && !dob;
  const patientReady = name.trim() !== "" && !needsDob;
  const canStart = !busy && note.trim() !== "" && patientReady;

  // Translate the note and look up the member at the same time.
  const start = () =>
    step(useLlm ? "Asking Azure OpenAI to read the service note and looking up the member…" : "Mapping the note and looking up the member…", async () => {
      setRun(null);
      setPlan(null);
      setPatients(null);
      setPatientId("");
      const [planRes, patientsRes] = await Promise.allSettled([api.plan(note, useLlm), api.patients(name.trim(), dob)]);
      const problems = [];
      if (planRes.status === "fulfilled") {
        adoptPlan(planRes.value);
      } else {
        problems.push(`Service note: ${planRes.reason.message}`);
      }
      if (patientsRes.status === "fulfilled") {
        setPatients(patientsRes.value);
        setPatientId(patientsRes.value.length === 1 ? patientsRes.value[0].id : "");
      } else {
        problems.push(`Patient: ${patientsRes.reason.message}`);
      }
      if (problems.length) throw new Error(problems.join(" · "));
    });

  // Take the agent's plan: every code on, and the fetch options its fallback_strategy asks for.
  const adoptPlan = (p) => {
    const on = (items = []) => items.map((i) => ({ ...i, on: true }));
    setPlan({ ...p, documents: on(p.documents), observations: on(p.observations), reports: on(p.reports), encounters: on(p.encounters) });
    const fallback = p.intent?.fallback_strategy || "strict";
    setTitleFallback(fallback === "title_match" || fallback === "broad");
    setDiscover(fallback === "broad" || !!p.all_records);
    setIncludeOther(!!p.all_records);
  };

  const retranslate = () =>
    step(useLlm ? "Asking Azure OpenAI to read the service note…" : "Applying keyword rules…", async () => {
      adoptPlan(await api.plan(note, useLlm));
      setRun(null);
    });

  const fetchAll = () =>
    step("Requesting each LOINC code from Cerner…", async () => {
      const sent = {
        ...plan,
        documents: checkedOnly(plan.documents), observations: checkedOnly(plan.observations),
        reports: checkedOnly(plan.reports || []), encounters: checkedOnly(plan.encounters || []),
      };
      const order = [...sent.documents, ...sent.reports, ...sent.encounters].map((d) => d.code);
      const byPlanOrder = (rows) => [...rows].sort((a, b) => order.indexOf(a.code) - order.indexOf(b.code));
      setRun(null);
      runAbort.current = new AbortController();
      await api.runStream(
        { patient_id: patientId, plan: sent, apply_dates: applyDates, title_fallback: titleFallback, discover },
        (ev) => {
          if (ev.type === "start") {
            setRun({
              id: Date.now(),
              plan: sent,
              done: false,
              otherSelected: [],
              progress: { codes: ev.codes, finished: [], phase: "documents", observations: ev.observations, titleSweep: ev.titleSweep, discover },
              result: { patient: ev.patient, window: ev.window, rows: [], observations: [], observationError: null, missing: [] },
            });
          } else if (ev.type === "row") {
            setRun((r) => ({
              ...r,
              progress: { ...r.progress, finished: [...r.progress.finished, ev.row.code] },
              result: { ...r.result, rows: byPlanOrder([...r.result.rows, ev.row]) },
            }));
          } else if (ev.type === "sweep") {
            setRun((r) => ({ ...r, progress: { ...r.progress, phase: "observations" }, result: { ...r.result, rows: ev.rows } }));
          } else if (ev.type === "other") {
            const ids = ev.groups.flatMap((g) => g.docs.filter((d) => d.binaryUrl).map((d) => d.id));
            setRun((r) => ({
              ...r,
              otherSelected: includeOther ? ids : [],
              result: { ...r.result, other: ev.groups, otherError: ev.error, otherTruncated: ev.truncated, otherRequest: ev.request },
            }));
          } else if (ev.type === "observations") {
            setRun((r) => ({ ...r, result: { ...r.result, observations: ev.observations, observationError: ev.observationError, observationsTruncated: ev.observationsTruncated } }));
          } else if (ev.type === "done") {
            setRun((r) => ({ ...r, done: true, progress: { ...r.progress, phase: "done" }, result: ev.result }));
          }
        },
        runAbort.current.signal,
      );
    });

  // Runs in the background: the page stays usable while files download.
  const downloadPacket = async () => {
    trace.clear();
    setError("");
    setInfo("");
    try {
      const job = await api.packetStart(run.plan, packetResult(run));
      setPacket({ runId: run.id, startedAt: Date.now(), ...job });
    } catch (e) {
      setError(`Packet failed to start: ${e.message}`);
    }
  };

  const toggleOther = (ids, on) =>
    setRun((r) => {
      const selected = new Set(r.otherSelected);
      ids.forEach((id) => (on ? selected.add(id) : selected.delete(id)));
      return { ...r, otherSelected: [...selected] };
    });

  // Encounter view: load the member's visits, and the notes of one visit.
  const loadVisits = async () => {
    const w = run.result.window || {};
    try {
      // Also ask for the visits the fetched notes point to, so they show even when Cerner's
      // listing is all undated registrations.
      const known = new Set(run.result.rows.filter((x) => x.resource === "Encounter").flatMap((x) => x.docs.map((v) => v.id)));
      const linked = [...new Set(run.result.rows.flatMap((x) => x.docs).concat(extraDocs(run))
        .flatMap((d) => encounterIds(d)))].filter((id) => !known.has(id)).slice(0, 200);
      const res = await api.encounters(run.result.patient.id, w.start, w.end, linked);
      setRun((r) => ({ ...r, visits: res.encounters, visitsRequest: res.request, visitsTruncated: res.truncated }));
    } finally {
      trace.refresh();
    }
  };
  const loadVisitDocs = async (encounterId) => {
    let res;
    try {
      res = await api.encounterDocs(run.result.patient.id, encounterId);
    } finally {
      trace.refresh();
    }
    setRun((r) => ({
      ...r,
      extraDocs: { ...(r.extraDocs || {}), ...Object.fromEntries(res.docs.map((d) => [d.id, d])) },
      visitDocsLoaded: { ...(r.visitDocsLoaded || {}), [encounterId]: { ids: res.docs.map((d) => d.id), truncated: res.truncated } },
    }));
    return res.docs;
  };

  const startOver = () => {
    runAbort.current?.abort();
    trace.clear();
    setNote(SAMPLES.TRC);
    setUseLlm(!!health?.llm);
    setPlan(null);
    setName("");
    setDob("");
    setPatients(null);
    setPatientId("");
    setApplyDates(true);
    setTitleFallback(false);
    setDiscover(false);
    setIncludeOther(false);
    setRun(null);
    setPacket(null);
    setBusy("");
    setError("");
    setInfo("");
    setFormKey((k) => k + 1);
    window.scrollTo({ top: 0, behavior: "smooth" });
  };

  const toggle = (kind, code) =>
    setPlan({ ...plan, [kind]: plan[kind].map((i) => (i.code === code ? { ...i, on: !i.on } : i)) });

  const addCode = (kind, code) => {
    if (!code || plan[kind].some((i) => i.code === code)) return;
    const hit = catalog[kind].find((c) => c.code === code);
    setPlan({ ...plan, [kind]: [...plan[kind], { code, label: hit?.label || code, reason: "added by you", on: true }] });
  };

  const windowOk = !applyDates || (plan && plan.start && plan.end && plan.start < plan.end);
  const selected = plan
    ? [plan.documents, plan.observations, plan.reports || [], plan.encounters || []].reduce((n, list) => n + list.filter((i) => i.on).length, 0)
    : 0;
  const demos = demoPatientsFor(health?.fhirBase);
  const demoHint = demos.find((p) => p.id === name || (p.name === name && p.dob === dob))?.hint;
  const runPacket = packet && run && packet.runId === run.id ? packet : null;

  return (
    <div className={`shell ${traceOpen ? "trace-open" : "trace-closed"}`}>
      <header className="topbar">
        <div className="brand">
          <img className="brand-wordmark" src="/advantmed-wordmark.svg" alt="Advantmed" />
          <div>
            <strong>FHIR Record Retrieval</strong>
            <p>Service note → LOINC → Cerner records</p>
          </div>
        </div>
        <div className="topbar-meta">
          {health && (
            <>
              <span className="chip mono" title="FHIR base URL">FHIR: {health.fhirBase}</span>
              <span className={`chip ${health.llm ? "ok" : "warn"}`}>
                {health.llm
                  ? `Azure OpenAI · ${health.llmDeployment}${health.llmTemperature != null ? ` · t=${health.llmTemperature}` : ""}`
                  : "Azure OpenAI not configured"}
              </span>
            </>
          )}
          <button className="ghost start-over" onClick={startOver} title="Clear everything, including the API trace">↺ Start over</button>
        </div>
      </header>

      <main className="page">
        <div className="intro">
          <h1>Chart-chase retrieval</h1>
          <p>Enter the service note and the member, review the LOINC plan, then pull every matching note from Cerner.</p>
        </div>

        {error && <div className="banner error" role="alert">{error}</div>}
        {info && <div className="banner warn">{info}</div>}
        {busy && <div className="banner busy"><span className="spinner" aria-hidden="true" />{busy}</div>}

        <section className="card request-card">
          <div className="card-head">
            <h2>Request</h2>
            <label className="check" title={health && !health.llm ? "Set AOAI_ENDPOINT and AOAI_DEPLOYMENT in .env" : "Read the service note with Azure OpenAI; untick for keyword rules"}>
              <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
              Use Azure OpenAI
            </label>
          </div>

          <div className="field">
            <div className="field-head">
              <label htmlFor="note">Service note</label>
              <div className="row tight">
                <span className="label">Samples</span>
                {Object.keys(SAMPLES).map((k) => (
                  <button key={k} className={`ghost ${note === SAMPLES[k] ? "active" : ""}`} onClick={() => setNote(SAMPLES[k])}>{k}</button>
                ))}
              </div>
            </div>
            <textarea id="note" rows={4} value={note} onChange={(e) => setNote(e.target.value)} />
          </div>

          <div className="field">
            <div className="field-head">
              <label htmlFor="patient">Member</label>
              {demos.length > 0 && (
                <button className="ghost small-btn" onClick={() => setShowSuggest((v) => !v)} aria-expanded={showSuggest}>
                  {showSuggest ? "Hide suggested members" : `Suggested members (${demos.length})`}
                </button>
              )}
            </div>
            <div className="row wrap member-row">
              <input id="patient" className="grow" placeholder="Patient name (e.g. Tim Peters) or Patient ID" value={name}
                onChange={(e) => setName(e.target.value)}
                onKeyDown={(e) => e.key === "Enter" && canStart && start()} />
              <DateInput key={`dob-${formKey}`} value={dob} onChange={setDob} aria-label="Date of birth"
                className={needsDob ? "invalid" : ""} aria-invalid={needsDob} required={!isPatientId}
                onKeyDown={(e) => e.key === "Enter" && canStart && start()}
                title={isPatientId ? "Date of birth (optional with a Patient ID)" : "Date of birth (required with a name)"} />
            </div>
            <p className={`hint ${needsDob ? "warn-text" : ""}`}>
              {needsDob ? "Date of birth (mm/dd/yyyy) is required when searching by name."
                : demoHint || "Patient ID on its own, or name + date of birth."}
            </p>
            {showSuggest && demos.length > 0 && (
              <div className="table-wrap suggest">
                <table>
                  <thead><tr><th>Suggested member</th><th>DOB</th><th>Patient ID</th><th>Good for</th><th>Fill in</th></tr></thead>
                  <tbody>
                    {demos.map((p) => {
                      const byId = name === p.id;
                      const byName = name === p.name && dob === p.dob;
                      return (
                        <tr key={p.id} className={byId || byName ? "picked" : ""}>
                          <td><b>{p.name}</b></td>
                          <td className="mono nowrap">{toUS(p.dob)}</td>
                          <td className="mono">{p.id}</td>
                          <td className="muted">{p.hint}</td>
                          <td className="nowrap">
                            <div className="row tight">
                              <button className={`ghost ${byId ? "active" : ""}`} onClick={() => { setName(p.id); setDob(""); }}>Use ID</button>
                              <button className={`ghost ${byName ? "active" : ""}`} onClick={() => { setName(p.name); setDob(p.dob); }}>Use name + DOB</button>
                            </div>
                          </td>
                        </tr>
                      );
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </div>

          <div className="row">
            <button className="primary" onClick={start} disabled={!canStart}>Translate &amp; Find Member</button>
            {!useLlm && <span className="hint">Keyword rules only</span>}
            <button className="ghost small-btn" aria-expanded={!!prompt}
              onClick={() => {
                if (prompt) { setPrompt(null); return; }
                setPromptError("");
                api.prompt(note).then(setPrompt).catch((e) => setPromptError(e.message));
              }}>
              {prompt ? "Hide prompt" : "View prompt"}
            </button>
          </div>
          {promptError && <div className="banner error small">{promptError}</div>}
          {prompt && <PromptView prompt={prompt} onRefresh={() => api.prompt(note).then(setPrompt)} stale={!prompt.messages[1].content.endsWith(note)} />}
        </section>

        <section className={`card ${plan ? "" : "dim"}`}>
          <h2><span className="num">1</span> LOINC plan</h2>
          {!plan ? <p className="hint">The codes and date window read from the service note appear here.</p> : (
            <div className="plan">
              <div className="row wrap">
                <span className={`chip ${plan.source === "azure-openai" ? "ok" : ""}`}>
                  {plan.source === "azure-openai" ? "Mapped by Azure OpenAI" : "Mapped by keyword rules"}
                </span>
                <span className="chip">Measures: {plan.measures.join(", ") || "generic"}</span>
                {plan.intent && <span className="chip">Mode: {plan.intent.mode.replace(/_/g, " ")}</span>}
                {plan.intent && <span className="chip" title="What happens when items are missing">Fallback: {plan.intent.fallback_strategy.replace(/_/g, " ")}</span>}
                {plan.intent?.clinical_targets.map((t) => (
                  <span key={t.kind} className="chip ok" title={t.any_history ? "Any date counts" : t.through ? `Through ${toUS(t.through)}` : "Within the window"}>
                    Target: {t.kind.replace(/_/g, " ")}{t.any_history ? " (any history)" : t.through ? ` (through ${toUS(t.through)})` : ""}
                  </span>
                ))}
                <button className="ghost" onClick={retranslate} disabled={!!busy || !note.trim()} title="Read the service note again">Re-translate</button>
              </div>
              <div className="row wrap">
                <span className="label" title="job_envelope: the window every document, lab report and encounter search uses">Job window</span>
                <DateInput value={plan.start} onChange={(v) => setPlan({ ...plan, start: v })} aria-label="Window start" />
                <span className="label">to before</span>
                <DateInput value={plan.end} onChange={(v) => setPlan({ ...plan, end: v })} aria-label="Window end (exclusive)" />
                {plan.intent?.date_windows.filter((w) => w.purpose === "measurement_year").map((w) => (
                  <span key={w.purpose} className="chip" title="Results (Observation) are searched in this window">
                    Measurement year {toUS(w.from)} – {toUS(w.to)}
                  </span>
                ))}
              </div>
              {plan.notes && <p className="hint warn-text">{plan.notes}</p>}

              <PlanTable title="Documents (DocumentReference.type)" kind="documents" items={plan.documents}
                options={catalog.documents} onToggle={toggle} onAdd={addCode} />
              <PlanTable title="Results (Observation.code)" kind="observations" items={plan.observations}
                options={catalog.observations} onToggle={toggle} onAdd={addCode} />
              {plan.reports?.length > 0 && (
                <PlanTable title="Lab reports (DiagnosticReport.category)" kind="reports" items={plan.reports} codeLabel="Category" onToggle={toggle} />
              )}
              {plan.encounters?.length > 0 && (
                <PlanTable title="Encounters (sorted by Encounter.class)" kind="encounters" items={plan.encounters} codeLabel="Group" onToggle={toggle} />
              )}
              <p className="hint">Also requested: <b>Patient</b> (biographical sheet).</p>
              {plan.intent && <IntentJson intent={plan.intent} />}
            </div>
          )}
        </section>

        <section className={`card ${patients ? "" : "dim"}`}>
          <h2><span className="num">2</span> Member Confirmation</h2>
          {!patients && <p className="hint">Matches for the member you entered appear here.</p>}
          {patients && patients.length === 0 && (
            <div className="banner warn">No patient matches “{name}”{dob ? ` born ${toUS(dob)}` : ""}. Check the spelling, DOB or Patient ID above and try again.</div>
          )}
          {patients && patients.length > 0 && (
            <>
              <p className="hint">{patients.length === 1 ? "One match, selected." : `${patients.length} matches: pick the right member.`}</p>
              <PatientTable patients={patients} patientId={patientId} onPick={setPatientId} />
            </>
          )}
        </section>

        <section className={`card ${plan && patientId ? "" : "dim"}`}>
          <h2><span className="num">3</span> Download Package</h2>
          <div className="row wrap">
            <label className="check">
              <input type="checkbox" checked={applyDates} onChange={(e) => setApplyDates(e.target.checked)} />
              Apply Date Window
            </label>
            <label className="check">
              <input type="checkbox" checked={titleFallback} onChange={(e) => setTitleFallback(e.target.checked)} />
              <span title="Catches notes with no LOINC code by their name; close matches count">Match By Doc Name</span>
            </label>
            <label className="check">
              <input type="checkbox" checked={discover} onChange={(e) => { setDiscover(e.target.checked); if (!e.target.checked) setIncludeOther(false); }} />
              <span title="Lists every other record the member has as “Not downloaded”">List Other Records</span>
            </label>
            <label className="check" title="On automatically when the service note asks for all medical records">
              <input type="checkbox" checked={includeOther} disabled={!discover} onChange={(e) => setIncludeOther(e.target.checked)} />
              <span title="Every record the member has goes in the packet, not only the plan's codes">Download All Record Types</span>
            </label>
            <button className="primary" onClick={fetchAll} disabled={!!busy || !plan || !patientId || selected === 0 || !windowOk}>
              Fetch ({selected} codes)
            </button>
          </div>
          {plan && !windowOk && <p className="hint warn-text">Enter a valid window in step 1 (mm/dd/yyyy, start before end) or untick “Apply Date window”.</p>}
          <p className="hint">Sandbox data is often older than the request window — untick “Apply Date Window” if nothing comes back.</p>

          {plan?.all_records && <p className="hint">The service note asks for <b>all medical records</b>: every record the member has goes in the packet (the plan codes plus everything under “Other records”).</p>}

          {run && <Results key={run.id} run={run} packet={runPacket} onPacket={downloadPacket} onToggleOther={toggleOther}
            onLoadVisits={loadVisits} onLoadVisitDocs={loadVisitDocs} rps={health?.fhirMaxRps} />}
        </section>
      </main>

      <TraceDock trace={trace} open={traceOpen} onToggle={() => setTraceOpen((o) => !o)} fhirBase={health?.fhirBase} />
    </div>
  );
}

function PromptView({ prompt, onRefresh, stale }) {
  return (
    <div className="prompt">
      <div className="prompt-head">
        <span className="hint">
          What <b>Translate</b> sends to Azure OpenAI
          {prompt.deployment ? <> · deployment <span className="mono">{prompt.deployment}</span></> : " · (no deployment configured)"}
          {" · "}API <span className="mono">{prompt.apiVersion}</span>
          {" · "}temperature <span className="mono">{prompt.temperature ?? "default"}</span>
          {" · "}response_format <span className="mono">{prompt.responseFormat}</span>
        </span>
        {stale && <button className="ghost small-btn" onClick={onRefresh}>Refresh for the current note</button>}
      </div>
      {prompt.messages.map((m) => (
        <div key={m.role} className="prompt-msg">
          <div className="prompt-role mono">{m.role}</div>
          <pre>{m.content}</pre>
        </div>
      ))}
      <p className="hint">The reply must be JSON. Codes not on the allowed lists are dropped, dates are checked and capped at tomorrow, and measures named in the note are kept.</p>
    </div>
  );
}

function PatientTable({ patients, patientId, onPick }) {
  const pager = usePager(patients);
  return (
    <>
      <div className="table-wrap">
        <table>
          <thead><tr><th className="narrow"></th><th>Name</th><th>DOB</th><th>Sex</th><th>Patient ID</th></tr></thead>
          <tbody>
            {pager.pageItems.map((p) => (
              <tr key={p.id} className={`clickable ${p.id === patientId ? "picked" : ""}`} onClick={() => onPick(p.id)}>
                <td><input type="radio" name="patient" checked={p.id === patientId} onChange={() => onPick(p.id)} /></td>
                <td>{p.name}</td><td className="mono">{toUS(p.birthDate)}</td><td>{p.gender}</td><td className="mono">{p.id}</td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager pager={pager} noun="matches" />
    </>
  );
}

function PlanTable({ title, kind, items, options, onToggle, onAdd, codeLabel = "LOINC" }) {
  const pager = usePager(items);
  return (
    <div className="plan-block">
      <h3>{title}</h3>
      {items.length === 0 ? <p className="hint">None requested.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th className="narrow"></th><th>{codeLabel}</th><th>What</th><th>Required</th><th>Why</th></tr></thead>
            <tbody>
              {pager.pageItems.map((i) => (
                <tr key={i.code} className={i.on ? "" : "off"}>
                  <td><input type="checkbox" checked={i.on} onChange={() => onToggle(kind, i.code)} aria-label={`Include ${i.code}`} /></td>
                  <td className="mono code">{i.code}</td>
                  <td>{i.label}</td>
                  <td>{i.required ? <span className="chip warn">required</span> : <span className="muted small">optional</span>}</td>
                  <td className="muted">{i.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <Pager pager={pager} noun="codes" />
      {onAdd && options && (
        <select value="" onChange={(e) => onAdd(kind, e.target.value)}>
          <option value="">+ Add a code…</option>
          {options.filter((o) => !items.some((i) => i.code === o.code)).map((o) => (
            <option key={o.code} value={o.code}>{o.code} · {o.label}</option>
          ))}
        </select>
      )}
    </div>
  );
}

// The agent's output exactly as validated (the agreed clinical-intent schema).
function IntentJson({ intent }) {
  const [open, setOpen] = useState(false);
  return (
    <div className="intent-json">
      <button className="ghost small-btn" onClick={() => setOpen((o) => !o)} aria-expanded={open}>
        {open ? "Hide agent output (JSON)" : "Agent output (JSON)"}
      </button>
      {open && <pre className="json">{JSON.stringify(intent, null, 2)}</pre>}
    </div>
  );
}

const PAGE_SIZE = 10;

function usePager(items, pageSize = PAGE_SIZE) {
  const [page, setPage] = useState(1);
  const pages = Math.max(1, Math.ceil(items.length / pageSize));
  const current = Math.min(page, pages); // stay in range when the list shrinks (e.g. filtering)
  const offset = (current - 1) * pageSize;
  return { page: current, pages, setPage, offset, pageItems: items.slice(offset, offset + pageSize), total: items.length, pageSize };
}

function Pager({ pager, noun = "records" }) {
  const { page, pages, setPage, offset, total, pageItems } = pager;
  if (total <= pager.pageSize) return null;
  return (
    <div className="pager">
      <span className="hint">{offset + 1}–{offset + pageItems.length} of {total} {noun}</span>
      <div className="pager-nav">
        <button className="ghost" onClick={() => setPage(1)} disabled={page === 1} aria-label="First page">«</button>
        <button className="ghost" onClick={() => setPage(page - 1)} disabled={page === 1}>‹ Prev</button>
        <span className="mono pager-label">Page {page} of {pages}</span>
        <button className="ghost" onClick={() => setPage(page + 1)} disabled={page === pages}>Next ›</button>
        <button className="ghost" onClick={() => setPage(pages)} disabled={page === pages} aria-label="Last page">»</button>
      </div>
    </div>
  );
}

function Progress({ progress }) {
  const total = progress.codes.length;
  const done = progress.finished.length;
  const pct = total ? Math.round((done / total) * 100) : 100;
  return (
    <div className="progress">
      <div className="progress-head">
        <span>
          {done < total ? `Fetched ${done} of ${total} codes`
            : progress.titleSweep && progress.phase === "documents" ? "Matching notes by title…"
            : progress.observations ? "Fetching results (BP / HbA1c / GMI)…" : "Finishing…"}
        </span>
        <span className="mono">{pct}%</span>
      </div>
      <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-codes">
        {progress.codes.map((c) => {
          const finished = progress.finished.includes(c.code);
          return (
            <span key={c.code} className={`chip ${finished ? "ok" : ""}`} title={c.label}>
              {finished ? "✓ " : ""}<span className="mono">{c.code}</span>
            </span>
          );
        })}
      </div>
    </div>
  );
}

function useFilter(items, text, fields) {
  const q = text.trim().toLowerCase();
  if (!q) return items;
  return items.filter((it) => fields.some((f) => String(f(it) ?? "").toLowerCase().includes(q)));
}

const DL_LABEL = { ready: "Ready", queued: "Queued", downloading: "Downloading", done: "Done", failed: "Failed", duplicate: "Duplicate", nofile: "No file", notdownloaded: "Not downloaded" };

// Where a note stands in the packet download. `picked` = other records added to the packet.
function downloadStatus(d, packet, picked) {
  if (!d.binaryUrl) return { status: "nofile" };
  if (d.duplicate) return { status: "duplicate", error: "Already in the packet under another code" };
  if ((d.matchedBy === "other" || d.matchedBy === "encounter") && !picked?.has(d.id)) {
    return { status: "notdownloaded", error: "Not in the LOINC plan. Tick it to add it to the packet." };
  }
  const f = packet?.files?.[d.file];
  return f ? { status: f.status, error: f.error } : { status: "ready" };
}

function StatusChip({ status, error }) {
  return (
    <span className={`dl dl-${status}`} title={error || undefined}>
      {status === "downloading" && <span className="spinner tiny" aria-hidden="true" />}
      {DL_LABEL[status] || status}
    </span>
  );
}

function DocTab({ row, packet }) {
  const [filter, setFilter] = useState("");
  const docs = useFilter(row.docs, filter, [(d) => d.title, (d) => d.author, (d) => d.date, (d) => toUS(d.date)]);
  const pager = usePager(docs);
  return (
    <div>
      <div className="tab-head">
        <div>
          <b>{row.label}</b> <span className="mono muted">LOINC {row.code}</span>
          {row.reason && <div className="hint">Why: {row.reason}</div>}
        </div>
        {row.docs.length > PAGE_SIZE && (
          <input className="tab-filter" placeholder="Filter by title, author, date…" value={filter}
            onChange={(e) => { setFilter(e.target.value); pager.setPage(1); }} />
        )}
      </div>
      {row.request && (
        <div className="request" title="The FHIR search sent to Cerner for this code">
          <span className="request-verb">GET</span>
          <code>{row.request}</code>
        </div>
      )}
      {row.error && <div className="banner error small">{row.error}</div>}
      {row.truncated && <div className="banner warn">Showing the first {row.count}; Cerner has more. Narrow the date window or raise FHIR_MAX_PAGES in .env.</div>}
      {!row.error && row.count === 0 && <div className="banner warn">Nothing found for this code. It is listed in missing-items.txt.</div>}
      {docs.length > 0 && (
        <div className="table-wrap">
          <table className="doc-table">
            <colgroup>
              <col className="col-date" /><col /><col className="col-author" /><col className="col-status" /><col className="col-file" />
            </colgroup>
            <thead><tr><th>Date</th><th>Title</th><th>Author</th><th>Status</th><th>File</th></tr></thead>
            <tbody>
              {pager.pageItems.map((d) => {
                const dl = downloadStatus(d, packet);
                return (
                  <tr key={d.id}>
                    <td className="mono nowrap">{d.date ? toUS(d.date) : "undated"}</td>
                    <td>
                      <b>{d.title || "(untitled)"}</b>
                      {(d.matchedBy === "title" || (d.docStatus && d.docStatus !== "final")) && (
                        <div className="chips sub">
                          {d.docStatus && d.docStatus !== "final" && <span className="chip warn">{d.docStatus}</span>}
                          {d.matchedBy === "title" && (
                            <span className="chip warn" title="No LOINC match; matched on the note title">
                              {d.matchScore != null && d.matchScore < 1 ? `close title match ${Math.round(d.matchScore * 100)}%` : "matched by title"}
                            </span>
                          )}
                        </div>
                      )}
                    </td>
                    <td className="muted">
                      {d.author}
                      {d.facility && <div className="small" title={`Encounter class ${d.encounterClass || "?"}`}>{d.facility}</div>}
                    </td>
                    <td><StatusChip {...dl} /></td>
                    <td className="nowrap">
                      {d.binaryUrl
                        ? <a className="open-link" href={api.binaryUrl(d)} target="_blank" rel="noreferrer" title={`Binary: ${d.binaryUrl}`}>Open</a>
                        : <span className="muted small">—</span>}
                    </td>
                  </tr>
                );
              })}
            </tbody>
          </table>
        </div>
      )}
      {filter && docs.length === 0 && <p className="hint">No notes match “{filter}”.</p>}
      <Pager pager={pager} noun="notes" />
    </div>
  );
}

// Records the member has that no plan code covered, grouped by type. Not downloaded
// unless the reviewer ticks them (or the note asked for all medical records).
function OtherTab({ r, done, packet, picked, onToggle }) {
  const [open, setOpen] = useState(() => new Set());
  const groupPager = usePager(r.other || []);
  if (!r.other && !r.otherError) {
    return <p className="hint">{done ? "No other records." : "Listing the member's other records once the plan codes are fetched…"}</p>;
  }
  const groups = r.other || [];
  const selectable = (g) => g.docs.filter((d) => d.binaryUrl).map((d) => d.id);
  const all = groups.flatMap(selectable);
  const pickedCount = all.filter((id) => picked.has(id)).length;
  const locked = packet?.state === "running";
  const toggleOpen = (key) => setOpen((o) => { const n = new Set(o); n.has(key) ? n.delete(key) : n.add(key); return n; });

  return (
    <div>
      <div className="tab-head">
        <div>
          <b>Other records</b> <span className="muted">· not in the LOINC plan</span>
          <div className="hint">
            {all.length} records in {groups.length} types. They are <b>not downloaded</b> unless you add them; {pickedCount} added to the packet.
          </div>
        </div>
        {all.length > 0 && (
          <div className="row tight">
            <button className="ghost" disabled={locked || pickedCount === all.length} onClick={() => onToggle(all, true)}>Add all to packet</button>
            <button className="ghost" disabled={locked || pickedCount === 0} onClick={() => onToggle(all, false)}>Remove all</button>
          </div>
        )}
      </div>
      {r.otherRequest && (
        <div className="request" title="The FHIR search sent to Cerner for every record of this member">
          <span className="request-verb">GET</span>
          <code>{r.otherRequest}</code>
        </div>
      )}
      {r.otherError && <div className="banner error small">Could not list other records: {r.otherError}</div>}
      {r.otherTruncated && <div className="banner warn">Only the first pages were read; the member has more records. Narrow the date window or raise FHIR_MAX_PAGES.</div>}
      {locked && <p className="hint warn-text">The packet is downloading; changes apply to the next packet.</p>}
      {groups.length === 0 && !r.otherError && <div className="banner warn">Every record the member has is already covered by the plan.</div>}

      <div className="other-groups">
        {groupPager.pageItems.map((g) => {
          const ids = selectable(g);
          const n = ids.filter((id) => picked.has(id)).length;
          const isOpen = open.has(g.key);
          return (
            <div key={g.key} className={`other-group ${n ? "has-picked" : ""}`}>
              <div className="other-head">
                <input type="checkbox" aria-label={`Add all ${g.label} to the packet`} disabled={locked || ids.length === 0}
                  checked={ids.length > 0 && n === ids.length}
                  ref={(el) => { if (el) el.indeterminate = n > 0 && n < ids.length; }}
                  onChange={(e) => onToggle(ids, e.target.checked)} />
                <button className="other-title" onClick={() => toggleOpen(g.key)} aria-expanded={isOpen}>
                  <span className="trace-caret">{isOpen ? "▾" : "▸"}</span>
                  <b>{g.label}</b>
                  {g.loinc ? <span className="mono muted">LOINC {g.loinc}</span> : <span className="chip">no LOINC</span>}
                  {g.inCatalog && <span className="chip" title="This code is on the allowed list but wasn't in the plan">known code, not requested</span>}
                </button>
                <span className="mono muted nowrap">{g.first ? `${toUS(g.first)} – ${toUS(g.last)}` : ""}</span>
                <span className="tab-count">{n ? `${n}/${g.count}` : g.count}</span>
              </div>
              {isOpen && <OtherDocs docs={g.docs} packet={packet} picked={picked} onToggle={onToggle} locked={locked} />}
            </div>
          );
        })}
      </div>
      <Pager pager={groupPager} noun="record types" />
    </div>
  );
}

function OtherDocs({ docs, packet, picked, onToggle, locked }) {
  const pager = usePager(docs);
  return (
    <div className="other-docs">
      <div className="table-wrap">
        <table className="doc-table">
          <colgroup>
            <col className="col-check" /><col className="col-date" /><col /><col className="col-author" /><col className="col-status" /><col className="col-file" />
          </colgroup>
          <thead><tr><th></th><th>Date</th><th>Title</th><th>Author</th><th>Status</th><th>File</th></tr></thead>
          <tbody>
            {pager.pageItems.map((d) => (
              <tr key={d.id} className={picked.has(d.id) ? "picked" : ""}>
                <td>
                  <input type="checkbox" aria-label={`Add ${d.title} to the packet`} disabled={locked || !d.binaryUrl}
                    checked={picked.has(d.id)} onChange={(e) => onToggle([d.id], e.target.checked)} />
                </td>
                <td className="mono nowrap">{d.date ? toUS(d.date) : "undated"}</td>
                <td><b>{d.title || "(untitled)"}</b></td>
                <td className="muted">{d.author}</td>
                <td><StatusChip {...downloadStatus(d, packet, picked)} /></td>
                <td className="nowrap">
                  {d.binaryUrl
                    ? <a className="open-link" href={api.binaryUrl(d)} target="_blank" rel="noreferrer" title={`Binary: ${d.binaryUrl}`}>Open</a>
                    : <span className="muted small">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager pager={pager} noun="records" />
    </div>
  );
}

const VISIT_KINDS = [
  { id: "all", label: "All", classes: null },
  { id: "inpatient", label: "Inpatient", classes: ["IMP", "ACUTE", "NONAC", "OBSENC", "SS"] },
  { id: "ed", label: "ED", classes: ["EMER"] },
  { id: "outpatient", label: "Outpatient", classes: ["AMB", "VR", "HH", "FLD"] },
];

function encounterIds(doc) {
  return (doc.encounter || "").split(",").map((ref) => ref.trim().split("/").pop()).filter(Boolean);
}

// Records grouped by the visit / stay they belong to (DocumentReference.context.encounter).
function EncounterView({ run, packet, picked, onToggle, onLoadVisits, onLoadVisitDocs }) {
  const r = run.result;
  const [kind, setKind] = useState("all");
  const [open, setOpen] = useState(() => new Set());
  const [loading, setLoading] = useState(() => new Set());
  const [error, setError] = useState("");
  const [loadingVisits, setLoadingVisits] = useState(false);
  const hasEncounterRows = r.rows.some((x) => x.resource === "Encounter" && x.count > 0);
  const visitsLoaded = Array.isArray(run.visits);

  // Plans without Encounter groups: load the member's visits as soon as this view opens.
  const loadVisits = async () => {
    setLoadingVisits(true);
    setError("");
    try {
      await onLoadVisits();
    } catch (e) {
      setError(`Could not load visits: ${e.message}`);
    } finally {
      setLoadingVisits(false);
    }
  };
  useEffect(() => {
    if (!hasEncounterRows && !visitsLoaded && !loadingVisits && !error) loadVisits();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hasEncounterRows, visitsLoaded]);

  // Visits: from the plan's Encounter groups and/or loaded on demand.
  const byId = new Map();
  for (const row of r.rows.filter((x) => x.resource === "Encounter")) row.docs.forEach((v) => byId.set(v.id, v));
  (run.visits || []).forEach((v) => byId.set(v.id, v));
  // Newest first; visits without a date go last.
  const allVisits = [...byId.values()].sort((a, b) => {
    if (!a.start !== !b.start) return a.start ? -1 : 1;
    return (a.start || "") < (b.start || "") ? 1 : -1;
  });

  // Every note we know about, once: plan rows (in the packet), other records and visit notes.
  const notes = new Map();
  for (const row of r.rows.filter((x) => x.resource !== "Encounter")) {
    row.docs.filter((d) => !d.duplicate).forEach((d) => notes.set(d.id, { ...d, inPlan: true, rowLabel: row.label }));
  }
  extraDocs(run).forEach((d) => { if (!notes.has(d.id)) notes.set(d.id, d); });
  const notesByVisit = new Map();
  const unlinked = [];
  for (const d of notes.values()) {
    const ids = encounterIds(d).filter((id) => byId.has(id));
    if (!ids.length) unlinked.push(d);
    ids.forEach((id) => notesByVisit.set(id, [...(notesByVisit.get(id) || []), d]));
  }

  const kindOf = (v) => VISIT_KINDS.find((k) => k.classes?.includes(v.class))?.id || "other";
  // Blank encounters (no date and no notes, e.g. open registrations) are not shown.
  const visits = allVisits.filter((v) => v.start || (notesByVisit.get(v.id) || []).length);
  // One facility per record: inpatient stays whose notes are going in the packet.
  const packetFacilities = [...new Set(visits
    .filter((v) => kindOf(v) === "inpatient" && (notesByVisit.get(v.id) || []).some((d) => d.inPlan || picked.has(d.id)))
    .map((v) => v.facility || "unknown facility"))];
  const shown = visits.filter((v) => kind === "all" || kindOf(v) === kind);
  const pager = usePager(shown);
  const locked = packet?.state === "running";

  const toggleOpen = (id) => setOpen((o) => { const n = new Set(o); n.has(id) ? n.delete(id) : n.add(id); return n; });
  const fetchVisit = async (id) => {
    setError("");
    setLoading((l) => new Set(l).add(id));
    try {
      await onLoadVisitDocs(id);
      setOpen((o) => new Set(o).add(id));
    } catch (e) {
      setError(`Could not get the notes for visit ${id}: ${e.message}`);
    } finally {
      setLoading((l) => { const n = new Set(l); n.delete(id); return n; });
    }
  };

  if (!visits.length) {
    const w = r.window || {};
    return (
      <div className="tab-panel">
        {loadingVisits || (!hasEncounterRows && !visitsLoaded && !error) ? (
          <div className="banner busy"><span className="spinner" aria-hidden="true" />Loading the member's visits and stays…</div>
        ) : error ? (
          <>
            <div className="banner error small">{error}</div>
            <button className="ghost" onClick={loadVisits}>Try again</button>
          </>
        ) : (
          <p className="hint">
            No visits or stays for this member{w.start ? ` from ${toUS(w.start)} to before ${toUS(w.end)}` : ""}.
            {w.start ? " Untick Apply Date Window and fetch again to look at every date." : ""}
          </p>
        )}
      </div>
    );
  }

  return (
    <div className="tab-panel">
      <div className="row wrap">
        <div className="seg" role="group" aria-label="Visit type">
          {VISIT_KINDS.map((k) => (
            <button key={k.id} className={kind === k.id ? "active" : ""} onClick={() => { setKind(k.id); pager.setPage(1); }}>
              {k.label} <span className="muted">{k.id === "all" ? visits.length : visits.filter((v) => kindOf(v) === k.id).length}</span>
            </button>
          ))}
        </div>
        <span className="hint">Expand a visit to see its notes; “Get all notes for this visit” asks Cerner for every note written for it.</span>
      </div>
      {run.visitsTruncated && (
        <div className="banner warn">
          Cerner's visit list for this member is longer than the pages read (much of it undated registrations).
          Every visit your fetched notes belong to is included regardless.
        </div>
      )}

      {packetFacilities.length > 1 && (
        <div className="banner warn">
          <b>Facility check:</b>&nbsp;the packet has inpatient notes from {packetFacilities.join(" and ")}. The request asks for one facility per record.
        </div>
      )}
      {error && <div className="banner error small">{error}</div>}

      <div className="other-groups">
        {pager.pageItems.map((v) => {
          const vNotes = (notesByVisit.get(v.id) || []).sort((a, b) => ((a.date || "") < (b.date || "") ? 1 : -1));
          const isOpen = open.has(v.id);
          const loaded = run.visitDocsLoaded?.[v.id];
          return (
            <div key={v.id} className={`other-group ${vNotes.some((d) => d.inPlan || picked.has(d.id)) ? "has-picked" : ""}`}>
              <div className="other-head">
                <button className="other-title" onClick={() => toggleOpen(v.id)} aria-expanded={isOpen}>
                  <span className="trace-caret">{isOpen ? "▾" : "▸"}</span>
                  <span className="mono">{v.start ? toUS(v.start) : "—"}{v.end && v.end !== v.start ? ` – ${toUS(v.end)}` : ""}</span>
                  <span className="chip">{v.class || "?"}</span>
                  <b>{v.type || v.classDisplay || "Visit"}</b>
                  <span className="muted">{v.facility || "unknown facility"}</span>
                </button>
                <span className="tab-count" title="Notes linked to this visit">{vNotes.length}</span>
              </div>
              {isOpen && (
                <div className="other-docs">
                  <div className="row">
                    <button className="ghost small-btn" disabled={loading.has(v.id)} onClick={() => fetchVisit(v.id)}>
                      {loading.has(v.id) ? "Getting notes…" : loaded ? "Get all notes again" : "Get all notes for this visit"}
                    </button>
                    {loaded && (
                      <span className={`hint ${loaded.truncated ? "warn-text" : ""}`}>
                        {loaded.truncated ? `Showing the first ${loaded.ids.length} notes; this visit has more.` : `Cerner has ${loaded.ids.length} notes for this visit.`}
                      </span>
                    )}
                    <span className="mono muted small">Encounter {v.id}{v.status ? ` · ${v.status}` : ""}</span>
                  </div>
                  {vNotes.length === 0 ? <p className="hint">No notes linked to this visit yet.</p> : (
                    <VisitNotes notes={vNotes} packet={packet} picked={picked} onToggle={onToggle} locked={locked} />
                  )}
                </div>
              )}
            </div>
          );
        })}
      </div>
      <Pager pager={pager} noun="visits" />

      {unlinked.length > 0 && (
        <div className="other-group unlinked">
          <div className="other-head">
            <button className="other-title" onClick={() => toggleOpen("__unlinked")} aria-expanded={open.has("__unlinked")}>
              <span className="trace-caret">{open.has("__unlinked") ? "▾" : "▸"}</span>
              <b>Not linked to a listed visit</b>
              <span className="muted">notes with no encounter, or a visit outside the window</span>
            </button>
            <span className="tab-count">{unlinked.length}</span>
          </div>
          {open.has("__unlinked") && (
            <div className="other-docs"><VisitNotes notes={unlinked} packet={packet} picked={picked} onToggle={onToggle} locked={locked} /></div>
          )}
        </div>
      )}
    </div>
  );
}

function VisitNotes({ notes, packet, picked, onToggle, locked }) {
  const pager = usePager(notes);
  return (
    <>
      <div className="table-wrap">
        <table className="doc-table">
          <colgroup>
            <col className="col-check" /><col className="col-date" /><col /><col className="col-author" /><col className="col-status" /><col className="col-file" />
          </colgroup>
          <thead><tr><th></th><th>Date</th><th>Title</th><th>Type</th><th>Status</th><th>File</th></tr></thead>
          <tbody>
            {pager.pageItems.map((d) => (
              <tr key={d.id} className={picked.has(d.id) ? "picked" : ""}>
                <td>
                  {d.inPlan
                    ? <span className="in-plan" title="In the packet through the plan">✓</span>
                    : <input type="checkbox" aria-label={`Add ${d.title} to the packet`} disabled={locked || !d.binaryUrl}
                        checked={picked.has(d.id)} onChange={(e) => onToggle([d.id], e.target.checked)} />}
                </td>
                <td className="mono nowrap">{d.date ? toUS(d.date) : "undated"}</td>
                <td><b>{d.title || "(untitled)"}</b></td>
                <td className="muted">{d.rowLabel || (d.loinc?.length ? `LOINC ${d.loinc[0]}` : "no LOINC")}</td>
                <td><StatusChip {...downloadStatus(d, packet, picked)} /></td>
                <td className="nowrap">
                  {d.binaryUrl
                    ? <a className="open-link" href={api.binaryUrl(d)} target="_blank" rel="noreferrer" title={`Binary: ${d.binaryUrl}`}>Open</a>
                    : <span className="muted small">—</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
      <Pager pager={pager} noun="notes" />
    </>
  );
}

function ResultsTab({ r }) {
  const pager = usePager(r.observations);
  return (
    <div>
      {r.observationError && <div className="banner error small">{r.observationError}</div>}
      {!r.observationError && r.observations.length === 0 && <div className="banner warn">No results found in this window.</div>}
      {r.observations.length > 0 && (
        <>
          {r.observationsTruncated && <div className="banner warn">Showing the first {r.observations.length}; Cerner has more. Narrow the date window or raise FHIR_MAX_PAGES.</div>}
          <p className="hint">Newest first. The highlighted row is the latest: skip it if it was taken in the ED, during an inpatient stay or on a procedure day, and use the next one.</p>
          <div className="table-wrap">
            <table>
              <thead><tr><th>Date</th><th>Result</th><th>Value</th><th>By</th></tr></thead>
              <tbody>
                {pager.pageItems.map((o, i) => (
                  <tr key={o.id} className={pager.offset + i === 0 ? "picked" : ""}>
                    <td className="mono nowrap">{toUS(o.date)}</td><td>{o.name}</td><td><b>{o.value}</b></td><td>{o.performer}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <Pager pager={pager} noun="results" />
        </>
      )}
    </div>
  );
}

function PacketFiles({ files, packet }) {
  const pager = usePager(files);
  return (
    <>
      <div className="table-wrap">
        <table className="doc-table">
          <colgroup><col /><col className="col-status" /></colgroup>
          <thead><tr><th>File</th><th>Status</th></tr></thead>
          <tbody>
            {pager.pageItems.map((f) => {
              const s = packet?.files?.[f];
              // The two sheets are written into the zip at the end, not downloaded.
              const sheetStatus = packet?.state === "ready" ? "done" : packet ? "queued" : "ready";
              const status = s ? s.status : sheetStatus;
              return (
                <tr key={f}>
                  <td className="mono">{f}</td>
                  <td><StatusChip status={status} error={s?.error} /></td>
                </tr>
              );
            })}
          </tbody>
        </table>
      </div>
      <Pager pager={pager} noun="files" />
    </>
  );
}

function PacketProgress({ packet, onSaveAgain }) {
  const c = packet.counts || {};
  const total = Object.keys(packet.files || {}).length;
  const finished = (c.done || 0) + (c.failed || 0);
  const pct = total ? Math.round((finished / total) * 100) : 100;
  const elapsed = (Date.now() - packet.startedAt) / 1000;
  const eta = finished > 0 && packet.state === "running" ? Math.round((elapsed / finished) * (total - finished)) : null;
  return (
    <div className="progress">
      <div className="progress-head">
        <span>
          {packet.state === "running" && `Downloading files in the background: ${finished} of ${total}`}
          {packet.state === "ready" && `Packet ready: ${packet.zipName}`}
          {packet.state === "failed" && <span className="bad">Packet failed: {packet.error}</span>}
        </span>
        <span className="mono">
          {eta != null ? `~${eta >= 60 ? `${Math.ceil(eta / 60)} min` : `${eta}s`} left · ` : ""}{pct}%
        </span>
      </div>
      <div className="progress-bar" role="progressbar" aria-valuemin={0} aria-valuemax={100} aria-valuenow={pct}>
        <span style={{ width: `${pct}%` }} />
      </div>
      <div className="progress-codes">
        {["queued", "downloading", "done", "failed"].filter((k) => c[k]).map((k) => (
          <span key={k} className={`dl dl-${k}`}>{DL_LABEL[k]} {c[k]}</span>
        ))}
        {packet.state === "ready" && <button className="ghost" onClick={onSaveAgain}>Save the zip again</button>}
      </div>
    </div>
  );
}

function Results({ run, packet, onPacket, onToggleOther, onLoadVisits, onLoadVisitDocs, rps }) {
  const r = run.result;
  const [tab, setTab] = useState("overview");
  const [view, setView] = useState("type"); // "type" = tabs by document type, "encounter" = by visit
  const picked = new Set(run.otherSelected || []);
  const otherDocs = (r.other || []).flatMap((g) => g.docs);
  const pickedDocs = extraDocs(run).filter((d) => picked.has(d.id) && d.binaryUrl);
  const notes = [...r.rows.flatMap((row) => row.docs.filter((d) => !d.duplicate && d.binaryUrl)), ...pickedDocs];
  const files = ["00_biographical-sheet.txt", ...[...new Set(notes.map((d) => d.file))].sort(), "missing-items.txt"];
  const missing = r.rows.filter((row) => row.count === 0 && !row.error).length;
  const minutes = rps > 0 ? Math.ceil(notes.length / rps / 60) : 0;
  const docRows = r.rows.filter((row) => row.code !== "clinical-note");
  const recordRows = docRows.filter((row) => row.resource !== "Encounter"); // visits live in the encounter view
  const sweepError = r.rows.find((row) => row.code === "clinical-note");
  const current = recordRows.find((row) => row.code === tab);
  const packetRunning = packet?.state === "running";
  const packetLabel = packetRunning ? "Downloading…" : packet?.state === "ready" ? "Download packet again" : "Download packet (.zip)";

  // Overview rows: every planned code, then other records and results. Paginated like every table.
  const overviewItems = [
    ...run.progress.codes.map((c) => ({ key: c.code, render: () => {
      const row = docRows.find((x) => x.code === c.code);
      const isVisit = row?.resource === "Encounter";
      const latest = row?.docs.map((d) => d.date || d.start).filter(Boolean).sort().pop();
      const open = () => { if (!row?.count) return; if (isVisit) setView("encounter"); else setTab(c.code); };
      return (
        <tr key={c.code} className={`${row?.count ? "clickable" : ""} ${row && row.count === 0 ? "missing" : ""}`} onClick={open}
          title={isVisit && row?.count ? "Open the encounter view" : undefined}>
          <td className="mono code">{c.code}</td>
          <td>{c.label}</td>
          <td>{row?.required ? <span className="chip warn">required</span> : <span className="muted small">{row ? "optional" : ""}</span>}</td>
          <td>
            {!row ? <span className="muted">waiting…</span>
              : row.error ? <span className="bad" title={row.error}>error</span>
              : row.count ? (row.truncated ? <span title="More exist: raise FHIR_MAX_PAGES or narrow the date window">{row.count}+</span> : row.count)
              : <span className="bad">missing</span>}
          </td>
          <td className="mono">{latest ? toUS(latest) : ""}</td>
        </tr>
      );
    } })),
    ...(run.progress.discover ? [{ key: "other", render: () => (
      <tr key="other" className={otherDocs.length ? "clickable" : ""} onClick={() => otherDocs.length && setTab("other")}>
        <td className="mono code">other</td>
        <td>Other records (not in the plan)</td>
        <td></td>
        <td>
          {r.otherError ? <span className="bad" title={r.otherError}>error</span>
            : r.other ? `${otherDocs.length} in ${r.other.length} types` : <span className="muted">waiting…</span>}
        </td>
        <td className="muted">{r.other ? (pickedDocs.length ? `${pickedDocs.length} added to the packet` : "Not downloaded") : ""}</td>
      </tr>
    ) }] : []),
    ...(run.progress.observations ? [{ key: "results", render: () => (
      <tr key="results" className={`${r.observations.length ? "clickable" : ""} ${run.done && !r.observations.length ? "missing" : ""}`}
        onClick={() => r.observations.length && setTab("results")}>
        <td className="mono code">results</td>
        <td>Observations (BP / HbA1c / GMI)</td>
        <td>{run.plan.observations.some((o) => o.required) ? <span className="chip warn">required</span> : <span className="muted small">optional</span>}</td>
        <td>{r.observationError ? <span className="bad">error</span> : run.done || r.observations.length ? r.observations.length : <span className="muted">waiting…</span>}</td>
        <td className="mono">{r.observations[0] ? `${toUS(r.observations[0].date)} · ${r.observations[0].value}` : ""}</td>
      </tr>
    ) }] : []),
  ];
  const overviewPager = usePager(overviewItems);

  const tabs = [
    { id: "overview", label: "Overview" },
    // Only tabs with at least one record; codes with nothing found stay listed in Overview.
    ...recordRows.filter((row) => row.count > 0).map((row) => ({ id: row.code, label: row.label, count: row.truncated ? `${row.count}+` : row.count })),
    ...(otherDocs.length > 0 ? [{ id: "other", label: "Other records", count: pickedDocs.length ? `${pickedDocs.length}/${otherDocs.length}` : otherDocs.length, state: "other" }] : []),
    ...(r.observations.length > 0 ? [{ id: "results", label: "Results", count: r.observations.length }] : []),
    { id: "packet", label: "Packet", count: run.done ? files.length : undefined },
  ];

  return (
    <div className="results">
      <div className="summary">
        <div className="summary-text">
          <div><b>{r.patient.name}</b> · DOB {toUS(r.patient.birthDate)} · Patient/{r.patient.id}</div>
          <div className="stats">
            <span className="stat"><span className="stat-value">{notes.length}</span> notes</span>
            <span className="stat-divider" />
            <span className="stat"><span className={`stat-value ${missing ? "bad" : ""}`}>{missing}</span> codes missing</span>
            <span className="stat-divider" />
            <span className="stat mono">{r.window.start ? `${toUS(r.window.start)} → before ${toUS(r.window.end)}` : "no date filter"}</span>
          </div>
        </div>
        <button className="primary" onClick={onPacket} disabled={!run.done || packetRunning}>{packetLabel}</button>
      </div>

      {!run.done && <Progress progress={run.progress} />}
      {packet && <PacketProgress packet={packet} onSaveAgain={() => api.packetSave(packet.job)} />}

      <div className="view-switch row">
        <div className="seg" role="group" aria-label="Group results">
          <button className={view === "type" ? "active" : ""} onClick={() => setView("type")}>By document type</button>
          <button className={view === "encounter" ? "active" : ""} onClick={() => setView("encounter")} disabled={!run.done}
            title={run.done ? "" : "Available once the fetch finishes"}>By encounter</button>
        </div>
        <span className="hint">{view === "type" ? "Records grouped by what they are." : "Records grouped by the visit or stay they belong to."}</span>
      </div>

      {view === "encounter" ? (
        <EncounterView run={run} packet={packet} picked={picked} onToggle={onToggleOther}
          onLoadVisits={onLoadVisits} onLoadVisitDocs={onLoadVisitDocs} />
      ) : (<>
      <div className="tabs" role="tablist">
        {tabs.map((t) => (
          <button key={t.id} role="tab" aria-selected={tab === t.id}
            className={`tab ${tab === t.id ? "active" : ""} ${t.state || ""}`} onClick={() => setTab(t.id)}>
            {t.label}
            {t.count !== undefined && <span className="tab-count">{t.count}</span>}
          </button>
        ))}
      </div>

      <div className="tab-panel" role="tabpanel">
        {tab === "overview" && (
          <>
            {sweepError && <div className="banner error small">Title matching failed: {sweepError.error}</div>}
            {r.gaps?.length > 0 && (
              <div className="banner error gaps">
                <div>
                  <b>Elements not in the record (required):</b>
                  <ul>{r.gaps.map((g) => <li key={g.purpose}>{g.notation}</li>)}</ul>
                  {r.fallbackNote && <div className="small">{r.fallbackNote}</div>}
                </div>
              </div>
            )}
            {r.facilityWarning && (
              <div className="banner warn">
                <div>
                  <b>Facility check:</b> {r.facilityWarning.message}{" "}
                  {Object.entries(r.facilityWarning.facilities).map(([f, n]) => `${f} (${n})`).join(", ")}
                </div>
              </div>
            )}
            <div className="table-wrap">
              <table>
                <thead><tr><th>Code</th><th>What</th><th>Required</th><th>Found</th><th>Latest</th></tr></thead>
                <tbody>
                  {overviewPager.pageItems.map((item) => item.render())}
                </tbody>
              </table>
            </div>
            <Pager pager={overviewPager} noun="rows" />
            <p className="hint">Click a row or a tab to see its records; visits and stays open the encounter view. Codes with nothing found have no tab; they are listed here and on the missing-items sheet.</p>
          </>
        )}

        {current && <DocTab key={current.code} row={current} packet={packet} />}

        {tab === "other" && <OtherTab r={r} done={run.done} packet={packet} picked={picked} onToggle={onToggleOther} />}

        {tab === "results" && <ResultsTab r={r} />}

        {tab === "packet" && (
          <div>
            {!run.done ? <p className="hint">The packet is ready once every code has been fetched.</p> : (
              <>
                <div className="row">
                  <button className="primary" onClick={onPacket} disabled={packetRunning}>{packetLabel}</button>
                  <span className="hint">
                    {files.length} files{!packet && minutes > 1 ? ` · roughly ${minutes}–${minutes * 2} min on the sandbox; it runs in the background` : ""}
                  </span>
                </div>
                <PacketFiles files={files} packet={packet} />
              </>
            )}
          </div>
        )}
      </div>
      </>)}
    </div>
  );
}
