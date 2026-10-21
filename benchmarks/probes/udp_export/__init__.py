# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Probe for the Lambda UDP span export path.

Scoped to this change path on purpose: OTLPUdpSpanExporter and lite mode exist only for Lambda, so a
probe that exercises them is Lambda-specific. Everything reusable lives in benchmarks/runner.
"""
from typing import Dict

from . import bench

name = "udp_export"


def run(scenario, context=None):
    # context is unused: this probe runs in-process against whatever --distro-src put on sys.path,
    # so it needs no per-ref setup and stands nothing up to tear down.
    return bench.run(scenario)


def describe_params() -> Dict[str, str]:
    return {
        "mode": "lite | full -- lite is OTEL_AWS_LAMBDA_FAST_START, full is the standard distro",
        "spans": "spans per export batch (int)",
        "attr_count": "string attributes per span (int)",
        "attr_bytes": "bytes per attribute value (int); spans * attr_count * attr_bytes drives payload size",
        "unicode": "fill attributes with multi-byte characters to exercise UTF-8 boundary truncation (bool)",
        "url_like": "add http.url and aws.remote.operation attributes, the keys review risk C-1 flags (bool)",
        "rcvbuf": "receiver SO_RCVBUF in bytes (int); pins the loss threshold so runs are comparable",
    }


class _Probe:
    name = name
    run = staticmethod(run)
    describe_params = staticmethod(describe_params)


PROBE = _Probe()
