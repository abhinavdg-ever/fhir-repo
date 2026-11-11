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

## Run it

Needs **Python 3.10+**. Node is only needed to change the UI — the built UI in `frontend/dist` is committed and served by the API.

**Windows** (PowerShell or Command Prompt, from the repo folder):

```powershell
.\run.cmd            # first run creates .venv, installs requirements and .env; then serves the app
.\run.cmd -Dev       # auto-reload while editing code
.\run.cmd -Mock      # also start the offline mock FHIR server on :9000
.\run.cmd -Test      # run the tests
```

`run.cmd` calls `run.ps1` with `-ExecutionPolicy Bypass`, so it works even where PowerShell blocks scripts. It finds Python through the `py` launcher (or `python` on PATH).

**macOS / Linux:** `./run.sh` (same options: `--dev`, `--mock`, `--test`).

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
| A proxy | Set `HTTPS_PROXY=http://proxy:port` (and `NO_PROXY=localhost,127.0.0.1`) before starting; httpx and the OpenAI SDK honour them |
| Keep it running after logoff | Run `run.cmd` from Task Scheduler ("At startup", "Run whether user is logged on or not"), or wrap it with a service manager such as NSSM |
| Edit `.env` | Any editor is fine; save as UTF-8 (Notepad's default) |

Line endings are normalised by `.gitattributes`, so a Windows clone works without changes. The text sheets in the packet use Windows line endings.

UI work (Node 18+): `cd frontend && npm install && npm run dev`, then open http://localhost:5173 (it proxies `/api` to :8000). Run `npm run build` to refresh `frontend/dist`.

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
