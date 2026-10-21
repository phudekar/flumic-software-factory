import importlib.util
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock

spec = importlib.util.spec_from_file_location(
    "dashboard_bootstrap",
    Path(__file__).resolve().parents[1] / "observability/bootstrap-dashboard.py",
)
bootstrap = importlib.util.module_from_spec(spec)
spec.loader.exec_module(bootstrap)


class BootstrapTests(unittest.TestCase):
    def payload(self):
        return {
            "schemaVersion": "v6",
            "name": "factory",
            "tags": [],
            "spec": {"display": {"name": "Factory"}},
        }

    def test_creates_once_and_updates_existing_dashboard(self):
        api = Mock()
        api.request.side_effect = [
            {"data": {"dashboards": [], "total": 0}},
            {"data": {"id": "id-1"}},
        ]
        self.assertEqual(bootstrap.provision(api, self.payload()), ("id-1", "created"))
        api.request.side_effect = [
            {
                "data": {
                    "dashboards": [
                        {
                            "id": "id-1",
                            "name": "factory",
                            "source": "user",
                            "locked": False,
                        }
                    ],
                    "total": 1,
                }
            },
            {"data": {**self.payload(), "spec": {"display": {"name": "Old"}}}},
            {},
        ]
        self.assertEqual(bootstrap.provision(api, self.payload()), ("id-1", "updated"))
        self.assertEqual(
            api.request.call_args.args[:2], ("PUT", "/api/v2/dashboards/id-1")
        )

    def test_unchanged_dashboard_does_not_write(self):
        api = Mock()
        api.request.side_effect = [
            {"data": {"dashboards": [{"id": "id-1", "name": "factory"}], "total": 1}},
            {"data": self.payload()},
        ]
        self.assertEqual(
            bootstrap.provision(api, self.payload()), ("id-1", "unchanged")
        )
        self.assertEqual(api.request.call_count, 2)

    def test_lookup_paginates_and_does_not_match_display_name(self):
        api = Mock()
        api.request.side_effect = [
            {
                "data": {
                    "dashboards": [{"id": "other", "name": "unrelated"}],
                    "total": 2,
                }
            },
            {"data": {"dashboards": [{"id": "id-1", "name": "factory"}], "total": 2}},
        ]
        self.assertEqual(bootstrap.find_dashboard(api, "factory")["id"], "id-1")
        self.assertIn("offset=1", api.request.call_args.args[1])

    def test_locked_dashboard_is_not_overwritten(self):
        api = Mock()
        api.request.return_value = {
            "data": {
                "dashboards": [{"id": "id-1", "name": "factory", "locked": True}],
                "total": 1,
            }
        }
        with self.assertRaisesRegex(bootstrap.ProvisioningError, "locked"):
            bootstrap.provision(api, self.payload())
        self.assertEqual(api.request.call_count, 1)

    def test_existing_account_without_credentials_is_not_changed(self):
        api = Mock()
        api.request.return_value = {"setupCompleted": True}
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                bootstrap.ProvisioningError, "Existing SigNoz account"
            ):
                bootstrap.authenticate(api, {}, directory)
            self.assertFalse((Path(directory) / "login.json").exists())
        api.request.assert_called_once_with("GET", "/api/v1/version")

    def test_fresh_account_generates_private_credentials_then_authenticates(self):
        api = Mock(headers={})
        api.request.side_effect = [
            {"setupCompleted": False},
            {},
            {"data": {"orgs": [{"id": "org-1"}]}},
            {"data": {"accessToken": "test-session-secret"}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            bootstrap.authenticate(api, {}, directory)
            path = Path(directory) / "login.json"
            credentials = json.loads(path.read_text())
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            self.assertGreater(len(credentials["password"]), 20)
        self.assertEqual(api.headers["Authorization"], "Bearer test-session-secret")
        self.assertEqual(api.request.call_args.args[2]["orgId"], "org-1")

    def test_api_key_skips_account_bootstrap_and_session_login(self):
        api = Mock()
        bootstrap.authenticate(api, {"SIGNOZ_API_KEY": "test-key"}, "/unused")
        api.request.assert_not_called()

    def test_multiple_orgs_require_explicit_selection(self):
        api = Mock()
        api.request.side_effect = [
            {"setupCompleted": True},
            {"data": {"orgs": [{"id": "one"}, {"id": "two"}]}},
        ]
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(
                bootstrap.ProvisioningError, "unique organization"
            ):
                bootstrap.authenticate(
                    api,
                    {
                        "SIGNOZ_ADMIN_EMAIL": "admin@example.invalid",
                        "SIGNOZ_ADMIN_PASSWORD": "secret",
                    },
                    directory,
                )
        self.assertEqual(api.request.call_count, 2)

    def test_dashboard_file_has_api_metadata_and_resolvable_panel_links(self):
        path = (
            Path(__file__).resolve().parents[1]
            / "observability/dashboards/software-factory.json"
        )
        payload = json.loads(path.read_text())
        self.assertEqual(payload["schemaVersion"], "v6")
        self.assertEqual(payload["name"], "software-factory-workers-and-ai-usage")
        for layout in payload["spec"]["layouts"]:
            for item in layout["spec"]["items"]:
                self.assertIn(
                    item["content"]["$ref"].split("/")[-1], payload["spec"]["panels"]
                )


if __name__ == "__main__":
    unittest.main()
