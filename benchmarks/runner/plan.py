# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""The benchmark plan: which probe to run, what scenarios, and which metrics matter.

The agent writes the plan; the runner only executes it. Both the metric specs and the scenario params
live in the plan rather than in code, because what matters is a property of the diff and not of the
harness -- a UDP change cares about datagram delivery, a memory fix cares about heap. Scenario params are
free-form here and validated by the probe that consumes them. See benchmarks/GAPS.md sections A1 and A3.
"""
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

import yaml

HIGHER_IS_BETTER = "higher_is_better"
LOWER_IS_BETTER = "lower_is_better"
INFORMATIONAL = "informational"
_DIRECTIONS = (HIGHER_IS_BETTER, LOWER_IS_BETTER, INFORMATIONAL)


@dataclass
class MetricSpec:
    """One measurement to report, and how to read it."""

    key: str
    label: str
    unit: str = ""
    direction: str = INFORMATIONAL

    def __post_init__(self) -> None:
        if self.direction not in _DIRECTIONS:
            raise ValueError(f"metric {self.key}: direction must be one of {_DIRECTIONS}, got {self.direction!r}")


@dataclass
class Scenario:
    """One benchmark case. params are probe-specific and validated by the probe, not here."""

    id: str
    intent: str = ""
    source: str = "dynamic"  # base | dynamic
    targets: List[str] = field(default_factory=list)
    repeats: int = 10
    params: Dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.repeats < 1:
            raise ValueError(f"scenario {self.id}: repeats must be >= 1")
        if self.source not in ("base", "dynamic"):
            raise ValueError(f"scenario {self.id}: source must be base or dynamic, got {self.source!r}")


@dataclass
class Plan:
    probe: str
    pr: int
    baseline_ref: str
    head_ref: str
    scenarios: List[Scenario]
    metrics: List[MetricSpec]
    notes: str = ""

    @property
    def scenario_ids(self) -> List[str]:
        return [s.id for s in self.scenarios]


def _require(data: Dict[str, Any], key: str, where: str) -> Any:
    if key not in data:
        raise ValueError(f"{where}: missing required key {key!r}")
    return data[key]


def load(path: Path) -> Plan:
    data = yaml.safe_load(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path}: expected a mapping at the top level")

    scenarios = [Scenario(**s) for s in _require(data, "scenarios", str(path))]
    if not scenarios:
        raise ValueError(f"{path}: plan has no scenarios")
    ids = [s.id for s in scenarios]
    duplicates = sorted({i for i in ids if ids.count(i) > 1})
    if duplicates:
        raise ValueError(f"{path}: duplicate scenario ids: {duplicates}")

    metrics = [MetricSpec(**m) for m in _require(data, "metrics", str(path))]
    if not metrics:
        raise ValueError(f"{path}: plan has no metrics")

    return Plan(
        probe=str(_require(data, "probe", str(path))),
        pr=int(_require(data, "pr", str(path))),
        baseline_ref=str(_require(data, "baseline_ref", str(path))),
        head_ref=str(_require(data, "head_ref", str(path))),
        scenarios=scenarios,
        metrics=metrics,
        notes=str(data.get("notes", "")),
    )


def merge_base(plan_data: Dict[str, Any], base_path: Path) -> Dict[str, Any]:
    """Prepend user-written base scenarios to a generated plan. Base scenarios win on id collision."""
    if not base_path.exists():
        return plan_data
    base = yaml.safe_load(base_path.read_text()) or {}
    base_scenarios = base.get("scenarios") or []
    for scenario in base_scenarios:
        scenario.setdefault("source", "base")
    base_ids = {s["id"] for s in base_scenarios}
    kept = [s for s in plan_data.get("scenarios", []) if s["id"] not in base_ids]
    plan_data["scenarios"] = base_scenarios + kept
    return plan_data
