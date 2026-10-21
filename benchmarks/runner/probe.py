# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The seam between the generic runner and the code that knows what a change does.

A probe exercises one change path and reports numbers for it. The runner never imports a probe directly:
it resolves the name from the plan, so adding a probe for a different PR -- a JVM heap probe, an EC2
request probe -- means adding a package under benchmarks/probes/ and nothing else.

Probes that need to stand something up once per ref (deploy a function, start a service) do it in
setup() and tear it down in teardown(), which the runner calls around the scenario loop. Probes that
need neither -- anything purely in-process -- can omit both.
"""
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Protocol, runtime_checkable

_PROBE_PACKAGE = "benchmarks.probes"


@dataclass
class RunContext:
    """Which ref is being measured and where its code lives. Identical across a ref's scenarios."""

    run_id: str
    ref_tag: str  # short label for this ref, e.g. "baseline" or "head"
    ref: str  # the commit actually measured, for the record
    distro_src: Path
    out_dir: Path

    @property
    def worktree(self) -> Path:
        """Checkout root, derived from the distro source path."""
        return self.distro_src.parent.parent

    @property
    def repo_root(self) -> Path:
        """The checkout the runner itself lives in, not the ref's worktree."""
        return Path(__file__).resolve().parents[2]


@dataclass
class ScenarioResult:
    """Outcome of one scenario. A probe that could not run reports ok=False rather than raising."""

    scenario_id: str
    ok: bool
    metrics: Dict[str, float] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def reported_failure(self) -> bool:
        """The probe ran but the code under test failed -- e.g. a baseline that cannot send at all."""
        return self.ok and (self.metrics.get("export_failures", 0) > 0 or self.metrics.get("invoke_errors", 0) > 0)


@runtime_checkable
class Probe(Protocol):
    """What a probe module must expose."""

    name: str

    def run(self, scenario, context: RunContext) -> ScenarioResult:
        """Execute one scenario and return its measurements."""

    def describe_params(self) -> Dict[str, str]:
        """Scenario params this probe understands, for plan validation and agent guidance."""


def setup(probe: Probe, context: RunContext) -> None:
    """Call a probe's setup if it has one."""
    hook = getattr(probe, "setup", None)
    if hook:
        hook(context)


def teardown(probe: Probe, context: RunContext) -> Optional[list]:
    """Call a probe's teardown if it has one. Returns whatever it reports having removed."""
    hook = getattr(probe, "teardown", None)
    return hook(context) if hook else None


def load(name: str) -> Probe:
    """Resolve a probe by plan name. Raises ValueError with the available names on a miss."""
    try:
        module = importlib.import_module(f"{_PROBE_PACKAGE}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith(f"{_PROBE_PACKAGE}.{name}"):
            raise  # a real missing dependency inside the probe, not a bad probe name
        raise ValueError(f"unknown probe {name!r}; available: {', '.join(available())}") from exc
    probe = getattr(module, "PROBE", None)
    if probe is None:
        raise ValueError(f"probe {name!r} does not define a module-level PROBE")
    return probe


def available() -> list:
    probes_dir = Path(__file__).resolve().parent.parent / "probes"
    if not probes_dir.is_dir():
        return []
    return sorted(p.name for p in probes_dir.iterdir() if (p / "__init__.py").exists())
