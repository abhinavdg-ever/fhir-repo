# FHIR Record Retrieval

Paste a payer chart-chase **service note** and a **member**. The app reads the note with an AI model
(Azure AI Foundry), finds the member in Cerner, pulls the matching records by LOINC code and builds a zip packet.

- **Backend:** Python (FastAPI), port **8000**
- **Frontend:** React. A built copy is served by the backend, so for normal use you only run the backend.
  Run the frontend separately only when you are changing the UI (port **5173**).

## 1. Requirements

- **Python 3.10+**
- **Node 18+**, only if you want to change the UI
- An Azure AI Foundry endpoint and key, to use the AI models. Without them the app uses keyword rules.

## 2. Settings (`.env`)

Copy `.env.example` to `.env` (the run scripts do this for you on the first run) and fill in:

```
AOAI_ENDPOINT=https://<your-resource>.services.ai.azure.com/
AOAI_API_KEY=<your key>
AOAI_MODELS=gpt-4o,gpt-4.1-mini,Llama-4-Maverick-17B-128E-Instruct-FP8,mistral-small-2503,claude-haiku-4-5
```

The first model in `AOAI_MODELS` is the default. Every other setting has a working default. Restart the backend after editing `.env`.

## 3. Run on macOS / Linux

**Backend + built UI** (all you need to use the app):

```bash
git clone https://github.com/abhinavdg-ever/fhir-repo.git
cd fhir-repo
./run.sh
```

Open **http://localhost:8000**. The first run creates `.venv`, installs the requirements and creates `.env`.

**Frontend (only to change the UI)** — keep the backend running, then in a second terminal:

```bash
cd frontend
npm install
npm run dev
```

Open **http://127.0.0.1:5173**. It forwards `/api` to the backend on port 8000. Run `npm run build` when you're done, so the backend serves the new UI.

## 4. Run on Windows

**Backend + built UI** (PowerShell or Command Prompt):

```powershell
git clone https://github.com/abhinavdg-ever/fhir-repo.git
cd fhir-repo
.\run.cmd
```

Open **http://localhost:8000**. The first run creates `.venv`, installs the requirements and creates `.env`.

**Frontend (only to change the UI)** — keep the backend running, then in a second window:

```powershell
cd frontend
npm install
npm run dev
```

Open **http://127.0.0.1:5173**. Run `npm run build` when you're done.

## 5. Run on a VM with Docker

```bash
git clone https://github.com/abhinavdg-ever/fhir-repo.git && cd fhir-repo
cp .env.example .env      # fill in AOAI_ENDPOINT and AOAI_API_KEY first
docker compose up -d --build
```

Open **http://<vm-ip>:1000**. To update: `git pull && docker compose up -d --build`. Logs: `docker compose logs -f app`.

## Script options

| macOS / Linux | Windows | What it does |
| --- | --- | --- |
| `./run.sh` | `.\run.cmd` | start the app |
| `./run.sh --dev` | `.\run.cmd -Dev` | start with auto-reload on code changes |
| `./run.sh --check` | `.\run.cmd -Check` | check this machine can reach Cerner and Azure |
| `./run.sh --test` | `.\run.cmd -Test` | run the tests |
| `./run.sh --mock` | `.\run.cmd -Mock` | also start an offline fake Cerner on :9000 |

## If something goes wrong

| Problem | Fix |
| --- | --- |
| `CERTIFICATE_VERIFY_FAILED` | Corporate network: set `USE_SYSTEM_CERTS=true` in `.env` |
| Windows `curl` fails with `CRYPT_E_NO_REVOCATION_CHECK` | Use `curl.exe --ssl-no-revoke ...` (the app itself is not affected) |
| UI on :5173 shows backend errors | Make sure the backend is running on port 8000 |
| Other machines can't open the app | Set `APP_HOST=0.0.0.0` in `.env` and open port 8000 in the firewall |
| Cerner returns nothing | Sandbox data is old: untick **Apply Date Window** |

## More

Curl examples, every setting, the API, the agent's output schema, models and prices, and redaction are all in
[docs/DETAILS.md](docs/DETAILS.md).
