import React, { useEffect, useState } from "react";
import { api } from "./api.js";

const SAMPLES = {
  TRC: "TRC - REQUIRED - Member demographic sheet: Name and date of birth, 2026 Evidence of discharge medication reconciliations, medication lists and medication reviews, 2026 Hospital discharge summaries, 2026 Hospital discharge instructions, 2026 Documentation of notification of inpatient admissions or evidence of admission orders, 2026 Specialist consultation reports, 2026 Progress notes and telehealth's. Require inpatient and outpatient records. Note: Inpatient notes should be only from one facility per record.",
  CBP: "CBP - REQUIRED - Member demographic sheet: Name and date of birth. Last blood pressure reading in 2026 – may be from a vital signs flowsheet or a progress note. Please note: Provider name must be included on documentation. Telehealth and patient reported BP is acceptable.",
  GSD: "GSD - REQUIRED - Member demographic sheet: Name and date of birth. Latest 2025 hemoglobin A1c (HbA1c), GMI value from 2025 lab result or progress note. Referrals, specialist or consultation notes. Hospital laboratory records, Progress notes.",
  "All records": "All medical records from 1/1/2025 through 2026 YTD including Member Biographical Information Sheet",
};

// Test patients that have data, per FHIR server. All made-up sandbox/mock data.
const DEMO_PATIENTS = {
  sandbox: [
    { id: "12724066", label: "Nancy Patchtest", hint: "Full record: ~990 progress notes, H&P, consults, discharge summaries, BP. Best for TRC / All records." },
    { id: "12742400", label: "Tim Peters", hint: "Progress notes + BP. Quick CBP demo with a small packet." },
    { id: "12744891", label: "Wilma Smart", hint: "Progress notes + BP, born 1950. CBP demo." },
    { id: "12744580", label: "John Smart", hint: "Progress notes, no BP: results show up as missing." },
    { id: "12744734", label: "Kelli Peters", hint: "Almost empty record: shows the missing-items sheet." },
  ],
  mock: [
    { id: "12724066", label: "Jane Doe", hint: "Offline mock: 8 notes and 3 BP readings, all in 2026. Keep the date window on." },
  ],
};

function demoPatientsFor(fhirBase = "") {
  if (fhirBase.includes("/r4/mock")) return DEMO_PATIENTS.mock;
  if (fhirBase.includes("fhir-open.cerner.com")) return DEMO_PATIENTS.sandbox;
  return [];
}

const checkedOnly =(items) => items.filter((i) => i.on).map(({ on, ...rest }) => rest);

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
  const [run, setRun] = useState(null); // { plan: plan as sent, result }
  const [busy, setBusy] = useState("");
  const [error, setError] = useState("");
  const [info, setInfo] = useState("");

  useEffect(() => {
    api.health()
      .then((h) => {
        setHealth(h);
        setUseLlm(h.llm);
      })
      .catch((e) => setError(`Backend not reachable: ${e.message}`));
    api.catalog().then(setCatalog).catch(() => {});
  }, []);

  async function step(label, fn) {
    setError("");
    setInfo("");
    setBusy(label);
    try {
      await fn();
    } catch (e) {
      setError(e.message);
    } finally {
      setBusy("");
    }
  }

  const translate = () =>
    step(useLlm ? "Asking Azure OpenAI to read the service note…" : "Applying keyword rules…", async () => {
      const p = await api.plan(note, useLlm);
      const on = (items) => items.map((i) => ({ ...i, on: true }));
      setPlan({ ...p, documents: on(p.documents), observations: on(p.observations) });
      setRun(null);
    });

  const findPatients = (query = name, birthdate = dob) =>
    step("Searching patients…", async () => {
      const list = await api.patients(query.trim(), birthdate);
      setPatients(list);
      setPatientId(list.length === 1 ? list[0].id : "");
    });

  const fetchAll = () =>
    step("Requesting each LOINC code from Cerner…", async () => {
      const sent = { ...plan, documents: checkedOnly(plan.documents), observations: checkedOnly(plan.observations) };
      const result = await api.run({
        patient_id: patientId, plan: sent, apply_dates: applyDates, title_fallback: titleFallback,
      });
      setRun({ plan: sent, result });
    });

  const downloadPacket = () =>
    step("Downloading every note and building the packet…", async () => {
      const failed = await api.packet(run.plan, run.result);
      if (failed) setInfo(`Packet saved. ${failed} file(s) failed to download; see download-errors.txt in the zip.`);
    });

  const toggle = (kind, code) =>
    setPlan({ ...plan, [kind]: plan[kind].map((i) => (i.code === code ? { ...i, on: !i.on } : i)) });

  const addCode = (kind, code) => {
    if (!code || plan[kind].some((i) => i.code === code)) return;
    const hit = catalog[kind].find((c) => c.code === code);
    setPlan({ ...plan, [kind]: [...plan[kind], { code, label: hit?.label || code, reason: "added by you", on: true }] });
  };

  const isPatientId = /^\d+$/.test(name.trim());
  const needsDob = name.trim() !== "" && !isPatientId && !dob;
  const canSearch = !busy && name.trim() !== "" && !needsDob;
  const selected = plan ? plan.documents.filter((d) => d.on).length + plan.observations.filter((o) => o.on).length : 0;

  return (
    <div className="shell">
      <header className="topbar">
        <div className="brand">
          <img className="brand-wordmark" src="/advantmed-wordmark.svg" alt="Advantmed" />
          <div>
            <strong>FHIR Record Retrieval</strong>
            <p>Service note → LOINC → Cerner records</p>
          </div>
        </div>
        {health && (
          <div className="topbar-meta">
            <span className="chip mono" title="FHIR base URL">FHIR: {health.fhirBase}</span>
            <span className={`chip ${health.llm ? "ok" : "warn"}`}>
              {health.llm
                ? `Azure OpenAI · ${health.llmDeployment}${health.llmTemperature != null ? ` · t=${health.llmTemperature}` : ""}`
                : "Azure OpenAI not configured"}
            </span>
          </div>
        )}
      </header>

      <main className="page">
        <div className="intro">
          <h1>Chart-chase retrieval</h1>
          <p>Paste a chart-chase service note, pick the member, and pull every matching note from Cerner.</p>
        </div>

        {error && <div className="banner error" role="alert">{error}</div>}
        {info && <div className="banner warn">{info}</div>}
        {busy && <div className="banner busy"><span className="spinner" aria-hidden="true" />{busy}</div>}

        <section className="card">
          <h2><span className="num">1</span> Service note → LOINC plan</h2>
          <div className="row">
            <span className="label">Sample</span>
            {Object.keys(SAMPLES).map((k) => (
              <button key={k} className={`ghost ${note === SAMPLES[k] ? "active" : ""}`} onClick={() => setNote(SAMPLES[k])}>
                {k}
              </button>
            ))}
          </div>
          <textarea rows={5} value={note} onChange={(e) => setNote(e.target.value)} aria-label="Service note" />
          <div className="row">
            <label className="check" title={health && !health.llm ? "Set AOAI_ENDPOINT and AOAI_DEPLOYMENT in backend/.env" : ""}>
              <input type="checkbox" checked={useLlm} onChange={(e) => setUseLlm(e.target.checked)} />
              Use Azure OpenAI
            </label>
            <button className="primary" onClick={translate} disabled={!!busy || !note.trim()}>Translate to LOINC</button>
            {!useLlm && <span className="hint">Keyword rules only</span>}
          </div>

          {plan && (
            <div className="plan">
              <div className="row wrap">
                <span className={`chip ${plan.source === "azure-openai" ? "ok" : ""}`}>
                  {plan.source === "azure-openai" ? "Mapped by Azure OpenAI" : "Mapped by keyword rules"}
                </span>
                <span className="chip">Measures: {plan.measures.join(", ") || "generic"}</span>
                <span className="label">From</span>
                <input type="date" value={plan.start} onChange={(e) => setPlan({ ...plan, start: e.target.value })} />
                <span className="label">to before</span>
                <input type="date" value={plan.end} onChange={(e) => setPlan({ ...plan, end: e.target.value })} />
              </div>
              {plan.notes && <p className="hint">{plan.notes}</p>}

              <PlanTable title="Documents (DocumentReference.type)" kind="documents" items={plan.documents}
                options={catalog.documents} onToggle={toggle} onAdd={addCode} />
              <PlanTable title="Results (Observation.code)" kind="observations" items={plan.observations}
                options={catalog.observations} onToggle={toggle} onAdd={addCode} />
              <p className="hint">Also requested: <b>Patient</b> (biographical sheet).</p>
            </div>
          )}
        </section>

        <section className={`card ${plan ? "" : "dim"}`}>
          <h2><span className="num">2</span> Find the member</h2>
          {demoPatientsFor(health?.fhirBase).length > 0 && (
            <div className="row">
              <span className="label">Try</span>
              {demoPatientsFor(health?.fhirBase).map((p) => (
                <button key={p.id + p.label} title={p.hint} disabled={!!busy}
                  className={`ghost ${name === p.id ? "active" : ""}`}
                  onClick={() => { setName(p.id); setDob(""); findPatients(p.id, ""); }}>
                  {p.label} <span className="mono muted">{p.id}</span>
                </button>
              ))}
            </div>
          )}
          {demoPatientsFor(health?.fhirBase).find((p) => p.id === name) && (
            <p className="hint">{demoPatientsFor(health?.fhirBase).find((p) => p.id === name).hint}</p>
          )}
          <div className="row wrap">
            <input className="grow" placeholder="Patient name or Patient ID" value={name}
              onChange={(e) => setName(e.target.value)}
              onKeyDown={(e) => e.key === "Enter" && canSearch && findPatients()} />
            <input type="date" value={dob} onChange={(e) => setDob(e.target.value)}
              className={needsDob ? "invalid" : ""} aria-invalid={needsDob} required={!isPatientId}
              title={isPatientId ? "Date of birth (optional with a Patient ID)" : "Date of birth (required with a name)"} />
            <button className="primary" onClick={() => findPatients()} disabled={!canSearch}>Search</button>
          </div>
          <p className={`hint ${needsDob ? "warn-text" : ""}`}>
            {needsDob
              ? "Date of birth is required when searching by name."
              : "Search by Patient ID, or by name + date of birth."}
          </p>
          {patients && patients.length === 0 && <p className="hint">No patients found.</p>}
          {patients && patients.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th className="narrow"></th><th>Name</th><th>DOB</th><th>Sex</th><th>Patient ID</th></tr></thead>
                <tbody>
                  {patients.map((p) => (
                    <tr key={p.id} className={`clickable ${p.id === patientId ? "picked" : ""}`} onClick={() => setPatientId(p.id)}>
                      <td><input type="radio" name="patient" checked={p.id === patientId} onChange={() => setPatientId(p.id)} /></td>
                      <td>{p.name}</td><td className="mono">{p.birthDate}</td><td>{p.gender}</td><td className="mono">{p.id}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <section className={`card ${plan && patientId ? "" : "dim"}`}>
          <h2><span className="num">3</span> Request, download, package</h2>
          <div className="row wrap">
            <label className="check">
              <input type="checkbox" checked={applyDates} onChange={(e) => setApplyDates(e.target.checked)} />
              Apply date window
            </label>
            <label className="check">
              <input type="checkbox" checked={titleFallback} onChange={(e) => setTitleFallback(e.target.checked)} />
              Also match notes by title (catches notes with no LOINC)
            </label>
            <button className="primary" onClick={fetchAll} disabled={!!busy || !plan || !patientId || selected === 0}>
              Fetch &amp; download ({selected} codes)
            </button>
          </div>
          <p className="hint">Sandbox data is often older than the request window — untick “Apply date window” if nothing comes back.</p>

          {run && <Results r={run.result} onPacket={downloadPacket} busy={!!busy} />}
        </section>
      </main>
    </div>
  );
}

function PlanTable({ title, kind, items, options, onToggle, onAdd }) {
  return (
    <div className="plan-block">
      <h3>{title}</h3>
      {items.length === 0 ? <p className="hint">None requested.</p> : (
        <div className="table-wrap">
          <table>
            <thead><tr><th className="narrow"></th><th>LOINC</th><th>What</th><th>Why</th></tr></thead>
            <tbody>
              {items.map((i) => (
                <tr key={i.code} className={i.on ? "" : "off"}>
                  <td><input type="checkbox" checked={i.on} onChange={() => onToggle(kind, i.code)} aria-label={`Include ${i.code}`} /></td>
                  <td className="mono code">{i.code}</td><td>{i.label}</td><td className="muted">{i.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
      )}
      <select value="" onChange={(e) => onAdd(kind, e.target.value)}>
        <option value="">+ Add a code…</option>
        {options.filter((o) => !items.some((i) => i.code === o.code)).map((o) => (
          <option key={o.code} value={o.code}>{o.code} · {o.label}</option>
        ))}
      </select>
    </div>
  );
}

function Results({ r, onPacket, busy }) {
  const notes = r.rows.flatMap((row) => row.docs.filter((d) => !d.duplicate && d.binaryUrl));
  const files = ["00_biographical-sheet.txt", ...[...new Set(notes.map((d) => d.file))].sort(), "missing-items.txt"];
  return (
    <div className="results">
      <div className="summary">
        <div className="summary-text">
          <div><b>{r.patient.name}</b> · DOB {r.patient.birthDate} · Patient/{r.patient.id}</div>
          <div className="stats">
            <span className="stat"><span className="stat-value">{notes.length}</span> notes</span>
            <span className="stat-divider" />
            <span className="stat"><span className={`stat-value ${r.missing.length ? "bad" : ""}`}>{r.missing.length}</span> codes missing</span>
            <span className="stat-divider" />
            <span className="stat mono">{r.window.start ? `${r.window.start} → before ${r.window.end}` : "no date filter"}</span>
          </div>
        </div>
        <button className="primary" onClick={onPacket} disabled={busy}>Download packet (.zip)</button>
      </div>

      <h3>Documents by LOINC call</h3>
      <div className="table-wrap">
        <table>
          <thead><tr><th>LOINC</th><th>What</th><th>Found</th><th>Notes</th></tr></thead>
          <tbody>
            {r.rows.map((row) => (
              <tr key={row.code} className={row.count === 0 ? "missing" : ""}>
                <td className="mono code">{row.code}</td>
                <td>{row.label}</td>
                <td>{row.error ? <span className="bad">error</span> : row.count || <span className="bad">missing</span>}</td>
                <td>
                  {row.error && <div className="bad small">{row.error}</div>}
                  {row.docs.map((d) => (
                    <div key={d.id + row.code} className="doc">
                      <span className="mono">{d.date || "undated"}</span>
                      <b>{d.title || "(untitled)"}</b>
                      {d.author && <span className="muted">{d.author}</span>}
                      {d.docStatus && d.docStatus !== "final" && <span className="chip warn">{d.docStatus}</span>}
                      {d.matchedBy === "title" && <span className="chip warn">matched by title</span>}
                      {d.duplicate && <span className="chip">also under another code</span>}
                      {d.binaryUrl ? (
                        <a href={api.binaryUrl(d)} target="_blank" rel="noreferrer">open</a>
                      ) : (
                        <span className="muted small">no file</span>
                      )}
                    </div>
                  ))}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {(r.observations.length > 0 || r.observationError) && (
        <>
          <h3>Results (newest first)</h3>
          {r.observationError && <div className="bad small">{r.observationError}</div>}
          {r.observations.length > 0 && (
            <div className="table-wrap">
              <table>
                <thead><tr><th>Date</th><th>Result</th><th>Value</th><th>By</th></tr></thead>
                <tbody>
                  {r.observations.map((o, i) => (
                    <tr key={o.id} className={i === 0 ? "picked" : ""}>
                      <td className="mono">{o.date}</td><td>{o.name}</td><td><b>{o.value}</b></td><td>{o.performer}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
          <p className="hint">The top row is the latest. Skip it if it was taken in the ED, during an inpatient stay or on a procedure day, and use the next one.</p>
        </>
      )}

      <h3>Files in the packet</h3>
      <ul className="files">
        {files.map((f) => <li key={f} className="mono">{f}</li>)}
      </ul>
    </div>
  );
}
