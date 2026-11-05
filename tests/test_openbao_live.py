"""Opt-in real OpenBao transport test using an isolated TLS dev server."""

import json
import os
from pathlib import Path
import secrets
import socket
import subprocess
import tempfile
import time
import unittest
from urllib.request import Request
from uuid import uuid4

from factory.adapters.openbao import OpenBao, OpenBaoError, credential_path


@unittest.skipUnless(os.environ.get("OPENBAO_TEST_BIN"), "Set OPENBAO_TEST_BIN to qualify OpenBao")
class OpenBaoLiveTests(unittest.TestCase):
    def test_create_only_pinned_read_and_single_use_unwrap(self):
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            address = f"127.0.0.1:{listener.getsockname()[1]}"
        token = secrets.token_urlsafe(32)
        with tempfile.TemporaryDirectory() as directory:
            process = subprocess.Popen(
                [os.environ["OPENBAO_TEST_BIN"], "server", "-dev-tls",
                 "-dev-listen-address=" + address, "-dev-tls-cert-dir=" + directory],
                env={**os.environ, "BAO_DEV_ROOT_TOKEN_ID": token},
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            try:
                for _ in range(60):
                    if process.poll() is not None:
                        self.fail("OpenBao test server exited")
                    ca = next(Path(directory).glob("*ca.pem"), None)
                    if ca is not None:
                        controller = OpenBao(f"https://{address}", token=token, ca_file=ca)
                        try:
                            controller.request("GET", "sys/health")
                            break
                        except OpenBaoError:
                            pass
                    time.sleep(0.1)
                else:
                    self.fail("OpenBao test server did not become ready")

                policy = Request(
                    f"https://{address}/v1/sys/policies/acl/flumic-fixture",
                    data=json.dumps({
                        "policy": 'path "secret/data/projects/*" { capabilities = ["create", "read"] }'
                    }).encode(),
                    headers={"X-Vault-Token": token, "Content-Type": "application/json"},
                    method="PUT",
                )
                with controller.http.open(policy, timeout=5) as response:
                    self.assertEqual(response.status, 204)
                identity = controller.request("POST", "auth/token/create", {
                    "policies": ["flumic-fixture"], "no_default_policy": True,
                    "ttl": "1m", "renewable": False,
                })
                controller = OpenBao(
                    f"https://{address}", token=identity["auth"]["client_token"], ca_file=ca
                )

                path = credential_path("secret", str(uuid4()), str(uuid4()))
                self.assertEqual(controller.create(path, "fixture-value"), 1)
                with self.assertRaises(OpenBaoError):
                    controller.create(path, "replacement")
                self.assertEqual(controller.read(path, 1), "fixture-value")
                wrap = controller.wrap(path, 1, 10)
                worker = OpenBao(f"https://{address}", ca_file=ca)
                with self.assertRaises(OpenBaoError):
                    worker.unwrap(credential_path("secret", str(uuid4()), str(uuid4())), 1, wrap)
                self.assertEqual(worker.unwrap(path, 1, wrap), "fixture-value")
                with self.assertRaises(OpenBaoError):
                    worker.unwrap(path, 1, wrap)
                with self.assertRaises(OpenBaoError):
                    worker.read(path, 1)
                expired = controller.wrap(path, 1, 1)
                time.sleep(1.2)
                with self.assertRaises(OpenBaoError):
                    worker.unwrap(path, 1, expired)
            finally:
                process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait()
