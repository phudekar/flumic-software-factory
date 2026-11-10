"""Broker scope and allowance contracts at the trusted service boundary."""
import sys
import unittest
from pathlib import Path

from pydantic import ValidationError

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.domain.broker import CapabilityScope, SessionLimits


class BrokerContractTests(unittest.TestCase):
    def test_scope_requires_all_attempt_coordinates_and_a_closed_audience(self):
        fields = {
            "project_id": "project",
            "run_id": "run",
            "stage_id": "stage",
            "attempt_id": "attempt",
            "assignment_id": "assignment",
            "audience": "model",
            "operations": ("model.infer",),
        }
        scope = CapabilityScope(**fields)
        self.assertEqual(scope.model_dump(), fields)
        for key in fields:
            with self.subTest(key=key), self.assertRaises(ValidationError):
                CapabilityScope(**{**fields, key: ""})
        with self.assertRaises(ValidationError):
            CapabilityScope(**{**fields, "audience": "arbitrary-proxy"})

    def test_limits_bound_lifetime_and_concurrency_without_coercion(self):
        fields = {
            "request_count": 2,
            "concurrent_requests": 1,
            "input_tokens": 1000,
            "output_tokens": 200,
            "ttl_seconds": 300,
        }
        self.assertEqual(SessionLimits(**fields).model_dump(), fields)
        for key, value in (
            ("ttl_seconds", 301),
            ("ttl_seconds", 0),
            ("concurrent_requests", 3),
            ("request_count", True),
            ("input_tokens", -1),
            ("output_tokens", "200"),
        ):
            with self.subTest(key=key, value=value), self.assertRaises(ValidationError):
                SessionLimits(**{**fields, key: value})
