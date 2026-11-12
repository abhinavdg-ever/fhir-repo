# FHIR Record Retrieval — Service Note → LOINC → Cerner

Paste a payer chart-chase **service note** and a **member**. The app:

Enter the service note and the member (name + date of birth, or Patient ID) together at the top, then:

1. **Plans** — a model on Azure AI Foundry (pick it in the Request card: GPT-4o, GPT-4.1 mini, Llama 4 Maverick, Mistral Small, Claude Haiku 4.5) turns the note into document LOINC codes, result LOINC codes and a date window. Every code is checked against the allow-list in `agent/doc_types.py`; anything else is dropped. Untick "Use Azure OpenAI" to use keyword rules instead (no silent fallback). The note is redacted with [phileas-redact](https://github.com/philterd/phileas-python) on this machine before it is sent (`agent/redact.py`): names, DOB and other dates, member IDs / MRNs, phone, email, address, ZIP, SSN become `[REDACTED-<type>]`; years and LOINC codes are kept. If redaction fails, nothing is sent.
2. **Finds the patient** in Cerner (`Patient?name=` or a Patient ID).
3. **Requests each LOINC code** — one `DocumentReference?patient=…&type=http://loinc.org|{code}&period=ge…&period=lt…` per code, plus one `Observation` search for BP / HbA1c / GMI.
4. **Packages** — streams each note from its `Binary` URL and builds a zip (notes + biographical sheet + missing-items sheet) **in memory**. Nothing is written to disk.

```
frontend (React/Vite) ──/api──▶ agent/app.py (FastAPI) ──▶ agent/planner.py ──▶ Azure OpenAI
                                                       └─▶ worker/main.py ──▶ worker/fhir_client.py ──▶ Cerner FHIR R4
```

## Layout

```
common/
  models.py        plan and request contracts (Pydantic)
agent/
  app.py           FastAPI app: routes, serves frontend/dist
  planner.py       service note -> plan (Azure OpenAI or keyword rules), validation, date window
  doc_types.py     allow-list of LOINC document/result codes, title keywords, keyword rules
worker/
  fhir_client.py   async Cerner client (paging, Binary download) + resource flatteners
  main.py          handle_job (per-LOINC searches) and build_packet (in-memory zip)
dev/
  mock_fhir.py     offline Cerner-shaped mock server
frontend/          React 18 + Vite UI (Advantmed styling)
tests/             pytest
requirements.txt
.env.example
```

## Deploy with Docker (VM)

One container serves the API and the UI. Needs Docker with the compose plugin on the VM.

```bash
git clone https://github.com/abhinavdg-ever/fhir-repo.git && cd fhir-repo
cp .env.example .env            # then set AOAI_ENDPOINT and AOAI_API_KEY
docker compose up -d --build
```

Open **http://<vm-ip>:1000**. Other port: `HOST_PORT=8080 docker compose up -d`.

| Task | Command |
| --- | --- |
| Logs | `docker compose logs -f app` |
| Connectivity check inside the container | `docker compose exec app python -m dev.check` |
| Update to the latest code | `git pull && docker compose up -d --build` |
| Apply `.env` changes | `docker compose restart app` |
| Stop | `docker compose down` |
| Offline mock instead of Cerner | set `FHIR_BASE=http://mock:9000/r4/mock` in `.env`, then `docker compose --profile mock up -d --build` |

Notes:

- `.env` is mounted read-only into the container (not baked into the image) and read by the app, so inline comments work. Create it **before** the first `up`, otherwise Docker creates an empty folder named `.env`.
- Open the port in the VM firewall / cloud security group (e.g. `sudo ufw allow 1000/tcp`).
- The container runs **one** uvicorn worker on purpose: packet jobs, the API trace and the Cerner rate limiter are in memory.
- Behind a corporate proxy, add `HTTPS_PROXY=http://proxy:port` and `NO_PROXY=localhost,127.0.0.1,mock` to `.env`.

## Run it

Needs **Python 3.10+**. Node is only needed to change the UI — the built UI in `frontend/dist` is committed and served by the API.

**Windows** (PowerShell or Command Prompt, from the repo folder):

```powershell
.\run.cmd            # first run creates .venv, installs requirements and .env; then serves the app
.\run.cmd -Dev       # auto-reload while editing code
.\run.cmd -Mock      # also start the offline mock FHIR server on :9000
.\run.cmd -Test      # run the tests
.\run.cmd -Check     # check this machine can reach Cerner (Patient, DocumentReference, Binary) and Azure OpenAI
```

`run.cmd` calls `run.ps1` with `-ExecutionPolicy Bypass`, so it works even where PowerShell blocks scripts. It finds Python through the `py` launcher (or `python` on PATH).

**macOS / Linux:** `./run.sh` (same options: `--dev`, `--mock`, `--test`, `--check`).

Then open **http://localhost:8000** and fill in `AOAI_ENDPOINT` / `AOAI_API_KEY` in `.env` to use Azure OpenAI.

Manual setup, if you prefer:

```powershell
python -m venv .venv
.venv\Scripts\activate           # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
copy .env.example .env            # macOS/Linux: cp .env.example .env
uvicorn agent.app:app --port 8000
```

### Deploying on a Windows machine

| Need | Do this |
| --- | --- |
| Others on the network open the app | `APP_HOST=0.0.0.0` in `.env`, and allow the port in Windows Defender Firewall: `New-NetFirewallRule -DisplayName "FHIR Retrieval" -Direction Inbound -Protocol TCP -LocalPort 8000 -Action Allow` (admin PowerShell) |
| A different port | `APP_PORT=8080` in `.env`, or `$env:APP_PORT="8080"; .\run.ps1` |
| `CERTIFICATE_VERIFY_FAILED` to Cerner or Azure | Corporate HTTPS inspection: set `USE_SYSTEM_CERTS=true` so the Windows certificate store is used |
| `curl.exe` fails with `CRYPT_E_NO_REVOCATION_CHECK (0x80092012)` | Windows curl (Schannel) can't reach the certificate authority's revocation server, often blocked on VMs. Add `--ssl-no-revoke` to curl. **The app is not affected** (Python doesn't do that online check); keep `USE_SYSTEM_CERTS=false` |
| A proxy | Set `HTTPS_PROXY=http://proxy:port` (and `NO_PROXY=localhost,127.0.0.1`) before starting; httpx and the OpenAI SDK honour them |
| Keep it running after logoff | Run `run.cmd` from Task Scheduler ("At startup", "Run whether user is logged on or not"), or wrap it with a service manager such as NSSM |
| Edit `.env` | Any editor is fine; save as UTF-8 (Notepad's default) |

Line endings are normalised by `.gitattributes`, so a Windows clone works without changes. The text sheets in the packet use Windows line endings.

UI work (Node 18+): start the backend first (`.\run.cmd -Dev`), then in a second window `cd frontend && npm install && npm run dev`, and open **http://127.0.0.1:5173**. It proxies `/api` to `http://127.0.0.1:8000` (IPv4 on purpose: on Windows `localhost` can resolve to IPv6 `::1`, which the backend doesn't listen on). Backend on another port: `$env:API_TARGET="http://127.0.0.1:8020"; npm run dev`. Run `npm run build` to refresh `frontend/dist`.

## Try Cerner with curl

The open sandbox needs no login. Patient `12742400` is Tim Peters (DOB 1990-09-15). macOS / Linux:

```bash
# Patient by ID, then by name + DOB
curl -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Patient/12742400"
curl -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Patient?family=Peters&given=Tim&birthdate=1990-09-15"

# History & physical notes (LOINC 34117-2) in a date window; the | is URL-encoded as %7C
curl -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/DocumentReference?patient=12742400&type=http://loinc.org%7C34117-2&_count=100&period=ge2025-01-01&period=lt2026-11-12"

# One note's file: the URL is content[].attachment.url from the search above.
# Accept must be the file's own type or application/fhir+json; anything else (e.g. */*) gets 406.
curl -H "Accept: application/pdf" -o note.pdf "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Binary/XR-208978749"

# Blood pressure results
curl -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Observation?patient=12742400&code=http://loinc.org%7C85354-9&_count=10"

# Every record the member has, any type (what "Other records" uses)
curl -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/DocumentReference?patient=12742400&_count=100"
```

**Windows** — always `curl.exe` (not `curl`) and always `--ssl-no-revoke` (Windows curl otherwise fails with `CRYPT_E_NO_REVOCATION_CHECK` when the revocation server is blocked, as on most VMs):

```powershell
curl.exe --ssl-no-revoke -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Patient/12742400"
curl.exe --ssl-no-revoke -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Patient?family=Peters&given=Tim&birthdate=1990-09-15"
curl.exe --ssl-no-revoke -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/DocumentReference?patient=12742400&type=http://loinc.org%7C34117-2&_count=100&period=ge2025-01-01&period=lt2026-11-12"
curl.exe --ssl-no-revoke -H "Accept: application/pdf" -o note.pdf "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Binary/XR-208978749"
curl.exe --ssl-no-revoke -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/Observation?patient=12742400&code=http://loinc.org%7C85354-9&_count=10"

# Every record the member has, any type (what "Other records" uses)
curl.exe --ssl-no-revoke -H "Accept: application/fhir+json" "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d/DocumentReference?patient=12742400&_count=100"
```

## Playing with the model (`.env`)

| Variable | What it does |
| --- | --- |
| `AOAI_ENDPOINT` | `https://<resource>.openai.azure.com` — empty/placeholder = LLM off |
| `AOAI_MODELS` | Foundry **deployment** names offered in the model picker, comma separated; the first is the default |
| `LLM_PRICES` | optional JSON price override, USD per 1M tokens: `{"gpt-4o": [2.5, 10, 1.25]}` (input, output, cached) |
| `LLM_REDACT` | `true` (default) redacts PII/PHI from the note before the LLM call; `false` sends it as typed |
| `LLM_REDACT_DATES` | `true` (default) also redacts full dates; years always stay |
| `AOAI_API_KEY` | key, or leave empty and use Entra ID (`pip install azure-identity`, `az login`) |
| `AOAI_TEMPERATURE` | default `0`; leave empty to send none (gpt-5 / o-series). If the model rejects temperature, the call is retried without it |
| `AOAI_TIMEOUT` | seconds per call, default `30` |

Restart uvicorn after editing `.env`. The header chip shows the active deployment and temperature.

## Cerner sandbox

`FHIR_BASE` defaults to the open sandbox `https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d` (no auth).

- Type `12724066` in the patient box to load that test patient. A name search needs the date of birth too, as mm/dd/yyyy (e.g. `Tim Peters` + 09/15/1990). Dates in the UI and packet sheets are mm/dd/yyyy.
- Sandbox notes are often outside the window — untick **Apply Date Window**.
- Tick **Match By Doc Name** to catch notes with no LOINC on `type`.
- `Binary` only accepts the file's own media type or `application/fhir+json` in `Accept`; anything else (e.g. a browser's `*/*`) gets **406 "The Accept Header is invalid"**. The app sends the exact type and falls back to FHIR JSON, so open notes through **Open** (which goes via `/api/binary`), not by pasting the Binary URL into a browser.
- The open sandbox rate-limits (HTTP 429). Requests are spaced out and retried automatically; tune it in `.env`:

| Variable | Default | What it does |
| --- | --- | --- |
| `FHIR_MAX_RPS` | `3` | requests per second to Cerner, shared by every search, page and download (`0` = no limit) |
| `FHIR_CONCURRENCY` | `2` | LOINC searches in flight at once |
| `FHIR_DOWNLOAD_CONCURRENCY` | `10` | Binary downloads in flight (each sandbox Binary call takes ~4 s whatever its size) |
| `FHIR_MAX_RETRIES` | `4` | retries on 429 / 502 / 503 / 504 and timeouts (honours `Retry-After`, else 1s, 2s, 4s, 8s) |
| `FHIR_TIMEOUT` | `60` | seconds per request |
| `FHIR_MAX_PAGES` | `10` | pages per search (100 notes each); a code that hits it shows as `1000+` |

Results stream into the UI as each LOINC call finishes (`POST /api/run/stream`) and are organised into tabs: Overview, one tab per document type (10 per page, with the exact DocumentReference request shown), Results (observations) and Packet.

The packet is a background job: each note's file is a separate `GET Binary/{id}` call (the DocumentReference's `content[].attachment.url`), so a large record takes a few minutes. The page stays usable meanwhile and each note's Status shows Queued → Downloading → Done / Failed. The zip is held in memory until saved.

**Agent output (clinical intent).** The agent — Azure OpenAI, or the keyword rules when it is off — returns one JSON object in this shape (see *Agent output (JSON)* in step 1):

```json
{
  "measure_codes": ["GSD"],
  "mode": "measure_specific",
  "date_windows": [
    {"purpose": "job_envelope", "from": "2025-01-01", "to": "2025-12-31"},
    {"purpose": "measurement_year", "from": "2025-01-01", "to": "2025-12-31"}
  ],
  "resource_groups": [
    {"resource": "Patient", "purpose": "demographics", "required": true},
    {"resource": "DiagnosticReport", "purpose": "lab_report", "required": true},
    {"resource": "Observation", "purpose": "hba1c_or_gmi", "required": true},
    {"resource": "DocumentReference", "purpose": "progress_notes", "required": false},
    {"resource": "DocumentReference", "purpose": "referrals_consults", "required": false}
  ],
  "clinical_targets": [{"kind": "hba1c", "any_history": false, "through": null}],
  "fallback_strategy": "strict",
  "notes": "GSD - REQUIRED - Member demographic sheet: Name and date of birth. Latest 2025 hemoglobin A1c..."
}
```

| Field | Values | What the app does with it |
| --- | --- | --- |
| `mode` | `measure_specific`, `all_records`, `general` | `all_records` puts every record in the packet |
| `date_windows` | `job_envelope` (always), `measurement_year` | documents, lab reports and encounters use the job envelope; results use the measurement year |
| `resource_groups` | `Patient`, `DocumentReference`, `Observation`, `DiagnosticReport`, `Encounter` + a purpose from `agent/doc_types.py` `PURPOSES` | each purpose maps to LOINC codes / a report category / encounter classes; `required` groups with nothing found are listed as gaps |
| `clinical_targets` | `hba1c`, `gmi`, `blood_pressure` | pick the result codes; `any_history` drops the lower date bound, `through` caps it |
| `fallback_strategy` | `strict`, `title_match`, `broad`, `progress_notes_if_missing` | sets *Match By Doc Name* / *List Other Records*; `progress_notes_if_missing` adds every progress note in the window when a required element is missing |

The output is validated before anything reaches Cerner: unknown groups and targets are dropped, dates are checked and never run past today, the job envelope stops at the last year the note names unless it says YTD / to date, and for a named measure the required groups are fixed by the measure (TRC: admission, discharge summary and instructions, medication reconciliation, progress notes; GSD: lab report and HbA1c/GMI; CBP and BPD: blood pressure).

**Encounters** give each note its facility; when inpatient notes come from more than one facility the UI and the missing-items sheet flag it (the request asks for one facility per record). **Lab reports** come from `DiagnosticReport?category=LAB`; their attached files go in the packet. The missing-items sheet starts with the required elements not in the record, in the note's own wording (e.g. "Discharge medications not reconciled").

**Document types.** `agent/doc_types.py` lists 24 document LOINC codes, including the ones Cerner actually puts on notes (e.g. `83805-2` admission note, `28623-7` nursing narrative, `28568-4` / `57053-1` ED physician / nurse, `34108-1` outpatient, `18761-7` transfer summary, `11504-8` operative report). Title keywords include Cerner's own titles ("Inpatient Clinical Summary" is a discharge summary).

**Match By Doc Name** (optional) catches notes Cerner never coded with LOINC: exact keyword hits score 1.0, and close titles ("Nursing Narr", "Discharge Sumary") match on similarity (`TITLE_MATCH_MIN`, default 0.82) and show as *close title match NN%*. Notes that have a different LOINC code are not pulled in by title.

**Other records** (off by default; tick **List Other Records**). Fetch then also runs one search for every record the member has (`DocumentReference?patient=…`, no type) and lists everything the plan didn't cover in the **Other records** tab, grouped by type, as **Not downloaded**. Tick a record or a whole type to add it to the packet. When the service note asks for **all medical records**, **Download All Record Types** is ticked automatically and every other record goes in the packet (the plan codes plus everything else); the missing-items sheet lists what was left out.

The **API trace** panel at the bottom lists every call for the current request — the browser's calls to this app and the backend's calls to Cerner (status, time, retries). It clears with each new request; **Start over** at the top resets everything.

**Offline:** `uvicorn dev.mock_fhir:app --port 9000` and set `FHIR_BASE=http://localhost:9000/r4/mock` (search `jane` or `12724066`).

## API

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/health` | — | `{fhirBase, llm, llmDeployment, llmTemperature}` |
| GET | `/api/catalog` | — | allowed document and result codes |
| POST | `/api/plan` | `{service_note, use_llm}` | plan (502 on LLM error) |
| GET | `/api/models` | | the model deployments and their price per 1M tokens |
| GET / DELETE | `/api/usage` | | input / output tokens and cost of every model call since the server started (DELETE clears) |
| POST | `/api/prompt` | `{service_note}` | the exact (redacted) messages and settings `/api/plan` sends to the model |
| GET | `/api/patients` | `name`, `birthdate?` | patient summaries |
| POST | `/api/run` | `{patient_id, plan, apply_dates, title_fallback}` | rows per LOINC call, observations, missing codes |
| POST | `/api/run/stream` | same as `/api/run` | NDJSON events: `start`, `row` (one per LOINC call), `sweep`, `observations`, `done` |
| GET | `/api/binary` | `url, type, name` | the file, inline (Cerner Binary URLs only; others → 400) |
| POST | `/api/packet/jobs` | `{plan, result}` | starts a background packet download → `{job, files, state}` |
| GET | `/api/packet/jobs/{job}` | — | per-file status (`queued` / `downloading` / `done` / `failed`) |
| GET | `/api/packet/jobs/{job}/zip` | — | the finished zip |
| GET | `/api/calls` | `after` | FHIR calls sent to Cerner (for the API trace) |
| POST | `/api/packet` | `{plan, result}` | zip attachment, header `X-Download-Errors` |

## Before real patient data

Demo only: no login, no audit log. For a real tenant add SMART backend-services auth (`system/Patient.rs system/DocumentReference.rs system/Binary.r system/Observation.rs`), Key Vault / managed identity instead of `.env`, a Service Bus queue for large batches, and confirm the LOINC list against loinc.org and your NCQA value sets.

## Models, tokens and cost

The Request card has a model picker (from `AOAI_MODELS`). Every plan shows the tokens it used, its cost and how long it took; **Usage & cost** shows the totals per model since the server started, and **Compare all models on this note** sends the same (redacted) note to every model side by side.

| Deployment | API | Input / 1M | Output / 1M | Price source |
|---|---|---|---|---|
| gpt-4o (2024-11-20) | OpenAI v1 | $2.50 | $10.00 | Azure Retail Prices API, Global |
| gpt-4.1-mini | OpenAI v1 | $0.40 | $1.60 | Azure Retail Prices API, Global |
| Llama-4-Maverick-17B-128E-Instruct-FP8 | OpenAI v1 | $0.25 | $1.00 | Azure Retail Prices API, Global |
| mistral-small-2503 | OpenAI v1 | $0.10 | $0.30 | Mistral list price (Marketplace) |
| claude-haiku-4-5 | Anthropic Messages | $1.00 | $5.00 | Anthropic list price (Marketplace) |

Cost = input × input price + output × output price (cached input at its own price). The usage ledger is in memory and resets when the server restarts. Check your Azure invoice for the billed amounts.
