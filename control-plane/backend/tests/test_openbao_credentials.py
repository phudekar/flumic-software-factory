import unittest
from unittest.mock import Mock
from uuid import uuid4

from factory.adapters.openbao import OpenBao, OpenBaoError, credential_path


class OpenBaoTests(unittest.TestCase):
    def setUp(self):
        self.bao = OpenBao("https://bao.example", token="fixture")
        self.bao.request = Mock()
        self.path = credential_path("flumic", str(uuid4()), str(uuid4()))

    def test_create_only_and_read_pinned_version(self):
        self.bao.request.return_value = {"data": {"version": 1}}
        self.assertEqual(self.bao.create(self.path, "secret"), 1)
        self.bao.request.assert_called_once_with(
            "POST", self.path, {"options": {"cas": 0}, "data": {"value": "secret"}}
        )
        self.bao.request.return_value = {
            "data": {"metadata": {"version": 2}, "data": {"value": "other"}}
        }
        with self.assertRaises(OpenBaoError):
            self.bao.read(self.path, 1)

    def test_wrapped_read_and_direct_unwrap_verify_binding(self):
        self.bao.request.side_effect = [
            {"wrap_info": {"token": "one-use", "ttl": 20, "creation_path": self.path}},
            {"data": {"creation_path": self.path}},
            {"data": {"metadata": {"version": 1}, "data": {"value": "secret"}}},
        ]
        wrap = self.bao.wrap(self.path, 1, 20)
        self.assertEqual(self.bao.unwrap(self.path, 1, wrap), "secret")
        self.bao.request.assert_any_call("GET", self.path + "?version=1", wrap_ttl=20)
        self.bao.request.assert_any_call("POST", "sys/wrapping/unwrap", token=wrap)

    def test_wrong_origin_does_not_consume_token(self):
        self.bao.request.return_value = {"data": {"creation_path": "other"}}
        with self.assertRaises(OpenBaoError):
            self.bao.unwrap(self.path, 1, "one-use")
        self.bao.request.assert_called_once()

    def test_rejects_insecure_endpoints_and_untrusted_path_segments(self):
        for url in ("http://bao.example", "https://user:pass@bao.example", "https://bao.example/path"):
            with self.subTest(url=url), self.assertRaises(ValueError):
                OpenBao(url)
        with self.assertRaises(ValueError):
            credential_path("flumic", "../other", str(uuid4()))


class CredentialStoreTests(unittest.TestCase):
    def test_openbao_reference_never_falls_back_to_ciphertext(self):
        from types import SimpleNamespace
        from fastapi import HTTPException
        from app.credential_store import read_credential

        row = SimpleNamespace(openbao_version=1, ciphertext="old-ciphertext")
        with self.assertRaises(HTTPException) as failure:
            read_credential(row, SimpleNamespace(openbao_address=None))
        self.assertEqual(failure.exception.status_code, 503)

    def test_reads_the_referenced_version(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.credential_store import read_credential

        row = SimpleNamespace(
            project_id=str(uuid4()), id=str(uuid4()), openbao_version=1, ciphertext=None
        )
        settings = SimpleNamespace(openbao_mount="flumic")
        with patch("app.credential_store.openbao_client") as factory:
            factory.return_value.read.return_value = "provider-value"
            self.assertEqual(read_credential(row, settings), "provider-value")
        factory.return_value.read.assert_called_once_with(
            credential_path("flumic", row.project_id, row.id), 1
        )

    def test_wrapping_uses_remaining_authority_window(self):
        from types import SimpleNamespace
        from unittest.mock import patch
        from app.credential_store import wrap_credential

        row = SimpleNamespace(project_id=str(uuid4()), id=str(uuid4()), openbao_version=1)
        settings = SimpleNamespace(openbao_mount="flumic")
        with patch("app.credential_store.openbao_client") as factory:
            factory.return_value.wrap.return_value = "single-use"
            grant = wrap_credential(row, settings, seconds_remaining=9.8)
        self.assertEqual(grant, {
            "path": credential_path("flumic", row.project_id, row.id),
            "version": 1, "token": "single-use",
        })
        factory.return_value.wrap.assert_called_once_with(grant["path"], 1, 9)
