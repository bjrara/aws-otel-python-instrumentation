# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Drives the deployed function and reads back what Lambda itself reported.

Throughput and latency come from concurrent lambda:Invoke rather than an HTTP endpoint, so nothing has
to be made reachable from outside the account. Memory and duration come from the REPORT line Lambda
writes per invocation, which is authoritative in a way a client-side measurement is not.

Cold starts are measured separately rather than averaged in: the first invocation of a fresh function
pays init cost that says nothing about the change under test.
"""
import json
import re
import statistics
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Dict, List

import boto3
from botocore.config import Config

from .stack import REGION, Stack

# Lambda buffers log delivery to CloudWatch; REPORT lines are not queryable the instant an invoke returns
_LOG_SETTLE_S = 20
_LOG_POLL_ATTEMPTS = 6

_REPORT_FIELDS = {
    "billed_duration_ms": r"Billed Duration: ([\d.]+) ms",
    "duration_ms": r"\tDuration: ([\d.]+) ms",
    "max_memory_used_mb": r"Max Memory Used: (\d+) MB",
    "memory_size_mb": r"Memory Size: (\d+) MB",
    "init_duration_ms": r"Init Duration: ([\d.]+) ms",
}


@dataclass
class InvokeOutcome:
    latencies_ms: List[float] = field(default_factory=list)
    errors: int = 0
    wall_seconds: float = 0.0
    reports: List[Dict[str, float]] = field(default_factory=list)
    truncation_events: int = 0
    drop_events: int = 0
    emsgsize_events: int = 0
    export_error_events: int = 0


def _percentile(values: List[float], pct: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return values[0]
    ordered = sorted(values)
    rank = max(0, min(len(ordered) - 1, int(round(pct / 100 * (len(ordered) - 1)))))
    return ordered[rank]


def warm(stack: Stack, event: Dict) -> None:
    """One invocation so the measured ones are not paying init cost."""
    client = boto3.client("lambda", config=Config(region_name=REGION, read_timeout=120))
    client.invoke(FunctionName=stack.function_name, Payload=json.dumps(event).encode())


def drive(stack: Stack, event: Dict, invocations: int, concurrency: int) -> InvokeOutcome:
    client = boto3.client(
        "lambda",
        config=Config(
            region_name=REGION,
            read_timeout=120,
            retries={"max_attempts": 0},  # a retry would corrupt the latency and throughput figures
            max_pool_connections=max(concurrency, 10),
        ),
    )
    payload = json.dumps(event).encode()
    outcome = InvokeOutcome()

    def one() -> None:
        started = time.perf_counter()
        try:
            response = client.invoke(FunctionName=stack.function_name, Payload=payload)
            if response.get("FunctionError"):
                outcome.errors += 1
        except Exception:  # pylint: disable=broad-except
            outcome.errors += 1
        outcome.latencies_ms.append((time.perf_counter() - started) * 1000)

    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        list(pool.map(lambda _: one(), range(invocations)))
    outcome.wall_seconds = time.perf_counter() - started

    _collect_logs(stack, outcome)
    return outcome


def _collect_logs(stack: Stack, outcome: InvokeOutcome) -> None:
    logs = boto3.client("logs", config=Config(region_name=REGION))
    time.sleep(_LOG_SETTLE_S)
    messages: List[str] = []
    for attempt in range(_LOG_POLL_ATTEMPTS):
        messages = _fetch(logs, stack.log_group)
        if any("REPORT" in m for m in messages):
            break
        time.sleep(5 * (attempt + 1))

    for message in messages:
        if message.startswith("REPORT") or "\tREPORT" in message:
            parsed = {}
            for key, pattern in _REPORT_FIELDS.items():
                found = re.search(pattern, message)
                if found:
                    parsed[key] = float(found.group(1))
            if parsed:
                outcome.reports.append(parsed)
        if "Truncated" in message:
            outcome.truncation_events += 1
        if "Dropping" in message:
            outcome.drop_events += 1
        if "Message too long" in message:
            outcome.emsgsize_events += 1
        if "Error sending" in message or "Failed to export" in message:
            outcome.export_error_events += 1


def _fetch(logs, log_group: str) -> List[str]:
    try:
        paginator = logs.get_paginator("filter_log_events")
        return [
            event["message"]
            for page in paginator.paginate(logGroupName=log_group)
            for event in page.get("events", [])
        ]
    except logs.exceptions.ResourceNotFoundException:
        return []


def metrics(outcome: InvokeOutcome) -> Dict[str, float]:
    """Flatten an outcome into the metric keys a plan can reference."""
    cold = [r["init_duration_ms"] for r in outcome.reports if "init_duration_ms" in r]
    warm_durations = [
        r["duration_ms"] for r in outcome.reports if "duration_ms" in r and "init_duration_ms" not in r
    ]
    memory_used = [r["max_memory_used_mb"] for r in outcome.reports if "max_memory_used_mb" in r]
    billed = [r["billed_duration_ms"] for r in outcome.reports if "billed_duration_ms" in r]

    return {
        "invocations": float(len(outcome.latencies_ms)),
        "invoke_errors": float(outcome.errors),
        "throughput_rps": (len(outcome.latencies_ms) / outcome.wall_seconds) if outcome.wall_seconds else 0.0,
        "invoke_latency_p50_ms": _percentile(outcome.latencies_ms, 50),
        "invoke_latency_p99_ms": _percentile(outcome.latencies_ms, 99),
        "lambda_duration_p50_ms": _percentile(warm_durations, 50),
        "lambda_duration_p99_ms": _percentile(warm_durations, 99),
        "lambda_billed_duration_mean_ms": statistics.fmean(billed) if billed else 0.0,
        "cold_start_init_ms_mean": statistics.fmean(cold) if cold else 0.0,
        "cold_starts": float(len(cold)),
        "max_memory_used_mb": max(memory_used) if memory_used else 0.0,
        "reports_seen": float(len(outcome.reports)),
        "truncated_spans": float(outcome.truncation_events),
        "dropped_spans": float(outcome.drop_events),
        "message_too_long": float(outcome.emsgsize_events),
        "export_errors": float(outcome.export_error_events),
    }
