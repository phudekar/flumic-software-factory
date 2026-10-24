"""OTLP/HTTP implementation. Vendor-neutral signals, metadata only."""

from __future__ import annotations

import atexit
import logging
import os
import socket
import threading
import time
import uuid
from contextlib import contextmanager
from contextvars import ContextVar

from factory.workflows.redaction import redact
from factory.domain.models import OperationBusy
from opentelemetry import metrics, trace
from opentelemetry.propagators.textmap import DefaultGetter
from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator
from opentelemetry.sdk.resources import Resource
from opentelemetry.sdk.trace import TracerProvider
from opentelemetry.sdk.trace.export import BatchSpanProcessor
from opentelemetry.sdk.metrics import MeterProvider
from opentelemetry.sdk.metrics.export import PeriodicExportingMetricReader
from opentelemetry.sdk._logs import LoggerProvider, LoggingHandler
from opentelemetry.sdk._logs.export import BatchLogRecordProcessor
from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
from opentelemetry.exporter.otlp.proto.http.metric_exporter import OTLPMetricExporter
from opentelemetry.exporter.otlp.proto.http._log_exporter import OTLPLogExporter

from factory.application.telemetry import (
    incoming_carrier,
    use_recorder,
    action_metadata,
)
from factory.adapters.telemetry import (
    CorrelationFilter,
    LoggingObserver,
    current_metadata,
)
from factory.adapters.pricing import Pricing
from factory.domain.safety import find_sensitive_categories


class MetadataHandler(LoggingHandler):
    def emit(self, record):
        assignment = getattr(self, "assignment_id", None)
        if (
            assignment is not None
            and action_metadata().get("assignment_id") != assignment
        ):
            return
        import copy

        record = copy.copy(record)
        if MetadataFilter().filter(record):
            super().emit(record)


class MetadataFilter(CorrelationFilter):
    """Export only factory records; redact before the OTLP log handler sees them."""

    def filter(self, record):
        if not record.name.startswith("issue-worker"):
            return False
        super().filter(record)
        for key, value in record.factory_context.items():
            setattr(record, "factory." + key, value)
        # Non-scalar dict context is not needed as an OTLP attribute.
        record.factory_context = str(record.factory_context)
        return True


def safe_attributes(values):
    result = {}
    authority = {"factory." + key: value for key, value in action_metadata().items()}
    for key, value in values.items():
        value = authority[key] if key in authority else redact(value)
        if value is None:
            continue
        if isinstance(value, str):
            value = (
                "[redacted]"
                if key not in authority and find_sensitive_categories(value)
                else value[:512]
            )
        if isinstance(value, (str, int, float, bool)):
            result[key] = value
    return result


class Call:
    def __init__(self, observer, span, attributes):
        self.observer, self.span, self.attributes = observer, span, attributes
        self.recorded = False
        self.usage = None
        self.leaf = True

    def record(self, usage):
        # One final usage report per call; callers never add cumulative snapshots.
        if self.recorded:
            return
        self.recorded = True
        self.usage = usage

    def flush(self):
        self.span.set_attribute("factory.accounting_leaf", self.leaf)
        if self.usage is None:
            return
        usage = self.usage
        estimate = self.observer.pricing.estimate(
            self.attributes["provider"], self.attributes["model"], usage
        )
        self.span.set_attribute(
            "factory.cost.status", "estimated" if estimate is not None else "unknown"
        )
        if estimate is not None:
            self.span.set_attribute("factory.cost.estimated_usd", estimate)
            if self.leaf:
                self.observer.estimated_cost.add(estimate, self.attributes)
        attrs = {
            "gen_ai.usage.input_tokens": usage.input_tokens,
            "gen_ai.usage.output_tokens": usage.output_tokens,
            "gen_ai.usage.cache_read.input_tokens": usage.cached_input_tokens,
            "gen_ai.usage.cache_creation.input_tokens": usage.cache_creation_tokens,
            "gen_ai.usage.reasoning.output_tokens": usage.reasoning_tokens,
            "gen_ai.response.model": usage.response_model,
            "factory.usage.status": "reported"
            if usage.input_tokens is not None and usage.output_tokens is not None
            else "unknown",
            "factory.cost.reported_usd": usage.reported_cost_usd,
        }
        self.span.set_attributes(safe_attributes(attrs))
        for category, count in (
            ("input", usage.input_tokens),
            ("output", usage.output_tokens),
            ("cache_read", usage.cached_input_tokens),
            ("cache_creation", usage.cache_creation_tokens),
        ):
            if count is not None and self.leaf:
                self.observer.tokens.add(
                    count, {**self.attributes, "token.type": category}
                )
        if usage.reported_cost_usd is not None and self.leaf:
            self.observer.reported_cost.add(usage.reported_cost_usd, self.attributes)


class OpenTelemetryObserver(LoggingObserver):
    def __init__(
        self,
        settings,
        *,
        tracer_provider=None,
        meter_provider=None,
        logger_provider=None,
    ):
        self.settings = settings
        self.infrastructure = None
        self.pricing = Pricing(settings.pricing_file or None)
        self.stage = ContextVar("factory_otel_stage", default="unscoped")
        self.parent_call = ContextVar("factory_otel_parent_call", default=None)
        self.worker_id = (
            settings.worker_name or f"software-factory-{settings.worker_type}-worker"
        )
        self.active = {}
        self.seen_stages = set()
        self.lock = threading.Lock()
        self.closed = False
        self.resource = Resource.create(
            {
                "service.name": "software-factory",
                "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                "deployment.environment": settings.deployment_environment,
                "factory.worker.name": self.worker_id,
                "factory.worker.type": settings.worker_type,
                "factory.process.role": os.getenv(
                    "FACTORY_TELEMETRY_PROCESS_ROLE", "worker"
                ),
            }
        )
        self.tracer_provider = tracer_provider or TracerProvider(resource=self.resource)
        if tracer_provider is None:
            self.tracer_provider.add_span_processor(
                BatchSpanProcessor(
                    OTLPSpanExporter(
                        endpoint=settings.telemetry_endpoint.rstrip("/") + "/v1/traces",
                        timeout=5,
                    )
                )
            )
        self.tracer = self.tracer_provider.get_tracer("software-factory")
        if meter_provider is None:
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=settings.telemetry_endpoint.rstrip("/") + "/v1/metrics",
                    timeout=5,
                ),
                export_interval_millis=15000,
            )
            meter_provider = MeterProvider(
                resource=self.resource, metric_readers=[reader]
            )
        self.meter_provider = meter_provider
        meter = meter_provider.get_meter("software-factory")
        self.completed = meter.create_counter(
            "factory.operation.count",
            description="Completed stage attempts, including failures",
        )
        self.duration = meter.create_histogram("factory.operation.duration", unit="s")
        self.calls = meter.create_counter("factory.model.call.count")
        self.tokens = meter.create_counter("factory.model.tokens", unit="{token}")
        self.estimated_cost = meter.create_counter(
            "factory.model.estimated_cost", unit="USD"
        )
        self.reported_cost = meter.create_counter(
            "factory.model.reported_cost", unit="USD"
        )
        if (
            os.getenv("FACTORY_TELEMETRY_PROCESS_ROLE", "worker") == "worker"
            and not action_metadata()
        ):
            meter.create_observable_gauge(
                "factory.worker.heartbeat", callbacks=[self._heartbeat], unit="s"
            )
            meter.create_observable_gauge(
                "factory.worker.active_tasks", callbacks=[self._active_tasks]
            )
        self.logger_provider = logger_provider
        self.log_handler = None
        if tracer_provider is None:
            self.logger_provider = logger_provider or LoggerProvider(
                resource=self.resource
            )
            self.logger_provider.add_log_record_processor(
                BatchLogRecordProcessor(
                    OTLPLogExporter(
                        endpoint=settings.telemetry_endpoint.rstrip("/") + "/v1/logs",
                        timeout=5,
                    )
                )
            )
            self.log_handler = MetadataHandler(logger_provider=self.logger_provider)
            logging.getLogger().addHandler(self.log_handler)
        atexit.register(self.shutdown)

    def attach_infrastructure(self, infrastructure):
        self.infrastructure = infrastructure
        infrastructure.activate(self.settings.telemetry_endpoint)

    def _heartbeat(self, options):
        if self.infrastructure is not None:
            return
        yield metrics.Observation(
            time.time(),
            {"worker.name": self.worker_id, "worker.type": self.settings.worker_type},
        )

    def _active_tasks(self, options):
        if self.infrastructure is not None:
            return
        with self.lock:
            counts = {stage: 0 for stage in self.seen_stages}
            for attrs in self.active.values():
                stage = attrs["factory.stage"]
                counts[stage] = counts.get(stage, 0) + 1
        # Emit zero when idle; stale time series should not look active forever.
        yield metrics.Observation(
            sum(counts.values()), {"worker.name": self.worker_id, "stage": "all"}
        )
        for stage, count in counts.items():
            yield metrics.Observation(
                count, {"worker.name": self.worker_id, "stage": stage}
            )

    @contextmanager
    def observe(self, stage, attributes):
        attrs = safe_attributes(
            {
                "factory.stage": stage,
                **{"factory." + k: v for k, v in attributes.items()},
                **{"factory." + k: v for k, v in action_metadata().items()},
            }
        )
        parent = (
            TraceContextTextMapPropagator().extract(
                incoming_carrier(), getter=DefaultGetter()
            )
            if incoming_carrier()
            else None
        )
        started, attempt = time.monotonic(), str(uuid.uuid4())
        outcome = "completed"
        with self.tracer.start_as_current_span(
            stage,
            context=parent,
            attributes=attrs,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            with self.lock:
                self.active[attempt] = attrs
                self.seen_stages.add(stage)
            try:
                stage_token = self.stage.set(stage)
                try:
                    with use_recorder(self), super().observe(stage, attributes):
                        yield
                finally:
                    self.stage.reset(stage_token)
            except OperationBusy:
                outcome = "waiting"
                span.set_attribute("factory.wait.reason", "work_item_busy")
                raise
            except BaseException as error:
                outcome = "failed"
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(trace.StatusCode.ERROR)
                raise
            finally:
                with self.lock:
                    self.active.pop(attempt, None)
                span.set_attribute("factory.outcome", outcome)
                labels = {
                    "stage": stage,
                    "outcome": outcome,
                    "worker.type": self.settings.worker_type,
                    **safe_attributes(
                        {"factory." + k: v for k, v in action_metadata().items()}
                    ),
                }
                self.completed.add(1, labels)
                self.duration.record(time.monotonic() - started, labels)

    def capture_trace(self):
        carrier = {}
        TraceContextTextMapPropagator().inject(carrier)
        return carrier

    @contextmanager
    def model_call(self, provider, model, *, agent="api", iteration=None):
        attrs = safe_attributes(
            {
                "gen_ai.provider.name": provider,
                "gen_ai.request.model": model,
                "gen_ai.operation.name": "chat" if agent == "api" else "invoke_agent",
                "gen_ai.agent.name": agent,
                "factory.iteration": iteration,
            }
        )
        attrs.update(
            safe_attributes(
                {"factory." + key: value for key, value in current_metadata().items()}
            )
        )
        labels = safe_attributes(
            {
                "provider": provider,
                "model": model or "unknown",
                "agent": agent,
                "stage": self.stage.get(),
                **{"factory." + k: v for k, v in action_metadata().items()},
            }
        )
        outcome = "completed"
        with self.tracer.start_as_current_span(
            "model." + agent,
            attributes=attrs,
            record_exception=False,
            set_status_on_exception=False,
        ) as span:
            span.set_attribute("factory.usage.status", "unknown")
            span.set_attribute("factory.cost.status", "unknown")
            call = Call(self, span, labels)
            parent = self.parent_call.get()
            if parent is not None:
                parent.leaf = False
            parent_token = self.parent_call.set(call)
            try:
                yield call
            except BaseException as error:
                outcome = "failed"
                span.set_attribute("error.type", type(error).__name__)
                span.set_status(trace.StatusCode.ERROR)
                raise
            finally:
                self.parent_call.reset(parent_token)
                call.flush()
                if model is not None and call.leaf:
                    self.calls.add(1, {**labels, "outcome": outcome})

    def flush(self):
        self.tracer_provider.force_flush(timeout_millis=5000)
        self.meter_provider.force_flush(timeout_millis=5000)
        if self.logger_provider:
            self.logger_provider.force_flush(timeout_millis=5000)

    def shutdown(self):
        if self.closed:
            return
        self.closed = True
        atexit.unregister(self.shutdown)
        if self.log_handler:
            logging.getLogger().removeHandler(self.log_handler)
        self.tracer_provider.shutdown()
        self.meter_provider.shutdown()
        if self.logger_provider:
            self.logger_provider.shutdown()


class WorkerTelemetry:
    """One worker-lifetime gauge source, independent of action-specific providers."""

    def __init__(self, settings, *, endpoint="", environment="development"):
        self.worker_id, self.worker_name, self.role = (
            settings.worker_id,
            settings.worker_name,
            settings.role,
        )
        self.endpoint, self.environment = endpoint, environment
        self.lock = threading.RLock()
        self.running = 0
        self.provider = None
        self.closed = False
        self.current = ContextVar("factory_shared_worker_execution", default=None)
        if endpoint:
            self.activate(endpoint)

    def activate(self, endpoint):
        with self.lock:
            if self.provider is not None or self.closed:
                return
            endpoint = self.endpoint or endpoint
            if not endpoint:
                return
            resource = Resource.create(
                {
                    "service.name": "software-factory",
                    "service.instance.id": f"{socket.gethostname()}:{os.getpid()}",
                    "deployment.environment": self.environment,
                    "factory.worker.id": self.worker_id,
                    "factory.worker.name": self.worker_name,
                    "factory.worker.type": self.role,
                    "factory.process.role": "worker",
                    "factory.worker.protocol": "shared-v1",
                }
            )
            reader = PeriodicExportingMetricReader(
                OTLPMetricExporter(
                    endpoint=endpoint.rstrip("/") + "/v1/metrics", timeout=5
                ),
                export_interval_millis=15000,
            )
            provider = MeterProvider(resource=resource, metric_readers=[reader])
            meter = provider.get_meter("software-factory.worker")
            meter.create_observable_gauge(
                "factory.worker.heartbeat", callbacks=[self.heartbeat], unit="s"
            )
            meter.create_observable_gauge(
                "factory.worker.active_tasks", callbacks=[self.active_tasks]
            )
            self.provider = provider
            self.endpoint = endpoint

    @contextmanager
    def track(self, execution_id):
        if self.current.get() == execution_id:
            yield
            return
        token = self.current.set(execution_id)
        with self.lock:
            self.running += 1
        try:
            yield
        finally:
            with self.lock:
                self.running -= 1
            self.current.reset(token)

    def labels(self):
        return {
            "worker.id": self.worker_id,
            "worker.name": self.worker_name,
            "worker.type": self.role,
        }

    def heartbeat(self, options):
        yield metrics.Observation(time.time(), self.labels())

    def active_tasks(self, options):
        with self.lock:
            count = self.running
        yield metrics.Observation(count, {**self.labels(), "stage": "all"})

    def shutdown(self):
        with self.lock:
            if self.closed:
                return
            self.closed = True
            provider = self.provider
        if provider:
            provider.shutdown()


def otel_observer(settings):
    return OpenTelemetryObserver(settings)
