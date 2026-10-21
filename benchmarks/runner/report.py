# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Renders a comparison as the markdown that gets posted to the PR.

Caveats are part of the report, not a footnote: a delivery rate measured against a pinned receive buffer
does not predict the real X-Ray daemon, and saying so is the difference between a useful number and a
misleading one.
"""
from typing import List

from .compare import (
    BOTH_FAILED,
    COMPARABLE,
    IMPROVEMENT,
    MISSING,
    NEWLY_BROKEN,
    NEWLY_WORKING,
    REGRESSION,
    Comparison,
    ScenarioComparison,
)

_VERDICT_MARK = {IMPROVEMENT: "improved", REGRESSION: "regressed", "neutral": "~"}

_CATEGORY_HEADING = {
    NEWLY_BROKEN: "Newly broken",
    NEWLY_WORKING: "Newly working (baseline could not run)",
    COMPARABLE: "Comparable",
    BOTH_FAILED: "Failed on both refs",
    MISSING: "Missing results",
}

# Order matters: the reader should see breakage before improvements
_CATEGORY_ORDER = [NEWLY_BROKEN, COMPARABLE, NEWLY_WORKING, BOTH_FAILED, MISSING]


def _fmt(value, unit: str) -> str:
    if value is None:
        return "n/a"
    if abs(value) >= 1_000_000:
        return f"{value / 1_000_000:.2f}M{(' ' + unit) if unit else ''}"
    if abs(value) >= 1000:
        return f"{value:,.0f}{(' ' + unit) if unit else ''}"
    if float(value).is_integer():
        return f"{value:.0f}{(' ' + unit) if unit else ''}"
    return f"{value:.3f}{(' ' + unit) if unit else ''}"


def _scenario_block(scenario: ScenarioComparison) -> List[str]:
    label = f"`{scenario.scenario_id}`"
    if scenario.labels:
        label += " (" + ", ".join(f"{k}={v}" for k, v in sorted(scenario.labels.items())) + ")"
    lines = [f"#### {label}", ""]
    if scenario.intent:
        lines += [f"_{scenario.intent}_", ""]
    lines += [f"Source: {scenario.source}", ""]

    if scenario.category in (BOTH_FAILED, MISSING, NEWLY_BROKEN, NEWLY_WORKING):
        if scenario.baseline_error:
            lines.append(f"- baseline: `{scenario.baseline_error}`")
        if scenario.head_error:
            lines.append(f"- head: `{scenario.head_error}`")
        lines.append("")

    if not scenario.deltas:
        return lines

    lines += ["| Metric | Baseline | Head | Delta | |", "| --- | --- | --- | --- | --- |"]
    for delta in scenario.deltas:
        pct = f" ({delta.delta_pct:+.1f}%)" if delta.delta_pct is not None else ""
        change = f"{_fmt(delta.delta, '')}{pct}" if delta.delta is not None else "n/a"
        lines.append(
            f"| {delta.label} | {_fmt(delta.baseline, delta.unit)} | {_fmt(delta.head, delta.unit)} "
            f"| {change} | {_VERDICT_MARK.get(delta.verdict, '')} |"
        )
    lines.append("")
    return lines


def render(comparison: Comparison, caveats: List[str], notes: str = "") -> str:
    counts = {c: len(comparison.by_category(c)) for c in _CATEGORY_ORDER}
    regressions = sum(
        1
        for s in comparison.by_category(COMPARABLE)
        for d in s.deltas
        if d.verdict == REGRESSION
    )

    lines = [
        f"## Benchmark: PR #{comparison.pr}",
        "",
        f"Probe `{comparison.probe}` · baseline `{comparison.baseline_ref}` → head `{comparison.head_ref}`",
        "",
        "| Outcome | Scenarios |",
        "| --- | --- |",
    ]
    for category in _CATEGORY_ORDER:
        if counts[category]:
            lines.append(f"| {_CATEGORY_HEADING[category]} | {counts[category]} |")
    lines += [f"| Metric regressions (comparable scenarios) | {regressions} |", ""]

    if notes:
        lines += ["### Plan notes", "", notes, ""]

    for category in _CATEGORY_ORDER:
        scenarios = comparison.by_category(category)
        if not scenarios:
            continue
        lines += [f"### {_CATEGORY_HEADING[category]}", ""]
        for scenario in scenarios:
            lines += _scenario_block(scenario)

    if caveats:
        lines += ["### How to read this", ""]
        lines += [f"- {c}" for c in caveats]
        lines.append("")

    return "\n".join(lines)
