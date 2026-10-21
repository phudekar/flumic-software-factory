"""Idempotent SigNoz V2 dashboard provisioning through authenticated HTTP APIs."""

from __future__ import annotations

import json
import os
from pathlib import Path
import secrets
import sys
import time
import urllib.error
import urllib.parse
import urllib.request


class ProvisioningError(RuntimeError):
    pass


class ApiError(ProvisioningError):
    def __init__(self, status):
        self.status = status
        super().__init__(f"SigNoz API returned HTTP {status}")


class Api:
    def __init__(self, base, api_key="", opener=urllib.request.urlopen):
        self.base = base.rstrip("/")
        self.headers = {"Content-Type": "application/json"}
        if api_key:
            self.headers["SIGNOZ-API-KEY"] = api_key
        self.opener = opener

    def request(self, method, path, body=None):
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(
            self.base + path, data=data, headers=self.headers, method=method
        )
        try:
            with self.opener(request, timeout=15) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as error:
            # Never echo response bodies or headers from authentication endpoints.
            raise ApiError(error.code) from None
        except (urllib.error.URLError, TimeoutError, OSError):
            raise ProvisioningError("Cannot reach SigNoz API") from None

    def wait_ready(self, timeout=180):
        deadline = time.monotonic() + timeout
        while True:
            try:
                self.request("GET", "/api/v1/health")
                return
            except ProvisioningError:
                if time.monotonic() >= deadline:
                    raise ProvisioningError(
                        "Timed out waiting for SigNoz health"
                    ) from None
                time.sleep(2)


def authenticate(api, env, state_dir):
    if env.get("SIGNOZ_API_KEY"):
        return
    email, password = env.get("SIGNOZ_ADMIN_EMAIL"), env.get("SIGNOZ_ADMIN_PASSWORD")
    if bool(email) != bool(password):
        raise ProvisioningError(
            "Set both SIGNOZ_ADMIN_EMAIL and SIGNOZ_ADMIN_PASSWORD, or SIGNOZ_API_KEY"
        )
    credentials_path = Path(state_dir) / "login.json"
    credentials = {"email": email, "password": password} if email else None
    if credentials is None and credentials_path.exists():
        credentials = json.loads(credentials_path.read_text())
    version = api.request("GET", "/api/v1/version")
    if not version.get("setupCompleted"):
        if credentials is None:
            credentials = {
                "email": "factory-admin@example.invalid",
                "password": secrets.token_urlsafe(32) + "aA1!",
            }
            credentials_path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            fd = os.open(credentials_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w") as file:
                json.dump(credentials, file, indent=2)
        api.request(
            "POST",
            "/api/v1/register",
            {
                "name": "Factory Admin",
                "orgName": "software-factory",
                "orgDisplayName": "Software Factory",
                **credentials,
            },
        )
        print(
            "Initialized fresh SigNoz account; generated credentials, if used, are in bootstrap state/login.json"
        )
    if credentials is None:
        raise ProvisioningError(
            "Existing SigNoz account requires SIGNOZ_API_KEY or SIGNOZ_ADMIN_EMAIL and "
            "SIGNOZ_ADMIN_PASSWORD in .env.observability. No account was changed."
        )
    context = api.request(
        "GET",
        "/api/v2/sessions/context?"
        + urllib.parse.urlencode({"email": credentials["email"]}),
    )
    orgs = context.get("data", {}).get("orgs", [])
    requested_org = env.get("SIGNOZ_ORG_ID")
    if requested_org:
        orgs = [org for org in orgs if org["id"] == requested_org]
    if len(orgs) != 1:
        raise ProvisioningError("Select a unique organization using SIGNOZ_ORG_ID")
    session = api.request(
        "POST",
        "/api/v2/sessions/email_password",
        {**credentials, "orgId": orgs[0]["id"]},
    )
    token = session.get("data", {}).get("accessToken")
    if not token:
        raise ProvisioningError("SigNoz did not return an access token")
    api.headers["Authorization"] = "Bearer " + token


def find_dashboard(api, name):
    offset, page_size = 0, 100
    while True:
        response = api.request(
            "GET", f"/api/v2/dashboards?limit={page_size}&offset={offset}"
        )
        data = response.get("data", {})
        dashboards = data.get("dashboards", [])
        matches = [
            dashboard for dashboard in dashboards if dashboard.get("name") == name
        ]
        if len(matches) > 1:
            raise ProvisioningError(
                "Multiple dashboards have the provisioned name; refusing an ambiguous update"
            )
        if matches:
            return matches[0]
        offset += len(dashboards)
        if not dashboards or offset >= data.get("total", len(dashboards)):
            return None


def provision(api, payload):
    if payload.get("schemaVersion") != "v6" or not payload.get("name"):
        raise ProvisioningError(
            "Dashboard must have schemaVersion=v6 and a stable name"
        )
    existing = find_dashboard(api, payload["name"])
    if existing is None:
        try:
            created = api.request("POST", "/api/v2/dashboards", payload)
            return created["data"]["id"], "created"
        except ApiError as error:
            if error.status != 409:
                raise
            # Another startup may have created this stable name concurrently.
            existing = find_dashboard(api, payload["name"])
            if existing is None:
                raise
    if existing.get("locked") or existing.get("source", "user") != "user":
        raise ProvisioningError(
            "Provisioned dashboard is locked or not user-managed; refusing to overwrite"
        )
    dashboard_id = existing["id"]
    current = api.request("GET", f"/api/v2/dashboards/{dashboard_id}")["data"]
    desired = {key: payload[key] for key in ("schemaVersion", "name", "tags", "spec")}
    if all(current.get(key) == value for key, value in desired.items()):
        return dashboard_id, "unchanged"
    api.request("PUT", f"/api/v2/dashboards/{dashboard_id}", desired)
    return dashboard_id, "updated"


def main():
    env = os.environ
    api = Api(
        env.get("SIGNOZ_URL", "http://factory-signoz-signoz-0:8080"),
        env.get("SIGNOZ_API_KEY", ""),
    )
    try:
        payload = json.loads(
            Path(
                env.get("DASHBOARD_FILE", "/dashboards/software-factory.json")
            ).read_text()
        )
        api.wait_ready()
        authenticate(api, env, env.get("BOOTSTRAP_STATE_DIR", "/state"))
        dashboard_id, action = provision(api, payload)
        print(f"Factory dashboard {action}: /dashboard/{dashboard_id}")
        return 0
    except (ProvisioningError, ValueError, KeyError, OSError) as error:
        # Configured credentials and session response content never go to logs.
        message = (
            str(error) if isinstance(error, ProvisioningError) else type(error).__name__
        )
        print("Dashboard provisioning failed: " + message, file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
