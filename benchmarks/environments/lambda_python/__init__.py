# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deployed-Lambda environment: a real function, a real X-Ray daemon, real Application Signals.

This is the long-lived half a customer provisions once and reuses across many PRs. It knows how to
build a layer from a worktree, deploy a function, drive it, collect what Lambda and CloudWatch report,
and remove everything it made. It knows nothing about what any particular change does.

The function is driven by lambda:Invoke -- no API Gateway, nothing reachable from outside the account.
Resources are named udpbench-<run_id>-<ref> and every delete asserts that prefix first, because the
account this runs in holds unrelated stacks.
"""
import time
from pathlib import Path
from typing import Dict, Optional

from benchmarks.runner import capabilities
from benchmarks.runner.environment import RunContext

from . import invoke, layer, signals, stack

name = "lambda_python"

# One deployed stack serves every scenario for a ref; the workload comes from the invocation event
_stacks: Dict[str, object] = {}


def capabilities_offered() -> Dict[str, object]:
    return {
        # Lambda allows the full IPv4 datagram; this is the environment the distro is written for
        capabilities.UDP_DATAGRAM_BYTES: 65_507,
        capabilities.REAL_TELEMETRY_BACKEND: True,
        # Lambda reports max memory per invocation, but not process CPU or RSS over time
        capabilities.PROCESS_RESOURCE_SAMPLING: False,
        capabilities.CONFIGURABLE_RECEIVE_BUFFER: False,
    }


def setup(context: RunContext) -> None:
    zip_path = layer.build(
        worktree=context.worktree,
        out_dir=Path(context.out_dir) / "layers",
        tag=context.ref_tag,
    )
    _stacks[context.ref_tag] = stack.create(
        run_id=context.run_id,
        tag=context.ref_tag,
        layer_zip=zip_path,
        repo_root=context.repo_root,
        lite_mode=True,
    )


def teardown(context: RunContext) -> Optional[list]:
    deployed = _stacks.pop(context.ref_tag, None)
    return stack.destroy(deployed) if deployed else None


def execute(instrument, scenario, context: RunContext) -> Dict:
    offered = capabilities_offered()
    capabilities.check(getattr(instrument, "REQUIRES", {}), offered)

    deployed = _stacks.get(context.ref_tag)
    if deployed is None:
        raise RuntimeError("setup() did not deploy a stack for this ref")

    event = instrument.workload(scenario)
    params = scenario.params
    concurrency = int(params.get("concurrency", 5))
    invocations = max(scenario.repeats, concurrency)

    invoke.warm(deployed, event)  # keep init cost out of the measured invocations
    started_at = time.time()
    outcome = invoke.drive(deployed, event, invocations=invocations, concurrency=concurrency)

    raw = {
        "metrics": invoke.metrics(outcome),
        "messages": outcome.messages,
        "invocations": invocations,
        "function_name": deployed.function_name,
        "_capabilities": offered,
    }
    raw["metrics"].update(
        signals.collect(
            deployed.function_name,
            started_at=started_at,
            invocations=invocations,
            settle_s=int(params.get("signals_settle_s", signals.DEFAULT_SETTLE_S)),
        )
    )
    return raw


class _Environment:
    name = name
    capabilities = staticmethod(capabilities_offered)
    setup = staticmethod(setup)
    teardown = staticmethod(teardown)
    execute = staticmethod(execute)


ENVIRONMENT = _Environment()
