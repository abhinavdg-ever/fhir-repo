# FHIR Record Retrieval — Service Note → LOINC → Cerner

Paste a payer chart-chase **service note** and a **member**. The app:

Enter the service note and the member (name + date of birth, or Patient ID) together at the top, then:

1. **Plans** — Azure OpenAI (gpt-4o-mini, JSON mode) turns the note into document LOINC codes, result LOINC codes and a date window. Every code is checked against the allow-list in `agent/doc_types.py`; anything else is dropped. Untick "Use Azure OpenAI" to use keyword rules instead (no silent fallback).
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
cp .env.example .env            # then set AOAI_ENDPOINT, AOAI_DEPLOYMENT, AOAI_API_KEY
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
| `AOAI_DEPLOYMENT` | Azure **deployment** name, e.g. `gpt-4o-mini`; swap to try another model |
| `AOAI_API_VERSION` | default `2024-10-21` |
| `AOAI_API_KEY` | key, or leave empty and use Entra ID (`pip install azure-identity`, `az login`) |
| `AOAI_TEMPERATURE` | default `0`; leave empty to send none (gpt-5 / o-series). If the model rejects temperature, the call is retried without it |
| `AOAI_TIMEOUT` | seconds per call, default `30` |

Restart uvicorn after editing `.env`. The header chip shows the active deployment and temperature.

## Cerner sandbox

`FHIR_BASE` defaults to the open sandbox `https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d` (no auth).

- Type `12724066` in the patient box to load that test patient. A name search needs the date of birth too, as mm/dd/yyyy (e.g. `Tim Peters` + 09/15/1990). Dates in the UI and packet sheets are mm/dd/yyyy.
- Sandbox notes are often outside the window — untick **Apply date window**.
- Tick **Also match notes by title** to catch notes with no LOINC on `type`.
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

**Document types.** `agent/doc_types.py` lists 24 document LOINC codes, including the ones Cerner actually puts on notes (e.g. `83805-2` admission note, `28623-7` nursing narrative, `28568-4` / `57053-1` ED physician / nurse, `34108-1` outpatient, `18761-7` transfer summary, `11504-8` operative report). Title keywords include Cerner's own titles ("Inpatient Clinical Summary" is a discharge summary).

**Title matching** (optional) catches notes Cerner never coded with LOINC: exact keyword hits score 1.0, and close titles ("Nursing Narr", "Discharge Sumary") match on similarity (`TITLE_MATCH_MIN`, default 0.82) and show as *close title match NN%*. Notes that have a different LOINC code are not pulled in by title.

**Other records.** Fetch also runs one search for every record the member has (`DocumentReference?patient=…`, no type) and lists everything the plan didn't cover in the **Other records** tab, grouped by type, as **Not downloaded**. Tick a record or a whole type to add it to the packet. When the service note asks for **all medical records**, every other record goes in the packet automatically (the plan codes plus everything else); the missing-items sheet lists what was left out.

The **API trace** panel at the bottom lists every call for the current request — the browser's calls to this app and the backend's calls to Cerner (status, time, retries). It clears with each new request; **Start over** at the top resets everything.

**Offline:** `uvicorn dev.mock_fhir:app --port 9000` and set `FHIR_BASE=http://localhost:9000/r4/mock` (search `jane` or `12724066`).

## API

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/health` | — | `{fhirBase, llm, llmDeployment, llmTemperature}` |
| GET | `/api/catalog` | — | allowed document and result codes |
| POST | `/api/plan` | `{service_note, use_llm}` | plan (502 on LLM error) |
| POST | `/api/prompt` | `{service_note}` | the exact messages and settings `/api/plan` sends to the model |
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
