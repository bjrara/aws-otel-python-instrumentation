# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Probe for the UDP span export path on a real Lambda function.

Complements udp_export rather than replacing it. udp_export measures the exporter in-process against a
stand-in receiver, which is fast and deterministic but cannot tell you what the real X-Ray daemon does
with a burst. This probe deploys the change and drives it, so delivery is judged by what actually
arrives in Application Signals.

The function is invoked directly -- no API Gateway, nothing reachable from outside the account.
Throughput and p99 are measured from concurrent lambda:Invoke.

One stack per ref: setup() builds the layer and deploys, teardown() removes everything it created.
"""
from pathlib import Path
from typing import Dict, Optional

from benchmarks.runner.probe import RunContext, ScenarioResult

from . import invoke, layer, signals, stack

name = "lambda_udp"

# One deployed stack serves every scenario for a ref; the span shape comes from the invocation event
_state: Dict[str, object] = {}


def setup(context: RunContext) -> None:
    """Build this ref's layer and deploy a function from it."""
    zip_path = layer.build(
        worktree=context.worktree,
        out_dir=Path(context.out_dir) / "layers",
        tag=context.ref_tag,
    )
    deployed = stack.create(
        run_id=context.run_id,
        tag=context.ref_tag,
        layer_zip=zip_path,
        repo_root=context.repo_root,
        lite_mode=True,
    )
    _state[context.ref_tag] = deployed


def teardown(context: RunContext) -> Optional[list]:
    deployed = _state.pop(context.ref_tag, None)
    if deployed is None:
        return None
    return stack.destroy(deployed)


def run(scenario, context: RunContext) -> ScenarioResult:
    deployed = _state.get(context.ref_tag)
    if deployed is None:
        return ScenarioResult(
            scenario_id=scenario.id, ok=False, error="setup() did not deploy a stack for this ref"
        )

    params = scenario.params
    event = {
        "spans": int(params.get("spans", 10)),
        "attr_count": int(params.get("attr_count", 4)),
        "attr_bytes": int(params.get("attr_bytes", 256)),
        "unicode": bool(params.get("unicode", False)),
        "url_like": bool(params.get("url_like", False)),
    }
    concurrency = int(params.get("concurrency", 5))
    invocations = max(scenario.repeats, concurrency)

    try:
        import time

        invoke.warm(deployed, event)  # keep init cost out of the measured invocations
        started_at = time.time()
        outcome = invoke.drive(deployed, event, invocations=invocations, concurrency=concurrency)
        metrics = invoke.metrics(outcome)
        metrics.update(
            signals.collect(
                deployed.function_name,
                started_at=started_at,
                invocations=invocations,
                settle_s=int(params.get("signals_settle_s", signals.DEFAULT_SETTLE_S)),
            )
        )
    except Exception as exc:  # pylint: disable=broad-except
        return ScenarioResult(scenario_id=scenario.id, ok=False, error=f"{type(exc).__name__}: {exc}")

    return ScenarioResult(
        scenario_id=scenario.id,
        ok=True,
        metrics=metrics,
        labels={"mode": "lite", "function": deployed.function_name},
    )


def describe_params() -> Dict[str, str]:
    return {
        "spans": "spans the handler creates per invocation (int)",
        "attr_count": "string attributes per span (int)",
        "attr_bytes": "bytes per attribute value (int); spans * attr_count * attr_bytes drives payload size",
        "unicode": "fill attributes with multi-byte characters (bool)",
        "url_like": "add http.url and aws.remote.operation attributes (bool)",
        "concurrency": "parallel invocations, for throughput and p99 (int, default 5)",
        "signals_settle_s": f"seconds to wait for Application Signals ingestion (int, default {signals.DEFAULT_SETTLE_S})",
    }


class _Probe:
    name = name
    setup = staticmethod(setup)
    teardown = staticmethod(teardown)
    run = staticmethod(run)
    describe_params = staticmethod(describe_params)


PROBE = _Probe()
