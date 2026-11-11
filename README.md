# FHIR Record Retrieval — Service Note → LOINC → Cerner

Paste a payer chart-chase **service note** and a **member**. The app:

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

From the repo root (Python 3.10+):

```bash
python -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate
pip install -r requirements.txt
cp .env.example .env               # then fill in AOAI_ENDPOINT and AOAI_API_KEY
uvicorn agent.app:app --reload --port 8000
```

Open **http://localhost:8000** — the built UI in `frontend/dist` is served by the API.

Working on the UI (Node 18+): `cd frontend && npm install && npm run dev`, then open http://localhost:5173 (it proxies `/api` to :8000). Run `npm run build` to refresh `frontend/dist`.

Tests: `pytest -q tests`

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

- Type `12724066` in the patient box to load that test patient.
- Sandbox notes are often outside the window — untick **Apply date window**.
- Tick **Also match notes by title** to catch notes with no LOINC on `type`.
- The open sandbox rate-limits (HTTP 429); a limited call shows as an error on its row instead of failing the run.

**Offline:** `uvicorn dev.mock_fhir:app --port 9000` and set `FHIR_BASE=http://localhost:9000/r4/mock` (search `jane` or `12724066`).

## API

| Method | Path | Body / query | Returns |
| --- | --- | --- | --- |
| GET | `/api/health` | — | `{fhirBase, llm, llmDeployment, llmTemperature}` |
| GET | `/api/catalog` | — | allowed document and result codes |
| POST | `/api/plan` | `{service_note, use_llm}` | plan (502 on LLM error) |
| GET | `/api/patients` | `name`, `birthdate?` | patient summaries |
| POST | `/api/run` | `{patient_id, plan, apply_dates, title_fallback}` | rows per LOINC call, observations, missing codes |
| GET | `/api/binary` | `url, type, name` | the file, inline (Cerner Binary URLs only; others → 400) |
| POST | `/api/packet` | `{plan, result}` | zip attachment, header `X-Download-Errors` |

## Before real patient data

Demo only: no login, no audit log. For a real tenant add SMART backend-services auth (`system/Patient.rs system/DocumentReference.rs system/Binary.r system/Observation.rs`), Key Vault / managed identity instead of `.env`, a Service Bus queue for large batches, and confirm the LOINC list against loinc.org and your NCQA value sets.
