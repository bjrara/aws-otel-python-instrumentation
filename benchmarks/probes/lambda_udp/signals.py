# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Application Signals RED metrics for the benchmarked function.

Metric names come from the distro itself -- Error, Fault and Latency, in the ApplicationSignals
namespace (aws_span_metrics_processor_builder.py). There is no separate call-count metric, so request
rate is the SampleCount of Latency.

Dimensions are discovered with ListMetrics rather than assumed: the exact dimension set Application
Signals emits for a Lambda function is not something this code should guess, and a wrong guess returns
an empty result that looks like "no traffic" rather than "wrong query".

Worth testing rather than assuming: in lite mode these metrics derive from spans that travel over the
very UDP path this PR changes, so a shortfall in Latency SampleCount against the known invocation count
may itself be a dropped-datagram signal. datapoint_shortfall_pct is recorded for exactly that.
"""
import time
from typing import Dict, List

import boto3
from botocore.config import Config

from .stack import REGION

NAMESPACE = "ApplicationSignals"
_METRICS = ("Latency", "Error", "Fault")
# Application Signals aggregates on 1-minute boundaries and ingests with a lag; querying immediately
# returns partial data that reads as a regression
DEFAULT_SETTLE_S = 180
_PERIOD_S = 60


def collect(
    function_name: str,
    started_at: float,
    invocations: int,
    settle_s: int = DEFAULT_SETTLE_S,
) -> Dict[str, float]:
    """Wait for ingestion, then read RED for this function. Returns {} if nothing was published."""
    cloudwatch = boto3.client("cloudwatch", config=Config(region_name=REGION))
    time.sleep(settle_s)

    discovered = _discover(cloudwatch, function_name)
    if not discovered:
        return {"red_metrics_found": 0.0}

    queries = []
    # Application Signals publishes the same metric at several dimension granularities (Service,
    # Service+Operation, ...). Summing across them multiplies the request count, so each query is
    # tagged with its dimension set and the sets are reduced with max(), not sum().
    id_map: Dict[str, str] = {}
    for index, (metric_name, dimensions) in enumerate(discovered):
        dims_key = "|".join(f"{d['Name']}={d['Value']}" for d in sorted(dimensions, key=lambda d: d["Name"]))
        id_map[f"m{index}"] = dims_key
        id_map[f"m{index}_count"] = dims_key
        id_map[f"m{index}_p99"] = dims_key
        stat = "Sum" if metric_name in ("Error", "Fault") else "Average"
        queries.append(
            {
                "Id": f"m{index}",
                "MetricStat": {
                    "Metric": {"Namespace": NAMESPACE, "MetricName": metric_name, "Dimensions": dimensions},
                    "Period": _PERIOD_S,
                    "Stat": stat,
                },
                "ReturnData": True,
            }
        )
        if metric_name == "Latency":
            for suffix, stat in (("count", "SampleCount"), ("p99", "p99")):
                queries.append(
                    {
                        "Id": f"m{index}_{suffix}",
                        "MetricStat": {
                            "Metric": {
                                "Namespace": NAMESPACE,
                                "MetricName": metric_name,
                                "Dimensions": dimensions,
                            },
                            "Period": _PERIOD_S,
                            "Stat": stat,
                        },
                        "ReturnData": True,
                    }
                )

    response = cloudwatch.get_metric_data(
        MetricDataQueries=queries[:500],  # GetMetricData caps queries per call
        StartTime=_floor_minute(started_at - _PERIOD_S),
        EndTime=_floor_minute(time.time() + _PERIOD_S),
    )
    return _summarise(response.get("MetricDataResults", []), invocations, id_map)


def _floor_minute(epoch: float):
    from datetime import datetime, timezone

    return datetime.fromtimestamp(epoch - (epoch % _PERIOD_S), tz=timezone.utc)


def _discover(cloudwatch, function_name: str) -> List[tuple]:
    found = []
    for metric_name in _METRICS:
        paginator = cloudwatch.get_paginator("list_metrics")
        pages = paginator.paginate(
            Namespace=NAMESPACE,
            MetricName=metric_name,
            Dimensions=[{"Name": "Service", "Value": function_name}],
        )
        for page in pages:
            for metric in page.get("Metrics", []):
                found.append((metric_name, metric["Dimensions"]))
    return found


def _summarise(results: List[dict], invocations: int, id_map: Dict[str, str]) -> Dict[str, float]:
    # Keyed by dimension set so overlapping granularities are reduced, not accumulated
    counts_by_dims: Dict[str, float] = {}
    errors_by_dims: Dict[str, float] = {}
    faults_by_dims: Dict[str, float] = {}
    latency_values, latency_p99 = [], []

    for result in results:
        values = result.get("Values") or []
        if not values:
            continue
        identifier = result["Id"]
        dims = id_map.get(identifier, identifier)
        label = result.get("Label", "")
        if identifier.endswith("_count"):
            counts_by_dims[dims] = counts_by_dims.get(dims, 0.0) + sum(values)
        elif identifier.endswith("_p99"):
            latency_p99.append(max(values))
        elif "Fault" in label:
            faults_by_dims[dims] = faults_by_dims.get(dims, 0.0) + sum(values)
        elif "Error" in label:
            errors_by_dims[dims] = errors_by_dims.get(dims, 0.0) + sum(values)
        else:
            latency_values.append(sum(values) / len(values))

    total_samples = max(counts_by_dims.values()) if counts_by_dims else 0.0
    metrics = {
        "red_metrics_found": 1.0,
        "red_dimension_sets": float(len(counts_by_dims)),
        "red_latency_avg_ms": (sum(latency_values) / len(latency_values)) if latency_values else 0.0,
        "red_latency_p99_ms": max(latency_p99) if latency_p99 else 0.0,
        "red_request_count": float(total_samples),
        "red_error_sum": max(errors_by_dims.values()) if errors_by_dims else 0.0,
        "red_fault_sum": max(faults_by_dims.values()) if faults_by_dims else 0.0,
    }
    if invocations:
        # Shortfall against a known invocation count: in lite mode, spans reach Application Signals over
        # the UDP path under test, so a gap here may be dropped datagrams rather than missing traffic
        metrics["red_datapoint_shortfall_pct"] = max(0.0, 100.0 * (1 - total_samples / invocations))
    return metrics
