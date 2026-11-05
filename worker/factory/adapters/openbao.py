"""Minimal OpenBao KV v2 and response-wrapping transport."""

import json
import re
import ssl
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)
from uuid import UUID


class OpenBaoError(RuntimeError):
    """A redacted credential delivery failure."""


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def credential_path(mount, project_id, credential_id):
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,63}", mount):
        raise ValueError("Invalid OpenBao mount")
    return f"{mount}/data/projects/{UUID(project_id)}/credentials/{UUID(credential_id)}"


class OpenBao:
    def __init__(self, endpoint, *, token=None, ca_file=None):
        url = urlsplit(endpoint)
        if (
            url.scheme != "https"
            or not url.hostname
            or url.username is not None
            or url.password is not None
            or url.path not in ("", "/")
            or url.query
            or url.fragment
        ):
            raise ValueError("OpenBao requires a trusted HTTPS origin")
        self.endpoint = endpoint.rstrip("/")
        self.token = token
        self.http = build_opener(
            ProxyHandler({}),
            NoRedirect(),
            HTTPSHandler(context=ssl.create_default_context(cafile=ca_file)),
        )

    def request(self, method, path, body=None, *, token=None, wrap_ttl=None):
        headers = {"Content-Type": "application/json"}
        auth = self.token if token is None else token
        if auth:
            headers["X-Vault-Token"] = auth
        if wrap_ttl is not None:
            headers["X-Vault-Wrap-TTL"] = f"{wrap_ttl}s"
        try:
            req = Request(
                self.endpoint + "/v1/" + path,
                data=None if body is None else json.dumps(body).encode(),
                headers=headers,
                method=method,
            )
            with self.http.open(req, timeout=5) as response:
                raw = response.read(131073)
            if len(raw) > 131072:
                raise ValueError("Oversized OpenBao response")
            data = json.loads(raw)
            if not isinstance(data, dict):
                raise ValueError("Malformed OpenBao response")
            return data
        except HTTPError as error:
            error.close()
            raise OpenBaoError("OpenBao credential request failed") from None
        except (URLError, OSError, ValueError):
            raise OpenBaoError("OpenBao credential request failed") from None

    def create(self, path, value):
        response = self.request(
            "POST", path, {"options": {"cas": 0}, "data": {"value": value}}
        )
        try:
            version = response["data"]["version"]
            if type(version) is int and version == 1:
                return version
        except (KeyError, TypeError):
            pass
        raise OpenBaoError("Invalid OpenBao credential version")

    def read(self, path, version):
        return self._value(self.request("GET", f"{path}?version={version}"), version)

    def wrap(self, path, version, ttl):
        if type(ttl) is not int or not 1 <= ttl <= 30:
            raise ValueError("Invalid OpenBao wrapping lifetime")
        response = self.request("GET", f"{path}?version={version}", wrap_ttl=ttl)
        try:
            wrapped = response["wrap_info"]
            if (
                wrapped["creation_path"] == path
                and type(wrapped["ttl"]) is int
                and 0 < wrapped["ttl"] <= ttl
                and isinstance(wrapped["token"], str)
                and wrapped["token"]
            ):
                return wrapped["token"]
        except (KeyError, TypeError):
            pass
        raise OpenBaoError("Invalid OpenBao wrapped response")

    def unwrap(self, path, version, token):
        if not isinstance(token, str) or not token:
            raise OpenBaoError("Invalid OpenBao wrapping token")
        metadata = self.request("POST", "sys/wrapping/lookup", token=token)
        try:
            matches = metadata["data"]["creation_path"] == path
        except (KeyError, TypeError):
            matches = False
        if not matches:
            raise OpenBaoError("OpenBao wrapping origin mismatch")
        return self._value(
            self.request("POST", "sys/wrapping/unwrap", token=token), version
        )

    @staticmethod
    def _value(response, version):
        try:
            data = response["data"]
            actual = data["metadata"]["version"]
            value = data["data"]["value"]
            if type(actual) is int and actual == version and isinstance(value, str) and value:
                return value
        except (KeyError, TypeError):
            pass
        raise OpenBaoError("Invalid OpenBao credential response")
