# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The persisted half: where a benchmark runs, and what it can represent.

An environment is change-agnostic and provisioned for the long term -- a customer stands one up once
and reuses it across many PRs. It owns deployment and teardown, the services an instrument needs, and
the capability assertions that stop a run from reporting numbers the environment cannot support.

Environments live in benchmarks/environments/ and are resolved by name from the plan, so adding one
never means editing the runner.
"""
import importlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Optional, Protocol

_ENVIRONMENT_PACKAGE = "benchmarks.environments"


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
    """Outcome of one scenario. A failure to run is reported, not raised."""

    scenario_id: str
    ok: bool
    metrics: Dict[str, float] = field(default_factory=dict)
    labels: Dict[str, str] = field(default_factory=dict)
    error: str = ""

    @property
    def reported_failure(self) -> bool:
        """The run succeeded but the code under test failed -- e.g. a baseline that cannot send."""
        return self.ok and any(
            self.metrics.get(key, 0) > 0 for key in ("export_failures", "invoke_errors", "workload_errors")
        )


class Environment(Protocol):
    name: str

    def capabilities(self) -> Dict[str, object]:
        """What this environment can represent, as capability keys."""

    def execute(self, instrument, scenario, context: RunContext) -> Dict:
        """Run one scenario's workload and return raw output for the instrument to interpret."""


def setup(environment, context: RunContext) -> None:
    hook = getattr(environment, "setup", None)
    if hook:
        hook(context)


def teardown(environment, context: RunContext) -> Optional[list]:
    hook = getattr(environment, "teardown", None)
    return hook(context) if hook else None


def load(name: str):
    try:
        module = importlib.import_module(f"{_ENVIRONMENT_PACKAGE}.{name}")
    except ModuleNotFoundError as exc:
        if exc.name and not exc.name.startswith(f"{_ENVIRONMENT_PACKAGE}.{name}"):
            raise  # a real missing dependency inside the environment, not a bad name
        raise ValueError(f"unknown environment {name!r}; available: {', '.join(available())}") from exc
    environment = getattr(module, "ENVIRONMENT", None)
    if environment is None:
        raise ValueError(f"environment {name!r} does not define a module-level ENVIRONMENT")
    return environment


def available() -> list:
    directory = Path(__file__).resolve().parent.parent / "environments"
    if not directory.is_dir():
        return []
    return sorted(p.name for p in directory.iterdir() if (p / "__init__.py").exists())
