"""
One-shot installer: waits for Open WebUI to come up, signs up (or signs in)
a demo admin account, and installs clausurus.py as a Function via the REST
API. Meant to run as the "installer" service in docker-compose.yml -- not
part of the Clausurus function itself.
"""

import os
import sys
import time
from pathlib import Path

import httpx

BASE_URL = os.environ.get("WEBUI_BASE_URL", "http://openwebui:8080")
ADMIN_EMAIL = os.environ.get("DEMO_ADMIN_EMAIL", "admin@example.com")
ADMIN_PASSWORD = os.environ.get("DEMO_ADMIN_PASSWORD", "clausurus-demo-only")
ADMIN_NAME = os.environ.get("DEMO_ADMIN_NAME", "Clausurus Demo Admin")
CLAUSURUS_PATH = Path(os.environ.get("CLAUSURUS_PATH", "/clausurus/clausurus.py"))


def wait_for_webui(client: httpx.Client, timeout: int = 120):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            resp = client.get(f"{BASE_URL}/health", timeout=5)
            if resp.status_code == 200:
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

    # already signed up (e.g. re-running the installer) -> sign in instead
    signin = client.post(
        f"{BASE_URL}/api/v1/auths/signin", json={"email": ADMIN_EMAIL, "password": ADMIN_PASSWORD}
    )
    signin.raise_for_status()
    return signin.json()["token"]


def install_function(client: httpx.Client, token: str):
    content = CLAUSURUS_PATH.read_text(encoding="utf-8")
    resp = client.post(
        f"{BASE_URL}/api/v1/functions/create",
        headers={"Authorization": f"Bearer {token}"},
        json={
            "id": "clausurus",
            "name": "Clausurus",
            "content": content,
            "meta": {"description": "Anonymizing BYOK router for external LLM providers."},
        },
    )
    if resp.status_code == 200:
        print("Clausurus function installed.")
    elif resp.status_code in (400, 409):
        print(f"Function may already exist ({resp.status_code}): {resp.text[:200]}")
    else:
        resp.raise_for_status()

    # enable it globally so it shows up in the model list for the demo
    toggle = client.post(
        f"{BASE_URL}/api/v1/functions/id/clausurus/toggle",
        headers={"Authorization": f"Bearer {token}"},
    )
    print(f"Toggle response: {toggle.status_code}")


def main():
    with httpx.Client() as client:
        wait_for_webui(client)
        token = get_admin_token(client)
        install_function(client, token)


if __name__ == "__main__":
    try:
        main()
    except Exception as e:  # noqa: BLE001
        print(f"Installer failed: {e}", file=sys.stderr)
        sys.exit(1)
