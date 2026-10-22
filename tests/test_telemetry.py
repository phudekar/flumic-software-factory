import json
import os
from pathlib import Path
import sys
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import SimpleSpanProcessor
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import InMemoryMetricReader

from factory.adapters.otel import OpenTelemetryObserver
from factory.adapters.cli_usage import record_codex_usage, read_claude_result
from factory.adapters.pricing import Pricing
from factory.application.telemetry import (
    ModelUsage,
    model_call,
    capture_trace,
    incoming_trace,
)
from factory.settings import Settings


class TelemetryTests(unittest.TestCase):
    def setUp(self):
        self.exporter = InMemorySpanExporter()
        self.tracer_provider = TracerProvider()
        self.tracer_provider.add_span_processor(SimpleSpanProcessor(self.exporter))
        self.reader = InMemoryMetricReader()
        self.meter_provider = MeterProvider(metric_readers=[self.reader])
        with patch.dict(os.environ, {"MODEL_PRICING_FILE": ""}):
            self.observer = OpenTelemetryObserver(
                Settings(),
                tracer_provider=self.tracer_provider,
                meter_provider=self.meter_provider,
            )
        self.addCleanup(self.observer.shutdown)

    def test_stage_and_model_usage_share_trace_without_exporting_contents(self):
        with self.observer.observe(
            "implement", {"repository": "owner/repo", "issue_number": 2}
        ):
            with model_call("openai", "test-model", iteration=2) as call:
                call.record(
                    ModelUsage(
                        input_tokens=100,
                        output_tokens=10,
                        cached_input_tokens=20,
                        reasoning_tokens=5,
                        response_model="test-model-actual",
                    )
                )
        model, stage = self.exporter.get_finished_spans()
        self.assertEqual(model.context.trace_id, stage.context.trace_id)
        self.assertEqual(model.parent.span_id, stage.context.span_id)
        self.assertEqual(model.attributes["gen_ai.usage.input_tokens"], 100)
        self.assertEqual(model.attributes["gen_ai.response.model"], "test-model-actual")
        self.assertEqual(model.attributes["factory.issue_number"], 2)
        self.assertEqual(model.attributes["factory.iteration"], 2)
        self.assertNotIn("gen_ai.input.messages", model.attributes)

    def test_unknown_usage_has_no_zero_token_attributes(self):
        with self.observer.observe("review", {}):
            with model_call("openai", "unknown-model"):
                pass
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes["factory.usage.status"], "unknown")
        self.assertNotIn("gen_ai.usage.input_tokens", span.attributes)
        self.assertNotIn("factory.cost.estimated_usd", span.attributes)

    def test_work_item_contention_is_waiting_without_error_span(self):
        from factory.domain.models import OperationBusy

        with self.assertRaises(OperationBusy), self.observer.observe("implement", {}):
            raise OperationBusy("Another execution owns this work item")
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes["factory.outcome"], "waiting")
        self.assertNotIn("error.type", span.attributes)
        self.assertNotEqual(span.status.status_code.name, "ERROR")

    def test_exception_text_and_stack_trace_are_not_exported(self):
        with self.assertRaises(RuntimeError):
            with self.observer.observe("implement", {}):
                with model_call("openai", "test"):
                    raise RuntimeError("confidential source contents")
        for span in self.exporter.get_finished_spans():
            self.assertEqual(span.status.status_code.name, "ERROR")
            self.assertEqual(span.events, ())
            self.assertNotIn("confidential source", str(span.attributes))

    def test_trace_context_survives_task_boundary(self):
        with self.observer.observe("dispatch", {}):
            carrier = capture_trace()
        with incoming_trace(carrier), self.observer.observe("implement", {}):
            pass
        first, second = self.exporter.get_finished_spans()
        self.assertEqual(first.context.trace_id, second.context.trace_id)
        self.assertEqual(first.context.span_id, second.parent.span_id)

    def test_cached_and_reasoning_tokens_are_not_double_charged(self):
        self.observer.pricing.models = {
            "openai/test": {"input": 2, "cached_input": 0.2, "output": 4}
        }
        with self.observer.observe("implement", {}):
            with model_call("openai", "test") as call:
                usage = ModelUsage(
                    input_tokens=100,
                    output_tokens=10,
                    cached_input_tokens=20,
                    reasoning_tokens=5,
                )
                call.record(usage)
                call.record(
                    usage
                )  # Duplicate accounting reports do not increment again.
        span = self.exporter.get_finished_spans()[0]
        self.assertAlmostEqual(
            span.attributes["factory.cost.estimated_usd"], 204 / 1_000_000
        )
        self.assertEqual(span.attributes["factory.cost.status"], "estimated")

    def test_heartbeat_and_active_task_metrics_return_to_zero(self):
        with self.observer.observe("implement", {}):
            self.assertEqual(list(self.observer._active_tasks(None))[0].value, 1)
        samples = list(self.observer._active_tasks(None))
        self.assertTrue(all(sample.value == 0 for sample in samples))
        self.assertGreater(list(self.observer._heartbeat(None))[0].value, 0)
        data = self.reader.get_metrics_data()
        names = {
            metric.name
            for resource in data.resource_metrics
            for scope in resource.scope_metrics
            for metric in scope.metrics
        }
        self.assertIn("factory.operation.count", names)
        self.assertIn("factory.worker.heartbeat", names)

    def test_codex_counts_completed_turns_only(self):
        events = [
            {"type": "item.completed", "item": {"text": "private source"}},
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 100,
                    "cached_input_tokens": 50,
                    "output_tokens": 10,
                },
            },
            {
                "type": "turn.completed",
                "usage": {
                    "input_tokens": 200,
                    "cached_input_tokens": 100,
                    "output_tokens": 20,
                },
            },
        ]
        with self.observer.observe("implement", {}):
            with model_call("openai", "codex-model", agent="codex"):
                record_codex_usage("\n".join(json.dumps(event) for event in events))
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 300)
        self.assertEqual(span.attributes["gen_ai.usage.cache_read.input_tokens"], 150)
        self.assertNotIn("private source", str(span.attributes))

    def test_claude_records_actual_model_and_normalizes_cached_input(self):
        payload = {
            "result": "completion text",
            "modelUsage": {
                "claude-test": {
                    "inputTokens": 80,
                    "cacheReadInputTokens": 20,
                    "cacheCreationInputTokens": 30,
                    "outputTokens": 10,
                    "costUSD": 0.001,
                }
            },
        }
        with self.observer.observe("implement", {}):
            with model_call("anthropic", None, agent="claude"):
                result = read_claude_result(json.dumps(payload))
        self.assertEqual(result, "completion text")
        span = self.exporter.get_finished_spans()[0]
        self.assertEqual(span.attributes["gen_ai.request.model"], "claude-test")
        self.assertEqual(span.attributes["gen_ai.usage.input_tokens"], 100)
        self.assertEqual(
            span.attributes["gen_ai.usage.cache_creation.input_tokens"], 30
        )
        self.assertEqual(span.attributes["factory.cost.reported_usd"], 0.001)
        self.assertNotIn("completion text", str(span.attributes))

    def test_malformed_cli_accounting_does_not_fail_successful_generation(self):
        with self.observer.observe("implement", {}):
            with model_call("openai", "test", agent="codex"):
                record_codex_usage(
                    json.dumps(
                        {
                            "type": "turn.completed",
                            "usage": {"input_tokens": 1, "cached_input_tokens": 20},
                        }
                    )
                )
        self.assertEqual(
            self.exporter.get_finished_spans()[0].attributes["factory.usage.status"],
            "unknown",
        )

    def test_api_success_records_provider_usage(self):
        from model_client import ModelClient

        client = ModelClient.__new__(ModelClient)
        client.model = "gpt-4.1-mini"
        client.client = Mock()
        client.client.chat.completions.create.return_value = SimpleNamespace(
            model="reported-model",
            choices=[SimpleNamespace(message=SimpleNamespace(content='{"ok":true}'))],
            usage=SimpleNamespace(
                prompt_tokens=100,
                completion_tokens=10,
                prompt_tokens_details=SimpleNamespace(cached_tokens=20),
                completion_tokens_details=SimpleNamespace(reasoning_tokens=5),
            ),
        )
        with self.observer.observe("planning", {}):
            self.assertEqual(
                client._complete([{"role": "user", "content": "task"}]), '{"ok":true}'
            )
        self.assertEqual(
            self.exporter.get_finished_spans()[0].attributes[
                "gen_ai.usage.output_tokens"
            ],
            10,
        )

    def test_pricing_requires_known_usage_and_model(self):
        pricing = Pricing()
        self.assertIsNone(
            pricing.estimate(
                "openai", "unknown", ModelUsage(input_tokens=10, output_tokens=2)
            )
        )
        pricing.models = {"openai/test": {"input": 2, "output": 4, "cached_input": 0.2}}
        self.assertIsNone(
            pricing.estimate(
                "openai", "test", ModelUsage(input_tokens=10, output_tokens=2)
            )
        )


if __name__ == "__main__":
    unittest.main()
