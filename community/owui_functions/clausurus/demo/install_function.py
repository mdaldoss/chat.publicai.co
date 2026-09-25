"""
One-shot demo installer: waits for Open WebUI, signs up (or signs in) a demo admin,
installs clausurus.py as a Function, switches it on, and copies the keys from the
environment into the function's valves. Safe to run more than once.

Used by docker-compose.yml, or directly against any Open WebUI you run yourself:

    WEBUI_BASE_URL=http://localhost:8080 python install_function.py

Keys are read from the environment only (see .env.example) and sent to your own Open
WebUI; nothing is written to disk.
"""

import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://openwebui:8080").rstrip("/")
ADMIN_EMAIL = os.environ.get("DEMO_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "clausurus-demo-only")
ADMIN_NAME = os.environ.get("DEMO_ADMIN_NAME", "Clausurus Demo Admin")
CLAUSURUS_PATH = Path(os.environ.get("CLAUSURUS_PATH", Path(__file__).resolve().parents[1] / "clausurus.py"))
FUNCTION_ID = "clausurus"

# environment variable -> valve name
VALVES_FROM_ENV = {
    "APERTUS_API_BASE": "apertus_api_base",
    "APERTUS_API_KEY": "apertus_api_key",
    "APERTUS_MODEL": "apertus_model",
    "APERTUS_LOCATION": "apertus_location",
    "SERVER_LOCATION": "server_location",
    "OPENAI_API_KEY": "openai_api_key",
    "GEMINI_API_KEY": "gemini_api_key",
    "ANTHROPIC_API_KEY": "anthropic_api_key",
    "OPENROUTER_API_KEY": "openrouter_api_key",
    "OPENROUTER_MODEL": "openrouter_model",
}


def wait_for_webui(client: httpx.Client, timeout: int = 300):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if client.get(f"{BASE_URL}/health", timeout=5).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(2)
    raise RuntimeError(f"Open WebUI did not become healthy within {timeout}s")


def get_admin_token(client: httpx.Client) -> str:
    signup = client.post(
        f"{BASE_URL}/api/v1/auths/signup",
        json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD, "name": ADMIN_NAME},
    )
    if signup.status_code == 200:
        return signup.json()["token"]
    signin = client.post(f"{BASE_URL}/api/v1/auths/signin", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD})
    signin.raise_for_status()
    return signin.json()["token"]


def install(client: httpx.Client, token: str):
    auth = {"Authorization": f"Bearer {token}"}
    form = {
        "id": FUNCTION_ID,
        "name": "Clausurus",
        "content": CLAUSURUS_PATH.read_text(encoding="utf-8"),
        "meta": {"description": "Hides personal data before it reaches an external LLM."},
    }
    existing = client.get(f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}", headers=auth)
    if existing.status_code == 200 and existing.json():
        resp = client.post(f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}/update", headers=auth, json=form)
        print(f"Updated Clausurus: {resp.status_code}")
    else:
        resp = client.post(f"{BASE_URL}/api/v1/functions/create", headers=auth, json=form)
        print(f"Created Clausurus: {resp.status_code}")
    resp.raise_for_status()

    # /toggle flips the state, so only call it when the function is off.
    function = client.get(f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}", headers=auth).json()
    if not function.get("is_active"):
        client.post(f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}/toggle", headers=auth).raise_for_status()
    print("Clausurus is active.")

    valves = client.get(f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}/valves", headers=auth).json() or {}
    updates = {valve: os.environ[env] for env, valve in VALVES_FROM_ENV.items() if os.environ.get(env)}
    if updates:
        valves.update(updates)
        client.post(
            f"{BASE_URL}/api/v1/functions/id/{FUNCTION_ID}/valves/update", headers=auth, json=valves
        ).raise_for_status()
    print("Valves set from environment: " + (", ".join(sorted(updates)) or "none"))


def main():
    with httpx.Client(timeout=30) as client:
        wait_for_webui(client)
        install(client, get_admin_token(client))
    print(f"Done. Open {os.environ.get('PUBLIC_URL', BASE_URL)} and log in as {ADMIN_EMAIL}.")


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"Installer failed: {e}", file=sys.stderr)
        sys.exit(1)
