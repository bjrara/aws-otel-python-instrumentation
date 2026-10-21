# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Span factories for both export paths, with the shape a scenario asks for.

Spans are produced through each mode's own tracer rather than hand-built, so the benchmark exercises the
same objects the exporters see in Lambda. The lite and full SDKs have incompatible span types, which is
why there are two factories rather than one.
"""
from typing import Any, List

# A long single-byte run would truncate at a tidy boundary; this exercises UTF-8 boundary handling.
_UNICODE_FILLER = "éü中文\U0001f600"


def _filler(size_bytes: int, unicode: bool) -> str:
    if not unicode:
        return "x" * size_bytes
    out = []
    total = 0
    while total < size_bytes:
        out.append(_UNICODE_FILLER)
        total += len(_UNICODE_FILLER.encode("utf-8"))
    return "".join(out)


def _attributes(attr_count: int, attr_bytes: int, unicode: bool, url_like: bool) -> dict:
    value = _filler(attr_bytes, unicode)
    attrs = {f"bench.attr.{i}": value for i in range(attr_count)}
    if url_like:
        # Risk C-1 from the review: URL and aws.* keys are truncated like any other string
        attrs["http.url"] = "https://example.com/" + value
        attrs["aws.remote.operation"] = value
    return attrs


def lite_spans(count: int, attr_count: int, attr_bytes: int, unicode: bool, url_like: bool) -> List[Any]:
    """Spans from the lite SDK's own tracer, as produced in OTEL_AWS_LAMBDA_FAST_START mode."""
    from amazon.opentelemetry.distro import opentelemetry_lite_sdk as lite

    captured: List[Any] = []

    class _Capture(lite.SpanProcessor):
        def on_end(self, span):
            captured.append(span)

    provider = lite.TracerProvider()
    provider.add_span_processor(_Capture())
    tracer = provider.get_tracer("benchmark")

    attrs = _attributes(attr_count, attr_bytes, unicode, url_like)
    for i in range(count):
        with tracer.start_as_current_span(f"bench-span-{i}", attributes=attrs):
            pass
    return captured


def full_spans(count: int, attr_count: int, attr_bytes: int, unicode: bool, url_like: bool) -> List[Any]:
    """ReadableSpans from the standard SDK, as produced by the full distro on Lambda."""
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import SpanProcessor

    captured: List[Any] = []

    class _Capture(SpanProcessor):
        def on_end(self, span):
            captured.append(span)

    provider = TracerProvider()
    provider.add_span_processor(_Capture())
    tracer = provider.get_tracer("benchmark")

    attrs = _attributes(attr_count, attr_bytes, unicode, url_like)
    for i in range(count):
        with tracer.start_as_current_span(f"bench-span-{i}", attributes=attrs):
            pass
    return captured
