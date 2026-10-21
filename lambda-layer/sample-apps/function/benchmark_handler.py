# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Handler that emits a configurable span volume, for benchmarking the UDP export path on Lambda.

Separate from lambda_function.py on purpose: that handler models a realistic small app and must stay
that way, while this one exists to push a single invocation's spans past the datagram limit. The shape
comes from the invocation event so one deployed function can serve every scenario.

Event fields (all optional):
    spans       int   spans to create (default 10)
    attr_count  int   string attributes per span (default 4)
    attr_bytes  int   bytes per attribute value (default 256)
    unicode     bool  fill with multi-byte characters to exercise UTF-8 boundary truncation
    url_like    bool  add http.url and aws.remote.operation, the keys review risk C-1 flags
"""
import json

from opentelemetry import trace

# A long single-byte run would truncate at a tidy boundary; this does not.
_UNICODE_FILLER = "éü中文\U0001f600"

_tracer = trace.get_tracer("benchmark")


def _filler(size_bytes, use_unicode):
    if not use_unicode:
        return "x" * size_bytes
    out = []
    total = 0
    while total < size_bytes:
        out.append(_UNICODE_FILLER)
        total += len(_UNICODE_FILLER.encode("utf-8"))
    return "".join(out)


def lambda_handler(event, context):
    event = event or {}
    span_count = int(event.get("spans", 10))
    attr_count = int(event.get("attr_count", 4))
    attr_bytes = int(event.get("attr_bytes", 256))
    use_unicode = bool(event.get("unicode", False))
    url_like = bool(event.get("url_like", False))

    value = _filler(attr_bytes, use_unicode)
    attributes = {f"bench.attr.{i}": value for i in range(attr_count)}
    if url_like:
        attributes["http.url"] = "https://example.com/" + value
        attributes["aws.remote.operation"] = value

    for i in range(span_count):
        with _tracer.start_as_current_span(f"bench-span-{i}", attributes=attributes):
            pass

    # Reported back so the runner can confirm the function did what the scenario asked, rather than
    # inferring it from telemetry that may itself have been dropped.
    return {
        "statusCode": 200,
        "body": json.dumps(
            {
                "spans": span_count,
                "attr_count": attr_count,
                "attr_bytes": attr_bytes,
                "unicode": use_unicode,
                "url_like": url_like,
                "approx_attr_payload_bytes": span_count * attr_count * len(value.encode("utf-8")),
            }
        ),
    }
