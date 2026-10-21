# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Tests for outcome categorisation and delta arithmetic.

This is the logic the whole report rests on: if a scenario is mis-categorised, the report either claims
an improvement over a crash or hides a regression. Run with:

    python -m unittest discover -s benchmarks/tests
"""
import unittest

from benchmarks.runner.compare import (
    BOTH_FAILED,
    COMPARABLE,
    IMPROVEMENT,
    MISSING,
    NEUTRAL,
    NEWLY_BROKEN,
    NEWLY_WORKING,
    REGRESSION,
    compare,
)
from benchmarks.runner.plan import HIGHER_IS_BETTER, INFORMATIONAL, LOWER_IS_BETTER, MetricSpec, Plan, Scenario
from benchmarks.runner.probe import ScenarioResult


def _plan(metrics=None, scenario_ids=("s1",)):
    return Plan(
        probe="udp_export",
        pr=1,
        baseline_ref="aaa",
        head_ref="bbb",
        scenarios=[Scenario(id=i) for i in scenario_ids],
        metrics=metrics or [MetricSpec(key="rate", label="Rate", direction=HIGHER_IS_BETTER)],
    )


def _ok(**metrics):
    return ScenarioResult(scenario_id="s1", ok=True, metrics=metrics)


class Categorisation(unittest.TestCase):
    def test_both_clean_is_comparable(self):
        result = compare(_plan(), {"s1": _ok(rate=50.0)}, {"s1": _ok(rate=60.0)})
        self.assertEqual(result.scenarios[0].category, COMPARABLE)

    def test_failed_baseline_is_newly_working(self):
        """The EMSGSIZE case: baseline could not send at all, so there is no delta to compute."""
        baseline = ScenarioResult(scenario_id="s1", ok=True, metrics={"rate": 0.0, "export_failures": 5})
        result = compare(_plan(), {"s1": baseline}, {"s1": _ok(rate=90.0, export_failures=0)})
        self.assertEqual(result.scenarios[0].category, NEWLY_WORKING)

    def test_failed_head_is_newly_broken(self):
        head = ScenarioResult(scenario_id="s1", ok=True, metrics={"rate": 0.0, "export_failures": 3})
        result = compare(_plan(), {"s1": _ok(rate=90.0)}, {"s1": head})
        self.assertEqual(result.scenarios[0].category, NEWLY_BROKEN)

    def test_probe_error_counts_as_failure(self):
        errored = ScenarioResult(scenario_id="s1", ok=False, error="host too small")
        result = compare(_plan(), {"s1": errored}, {"s1": errored})
        self.assertEqual(result.scenarios[0].category, BOTH_FAILED)

    def test_invoke_errors_count_as_failure(self):
        """Tier B reports failure through invoke_errors rather than export_failures."""
        baseline = ScenarioResult(scenario_id="s1", ok=True, metrics={"rate": 0.0, "invoke_errors": 2})
        result = compare(_plan(), {"s1": baseline}, {"s1": _ok(rate=10.0)})
        self.assertEqual(result.scenarios[0].category, NEWLY_WORKING)

    def test_absent_result_is_missing(self):
        result = compare(_plan(), {}, {"s1": _ok(rate=1.0)})
        self.assertEqual(result.scenarios[0].category, MISSING)
        self.assertEqual(result.scenarios[0].deltas, [])

    def test_every_planned_scenario_appears(self):
        result = compare(_plan(scenario_ids=("s1", "s2")), {"s1": _ok(rate=1.0)}, {"s1": _ok(rate=1.0)})
        self.assertEqual([s.scenario_id for s in result.scenarios], ["s1", "s2"])


class Verdicts(unittest.TestCase):
    def _verdict(self, direction, baseline, head):
        plan = _plan([MetricSpec(key="m", label="M", direction=direction)])
        result = compare(plan, {"s1": _ok(m=baseline)}, {"s1": _ok(m=head)})
        return result.scenarios[0].deltas[0]

    def test_higher_is_better_improves(self):
        self.assertEqual(self._verdict(HIGHER_IS_BETTER, 50.0, 80.0).verdict, IMPROVEMENT)

    def test_higher_is_better_regresses(self):
        self.assertEqual(self._verdict(HIGHER_IS_BETTER, 80.0, 50.0).verdict, REGRESSION)

    def test_lower_is_better_inverts(self):
        self.assertEqual(self._verdict(LOWER_IS_BETTER, 80.0, 50.0).verdict, IMPROVEMENT)
        self.assertEqual(self._verdict(LOWER_IS_BETTER, 50.0, 80.0).verdict, REGRESSION)

    def test_informational_never_judges(self):
        self.assertEqual(self._verdict(INFORMATIONAL, 1.0, 1000.0).verdict, NEUTRAL)

    def test_small_change_is_noise(self):
        """Under the 5% threshold, a difference is not narrated as a win or a loss."""
        self.assertEqual(self._verdict(HIGHER_IS_BETTER, 100.0, 102.0).verdict, NEUTRAL)

    def test_identical_values_are_neutral(self):
        self.assertEqual(self._verdict(HIGHER_IS_BETTER, 7.0, 7.0).verdict, NEUTRAL)

    def test_zero_baseline_has_no_percentage(self):
        """0 -> n is not an infinite improvement; the delta stands but the percentage is withheld."""
        delta = self._verdict(HIGHER_IS_BETTER, 0.0, 42.0)
        self.assertEqual(delta.delta, 42.0)
        self.assertIsNone(delta.delta_pct)
        self.assertEqual(delta.verdict, IMPROVEMENT)

    def test_missing_metric_yields_no_delta(self):
        plan = _plan([MetricSpec(key="absent", label="Absent", direction=HIGHER_IS_BETTER)])
        result = compare(plan, {"s1": _ok(other=1.0)}, {"s1": _ok(other=2.0)})
        delta = result.scenarios[0].deltas[0]
        self.assertIsNone(delta.baseline)
        self.assertIsNone(delta.delta)


if __name__ == "__main__":
    unittest.main()
