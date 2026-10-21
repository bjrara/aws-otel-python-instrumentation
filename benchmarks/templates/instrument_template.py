# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Scaffold for a generated instrument. Copy into the run directory and fill in.

An instrument is the throwaway half of a benchmark: it knows what one particular change does. It is
generated per run, lives in the run's sandbox, and is never committed -- so write it for this diff and
this diff only, and do not try to make it reusable.

It is also assumed wrong until proven otherwise. Run

    python -m benchmarks.runner.cli conformance --plan <plan> --distro-src <src>

which measures one commit twice and requires every verdict to come back neutral. If it reports signal
where there is none, the instrument is measuring noise or measuring the harness.

Rules that come from instruments having lied before:

- Count what actually happened, not what should have. Wrap the call that does the work rather than
  inferring counts from sizes, so an attempt that raises is still counted.
- Declare every environment property the measurement depends on in REQUIRES. If the environment
  cannot represent production, the run must fail loudly instead of returning a plausible number.
- Never derive a rate from a denominator the environment caps. Record the cap as a metric so a
  reader can tell a real result from an artefact.
"""
from typing import Dict

# Capability keys this measurement depends on; the environment refuses the run if it cannot meet them.
# Keys live in benchmarks/runner/capabilities.py.
REQUIRES: Dict[str, object] = {}

# Environment names this instrument knows how to drive, from `cli environments`.
SUPPORTS = ("local_process",)


def workload(scenario) -> Dict:
    """The input this scenario presents to the environment.

    For a deployed environment this is the invocation event. For an in-process environment it is
    whatever `drive` needs to build the work. Read knobs from `scenario.params`.
    """
    params = scenario.params
    return {"example_knob": params.get("example_knob", 1)}


def drive(scenario, services) -> Dict:
    """In-process environments only: exercise the code under test and return raw counters.

    `services` offers what the environment provides -- a telemetry sink, resource sampling. Do not
    reach past it. Return raw observations; interpret them in `observe`, so the two can be reviewed
    separately.
    """
    raise NotImplementedError("implement drive() for in-process environments, or drop it")


def observe(raw: Dict, scenario) -> Dict[str, float]:
    """Turn the environment's raw output into the metrics the plan names.

    Every key a plan's `metrics` list references must appear here (or in the environment's own
    metrics). Values must be floats. Return only what the measurement supports -- a metric that
    cannot be measured honestly is better absent than guessed.
    """
    raise NotImplementedError("implement observe()")
