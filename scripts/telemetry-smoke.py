"""Send clearly labelled synthetic telemetry without GitHub calls or model charges."""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "worker"))

from factory.settings import Settings
from factory.adapters.otel import OpenTelemetryObserver
from factory.adapters.telemetry import configure_logging
from factory.application.telemetry import ModelUsage, model_call

settings = Settings(
    worker_name="factory-telemetry-smoke",
    telemetry_endpoint="http://localhost:4318",
    deployment_environment="telemetry-smoke",
    pricing_file=str(
        Path(__file__).resolve().parents[1] / "observability/model-pricing.json"
    ),
)
configure_logging(settings)
observer = OpenTelemetryObserver(settings)
try:
    with observer.observe(
        "telemetry_smoke", {"factory_run_id": "synthetic-validation", "synthetic": True}
    ):
        with model_call("openai", "gpt-5-nano") as call:
            call.record(
                ModelUsage(
                    input_tokens=1000,
                    output_tokens=100,
                    cached_input_tokens=200,
                    response_model="gpt-5-nano",
                )
            )
    observer.flush()
finally:
    observer.shutdown()
print(
    "Synthetic telemetry exported. Filter deployment.environment=telemetry-smoke; no model API was called."
)
