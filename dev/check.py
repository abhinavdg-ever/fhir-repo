"""Connectivity check: can this machine reach Cerner and Azure OpenAI the way the app does?

    python -m dev.check          (or: run.cmd -Check  /  ./run.sh --check)

Uses the same .env, HTTP client and certificate settings as the app, and prints
what to change when something fails. Exit code 1 if a required check fails.
"""
from __future__ import annotations

import os
import ssl
import sys
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
load_dotenv(ROOT / ".env")

TEST_PATIENT = os.getenv("CHECK_PATIENT_ID", "12742400")  # Tim Peters on the open sandbox
OK, FAIL, SKIP = "OK  ", "FAIL", "SKIP"


def _setting(name: str) -> str:
    value = os.getenv(name, "").strip()
    return "" if "<" in value else value


def explain(error: Exception) -> str:
    text = str(error)
    if isinstance(error, httpx.ConnectTimeout) or "timed out" in text.lower():
        return "Timed out. The machine may need a proxy: set HTTPS_PROXY=http://proxy:port before starting."
    if "CERTIFICATE_VERIFY_FAILED" in text or isinstance(error, ssl.SSLCertVerificationError):
        return ("Certificate not trusted, usually corporate HTTPS inspection. Set USE_SYSTEM_CERTS=true in .env "
                "so the Windows certificate store is used.")
    if "getaddrinfo" in text or "Name or service not known" in text or "nodename nor servname" in text:
        return "DNS lookup failed. Check the URL, the network, or set HTTPS_PROXY."
    if "ProxyError" in type(error).__name__ or "407" in text:
        return "The proxy refused the request. Check HTTPS_PROXY (and proxy credentials)."
    return text[:300]


def main() -> int:
    failures = 0
    use_system_certs = os.getenv("USE_SYSTEM_CERTS", "").strip().lower() in ("1", "true", "yes")
    if use_system_certs:
        import truststore

        truststore.inject_into_ssl()

    base = os.getenv("FHIR_BASE", "https://fhir-open.cerner.com/r4/ec2458f2-1e24-41c8-b71b-0e701af7583d").rstrip("/")
    proxy = os.getenv("HTTPS_PROXY") or os.getenv("https_proxy") or "none"
    print(f"Python {sys.version.split()[0]} on {sys.platform} | httpx {httpx.__version__}")
    print(f"FHIR_BASE {base}")
    print(f"Certificates: {'system store (USE_SYSTEM_CERTS=true)' if use_system_certs else 'certifi bundle'} | HTTPS_PROXY: {proxy}\n")

    headers = {"Accept": "application/fhir+json"}
    token = os.getenv("FHIR_BEARER_TOKEN")
    if token:
        headers["Authorization"] = f"Bearer {token}"

    with httpx.Client(timeout=float(os.getenv("FHIR_TIMEOUT", "60") or 60), headers=headers, follow_redirects=True) as c:
        # 1. Patient
        binary_url = None
        try:
            r = c.get(f"{base}/Patient/{TEST_PATIENT}")
            if r.status_code == 200:
                name = (r.json().get("name") or [{}])[0].get("text", "?")
                print(f"[{OK}] Cerner Patient/{TEST_PATIENT}: {name}")
            else:
                failures += 1
                print(f"[{FAIL}] Cerner Patient/{TEST_PATIENT}: HTTP {r.status_code} {r.text[:200]}")
        except Exception as e:
            failures += 1
            print(f"[{FAIL}] Cerner Patient/{TEST_PATIENT}: {type(e).__name__}: {explain(e)}")

        # 2. DocumentReference search (one LOINC code)
        try:
            r = c.get(f"{base}/DocumentReference", params={"patient": TEST_PATIENT, "type": "http://loinc.org|11506-3", "_count": "5"})
            entries = [e["resource"] for e in r.json().get("entry", []) if e.get("resource", {}).get("resourceType") == "DocumentReference"]
            print(f"[{OK if r.status_code == 200 else FAIL}] DocumentReference search: HTTP {r.status_code}, {len(entries)} notes on the first page")
            failures += r.status_code != 200
            for d in entries:
                att = next((ct["attachment"] for ct in d.get("content", []) if ct.get("attachment", {}).get("url")), None)
                if att:
                    binary_url, binary_type = att["url"], att.get("contentType") or "application/fhir+json"
                    break
        except Exception as e:
            failures += 1
            print(f"[{FAIL}] DocumentReference search: {type(e).__name__}: {explain(e)}")

        # 3. Binary download with the exact Accept Cerner requires
        if binary_url:
            try:
                r = c.get(binary_url, headers={"Accept": binary_type})
                print(f"[{OK if r.status_code == 200 else FAIL}] Binary download (Accept {binary_type}): HTTP {r.status_code}, {len(r.content)} bytes")
                failures += r.status_code != 200
            except Exception as e:
                failures += 1
                print(f"[{FAIL}] Binary download: {type(e).__name__}: {explain(e)}")
        else:
            print(f"[{SKIP}] Binary download: no note with a file found for this patient")

    # 4. Azure OpenAI (optional)
    endpoint, deployment, key = _setting("AOAI_ENDPOINT"), _setting("AOAI_DEPLOYMENT"), _setting("AOAI_API_KEY")
    if not (endpoint and deployment):
        print(f"[{SKIP}] Azure OpenAI: not configured (keyword rules still work)")
    elif not key:
        print(f"[{SKIP}] Azure OpenAI: no AOAI_API_KEY (Entra ID sign-in is not checked here)")
    else:
        try:
            r = httpx.get(f"{endpoint.rstrip('/')}/openai/deployments", params={"api-version": "2022-12-01"},
                          headers={"api-key": key}, timeout=30)
            if r.status_code == 200:
                names = [d.get("id") for d in r.json().get("data", [])]
                found = deployment in names
                print(f"[{OK if found else FAIL}] Azure OpenAI: reachable; deployments {names}"
                      + ("" if found else f" - AOAI_DEPLOYMENT={deployment} is not one of them"))
                failures += not found
            elif r.status_code in (401, 403):
                failures += 1
                print(f"[{FAIL}] Azure OpenAI: HTTP {r.status_code} - check AOAI_API_KEY")
            else:
                print(f"[{OK}] Azure OpenAI: reachable (HTTP {r.status_code} listing deployments; the chat call may still work)")
        except Exception as e:
            failures += 1
            print(f"[{FAIL}] Azure OpenAI: {type(e).__name__}: {explain(e)}")

    print("\nAll required checks passed." if not failures else f"\n{failures} check(s) failed - see the hints above.")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
