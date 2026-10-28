"""Short-lived SDK runtimes must release pooled sockets, including failures."""

import unittest
from unittest.mock import Mock

from test_runtime_targets import target
from app.runtime_targets import TargetSnapshot, digest
from app.adapters.kubernetes_api import KubernetesApi


class RuntimeClientLifecycleTests(unittest.TestCase):
    def test_sdk_close_releases_rest_pool_as_well_as_sdk_thread_pool(self):
        config = target()
        snapshot = TargetSnapshot(
            id=config["id"],
            kind="kubernetes",
            revision=digest(config),
            configuration=config,
        )
        api = KubernetesApi(snapshot, clients={})
        sdk = Mock()
        api.api_client = sdk
        api.close()
        sdk.rest_client.pool_manager.clear.assert_called_once_with()
        sdk.close.assert_called_once_with()

    def test_sdk_error_responses_are_closed_before_unbounded_error_body_loading(self):
        from app.adapters.kubernetes_api import KubernetesError

        sdk = Mock()
        pool = sdk.rest_client.pool_manager
        response = Mock(status=403)
        pool.request.return_value = response
        KubernetesApi.bound_error_responses(sdk)
        with self.assertRaises(KubernetesError) as error:
            pool.request("GET", "https://fixture")
        self.assertEqual(error.exception.status, 403)
        response.read.assert_not_called()
        response.close.assert_called_once_with()
        response.release_conn.assert_called_once_with()
        response = Mock(status=200)
        pool.request.__wrapped__.return_value = response
        self.assertIs(pool.request("GET", "https://fixture"), response)
        response.close.assert_not_called()

    def test_admission_negative_probe_uses_a_valid_different_uid(self):
        import copy
        import uuid
        from app.adapters.kubernetes_api import KubernetesError

        config = target()
        snapshot = TargetSnapshot(
            id=config["id"],
            kind="kubernetes",
            revision=digest(config),
            configuration=config,
        )
        api = KubernetesApi(snapshot, clients={})
        expected = config["job_namespace_uid"]
        seen = []

        def raw(group, method, **kwargs):
            if method == "read_namespace":
                return {
                    "metadata": {
                        "uid": expected,
                        "annotations": {"flumic.io/namespace-uid": expected},
                    }
                }
            body = copy.deepcopy(kwargs["body"])
            seen.append(body)
            if body["metadata"]["annotations"]["flumic.io/namespace-uid"] != expected:
                raise KubernetesError(403)
            return body

        api._raw = raw
        api._admission_fence("job")
        incorrect = seen[-1]["metadata"]["annotations"]["flumic.io/namespace-uid"]
        self.assertNotEqual(incorrect, expected)
        self.assertEqual(str(uuid.UUID(incorrect)), incorrect)

    def test_runtime_scope_closes_on_success_and_failure(self):
        from app.ports import managed_runtime

        class Adapter:
            closed = 0

            def close(self):
                self.closed += 1

        adapter = Adapter()
        with managed_runtime(adapter):
            pass
        self.assertEqual(adapter.closed, 1)
        with self.assertRaises(ValueError):
            with managed_runtime(adapter):
                raise ValueError("fixture")
        self.assertEqual(adapter.closed, 2)
