# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Drives the UDP span export path against a local receiver and records what happened.

Datagrams are counted by wrapping the exporter's send method rather than inferred from payload sizes, so
a send that raises is still counted as attempted. Truncation and drop counts come from the distro's own
log records, which is the only place that information surfaces.
"""
import logging
import statistics
import time
from typing import Any, Callable, Dict, List

from benchmarks.runner.probe import ScenarioResult
from benchmarks.runner.resources import ResourceSampler

from . import host
from .receiver import UdpReceiver
from .spans import full_spans, lite_spans

# Time for the receiver thread to drain in-flight datagrams before counting
_DRAIN_SECONDS = 0.5

_DISTRO_LOGGERS = (
    "amazon.opentelemetry.distro._udp_span_payload",
    "amazon.opentelemetry.distro.opentelemetry_lite_sdk",
    "amazon.opentelemetry.distro.otlp_udp_exporter",
)


class _LogCapture(logging.Handler):
    """Counts the distro's warnings, which is where truncation and drops are reported."""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.messages: List[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.messages.append(record.getMessage())
        except Exception:  # pylint: disable=broad-except
            self.messages.append(str(record.msg))

    def count(self, needle: str) -> int:
        return sum(1 for m in self.messages if needle in m)

    def __enter__(self) -> "_LogCapture":
        for name in _DISTRO_LOGGERS:
            logger = logging.getLogger(name)
            logger.addHandler(self)
            logger.setLevel(logging.DEBUG)
        return self

    def __exit__(self, *_exc) -> None:
        for name in _DISTRO_LOGGERS:
            logging.getLogger(name).removeHandler(self)


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[rank]


def _build_lite_exporter(endpoint: str):
    from amazon.opentelemetry.distro import opentelemetry_lite_sdk as lite

    exporter = lite.UdpSpanExporter(endpoint=endpoint)
    inner = exporter._udp_exporter  # pylint: disable=protected-access
    original = inner.send_otlp
    counters = {"datagrams": 0, "bytes": 0, "send_errors": 0}

    def counted(data, prefix="T1U"):
        counters["datagrams"] += 1
        counters["bytes"] += len(data)
        try:
            return original(data, prefix=prefix)
        except Exception:
            counters["send_errors"] += 1
            raise

    inner.send_otlp = counted
    return exporter, counters, exporter.export


def _build_full_exporter(endpoint: str):
    from amazon.opentelemetry.distro.otlp_udp_exporter import OTLPUdpSpanExporter

    exporter = OTLPUdpSpanExporter(endpoint=endpoint, sampled=True)
    inner = exporter._udp_exporter  # pylint: disable=protected-access
    original = inner.send_data
    counters = {"datagrams": 0, "bytes": 0, "send_errors": 0}

    def counted(data, signal_format_prefix):
        counters["datagrams"] += 1
        counters["bytes"] += len(data)
        try:
            return original(data=data, signal_format_prefix=signal_format_prefix)
        except Exception:
            counters["send_errors"] += 1
            raise

    inner.send_data = counted
    return exporter, counters, exporter.export


def run(scenario) -> ScenarioResult:
    """Execute one scenario. Benchmark-visible failures are reported, not raised."""
    try:
        return _run(scenario)
    except Exception as exc:  # pylint: disable=broad-except
        return ScenarioResult(
            scenario_id=scenario.id,
            ok=False,
            error=f"{type(exc).__name__}: {exc}",
        )


def _run(scenario) -> ScenarioResult:
    params: Dict[str, Any] = scenario.params
    host.check(allow_undersized=bool(params.get("allow_undersized_host", False)))
    mode = params.get("mode", "lite")
    if mode not in ("lite", "full"):
        raise ValueError(f"scenario {scenario.id}: mode must be lite or full, got {mode!r}")
    rcvbuf = int(params.get("rcvbuf", 212_992))

    make_spans: Callable[..., List[Any]] = lite_spans if mode == "lite" else full_spans
    spans = make_spans(
        int(params.get("spans", 10)),
        int(params.get("attr_count", 4)),
        int(params.get("attr_bytes", 256)),
        bool(params.get("unicode", False)),
        bool(params.get("url_like", False)),
    )

    latencies_ms: List[float] = []
    export_failures = 0

    with UdpReceiver(rcvbuf=rcvbuf) as receiver:
        build = _build_lite_exporter if mode == "lite" else _build_full_exporter
        exporter, counters, export = build(receiver.endpoint)

        with _LogCapture() as logs, ResourceSampler() as sampler:
            for _ in range(scenario.repeats):
                started = time.perf_counter()
                outcome = export(spans)
                latencies_ms.append((time.perf_counter() - started) * 1000)
                # lite returns bool, full returns SpanExportResult
                if outcome is False or getattr(outcome, "name", "SUCCESS") != "SUCCESS":
                    export_failures += 1

        time.sleep(_DRAIN_SECONDS)
        sent_datagrams = counters["datagrams"]
        sent_bytes = counters["bytes"]
        received_datagrams = receiver.datagrams
        received_bytes = receiver.bytes
        effective_rcvbuf = receiver.effective_rcvbuf
        requested_rcvbuf = receiver.requested_rcvbuf
        exporter.shutdown()

    metrics = {
        "datagrams_sent": float(sent_datagrams),
        "datagrams_received": float(received_datagrams),
        "datagrams_per_export": sent_datagrams / scenario.repeats,
        "bytes_sent": float(sent_bytes),
        "bytes_received": float(received_bytes),
        "delivery_rate_pct": (100.0 * received_datagrams / sent_datagrams) if sent_datagrams else 0.0,
        "export_failures": float(export_failures),
        "send_errors": float(counters["send_errors"]),
        "truncated_spans": float(logs.count("Truncated")),
        "dropped_spans": float(logs.count("Dropping")),
        "message_too_long": float(logs.count("Message too long")),
        "blocking_io_errors": float(logs.count("BlockingIOError")),
        "export_latency_p50_ms": _percentile(latencies_ms, 50),
        "export_latency_p95_ms": _percentile(latencies_ms, 95),
        "export_latency_p99_ms": _percentile(latencies_ms, 99),
        "export_latency_mean_ms": statistics.fmean(latencies_ms) if latencies_ms else 0.0,
        "effective_rcvbuf_bytes": float(effective_rcvbuf),
        "requested_rcvbuf_bytes": float(requested_rcvbuf),
        # <1 means the receive buffer cannot hold a single datagram of this size, so loss is the
        # harness's buffer rather than the burst behaviour being measured
        "rcvbuf_datagram_headroom": (
            effective_rcvbuf / (sent_bytes / sent_datagrams) if sent_datagrams and sent_bytes else 0.0
        ),
        "host_max_datagram_bytes": float(host.max_sendable_datagram()),
    }
    if sampler.usage:
        metrics.update(
            {
                "cpu_user_s": sampler.usage.cpu_user_s,
                "cpu_sys_s": sampler.usage.cpu_sys_s,
                "cpu_total_s": sampler.usage.cpu_user_s + sampler.usage.cpu_sys_s,
                "peak_rss_bytes": float(sampler.usage.peak_rss_bytes),
                "rss_delta_bytes": float(sampler.usage.rss_delta_bytes),
            }
        )

    return ScenarioResult(scenario_id=scenario.id, ok=True, metrics=metrics, labels={"mode": mode})
