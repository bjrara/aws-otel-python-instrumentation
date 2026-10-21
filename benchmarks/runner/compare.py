# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Baseline vs head.

A percentage delta against a baseline that could not run at all is meaningless, so scenarios are first
sorted into outcome categories and only comparable ones get deltas. This matters for real changes: on
main, oversized payloads fail with EMSGSIZE and deliver nothing. See benchmarks/GAPS.md section A11.
"""
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from .plan import HIGHER_IS_BETTER, LOWER_IS_BETTER, MetricSpec, Plan
from .probe import ScenarioResult

COMPARABLE = "comparable"
NEWLY_WORKING = "newly-working"
NEWLY_BROKEN = "newly-broken"
BOTH_FAILED = "both-failed"
MISSING = "missing"

IMPROVEMENT = "improvement"
REGRESSION = "regression"
NEUTRAL = "neutral"

# Relative change below this is treated as noise rather than signal
NOISE_THRESHOLD_PCT = 5.0


@dataclass
class MetricDelta:
    key: str
    label: str
    unit: str
    direction: str
    baseline: Optional[float]
    head: Optional[float]
    delta: Optional[float] = None
    delta_pct: Optional[float] = None
    verdict: str = NEUTRAL


@dataclass
class ScenarioComparison:
    scenario_id: str
    category: str
    intent: str = ""
    source: str = "dynamic"
    labels: Dict[str, str] = field(default_factory=dict)
    baseline_error: str = ""
    head_error: str = ""
    deltas: List[MetricDelta] = field(default_factory=list)


@dataclass
class Comparison:
    pr: int
    probe: str
    baseline_ref: str
    head_ref: str
    scenarios: List[ScenarioComparison]

    def by_category(self, category: str) -> List[ScenarioComparison]:
        return [s for s in self.scenarios if s.category == category]


def _failed(result: Optional[ScenarioResult]) -> bool:
    if result is None:
        return True
    return (not result.ok) or result.reported_failure


def _verdict(spec: MetricSpec, baseline: float, head: float) -> str:
    if spec.direction not in (HIGHER_IS_BETTER, LOWER_IS_BETTER):
        return NEUTRAL
    if baseline == head:
        return NEUTRAL
    denominator = abs(baseline) if baseline else abs(head)
    if denominator and abs(head - baseline) / denominator * 100 < NOISE_THRESHOLD_PCT:
        return NEUTRAL
    improved = head > baseline if spec.direction == HIGHER_IS_BETTER else head < baseline
    return IMPROVEMENT if improved else REGRESSION


def _deltas(specs: List[MetricSpec], baseline: ScenarioResult, head: ScenarioResult) -> List[MetricDelta]:
    out = []
    for spec in specs:
        before = baseline.metrics.get(spec.key)
        after = head.metrics.get(spec.key)
        delta = MetricDelta(
            key=spec.key,
            label=spec.label,
            unit=spec.unit,
            direction=spec.direction,
            baseline=before,
            head=after,
        )
        if before is not None and after is not None:
            delta.delta = after - before
            delta.delta_pct = ((after - before) / abs(before) * 100) if before else None
            delta.verdict = _verdict(spec, before, after)
        out.append(delta)
    return out


def compare(
    plan: Plan,
    baseline_results: Dict[str, ScenarioResult],
    head_results: Dict[str, ScenarioResult],
) -> Comparison:
    comparisons = []
    for scenario in plan.scenarios:
        baseline = baseline_results.get(scenario.id)
        head = head_results.get(scenario.id)

        if baseline is None or head is None:
            category = MISSING
        elif _failed(baseline) and _failed(head):
            category = BOTH_FAILED
        elif _failed(baseline):
            category = NEWLY_WORKING
        elif _failed(head):
            category = NEWLY_BROKEN
        else:
            category = COMPARABLE

        comparison = ScenarioComparison(
            scenario_id=scenario.id,
            category=category,
            intent=scenario.intent,
            source=scenario.source,
            labels=(head.labels if head else {}) or (baseline.labels if baseline else {}),
            baseline_error=baseline.error if baseline else "result missing",
            head_error=head.error if head else "result missing",
        )
        if baseline is not None and head is not None:
            comparison.deltas = _deltas(plan.metrics, baseline, head)
        comparisons.append(comparison)

    return Comparison(
        pr=plan.pr,
        probe=plan.probe,
        baseline_ref=plan.baseline_ref,
        head_ref=plan.head_ref,
        scenarios=comparisons,
    )
