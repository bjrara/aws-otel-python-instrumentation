# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic executor for an agent-designed benchmark.

The agent designs the plan and the instrument and interprets the results; this CLI only measures, and
it measures the same way every time. Nothing here calls a model: a model deciding timing or sampling
mid-run would make the baseline and head runs incomparable.

Two halves meet here. The *environment* is persisted and change-agnostic -- provisioned once, reused
across PRs. The *instrument* is generated per run and thrown away, because code that knows what one
PR does has no business accumulating in a harness. Since it is generated, `conformance` exists to
prove it is quiet before its numbers are believed.

Results are written as JSON lines as each scenario finishes, so a run that is interrupted -- or whose
terminal goes away -- keeps everything completed up to that point.

    python -m benchmarks.runner.cli environments
    python -m benchmarks.runner.cli prepare --ref <sha> --dir /tmp/bench-base
    python -m benchmarks.runner.cli conformance --plan plan.yaml --distro-src <src>
    python -m benchmarks.runner.cli exec --plan plan.yaml --distro-src <src> --ref-tag head --out head.jsonl
    python -m benchmarks.runner.cli compare --plan plan.yaml --baseline b.jsonl --head h.jsonl --out c.json
    python -m benchmarks.runner.cli report --plan plan.yaml --comparison c.json --out report.md
"""
import argparse
import dataclasses
import json
import subprocess
import sys
import tempfile
from pathlib import Path
from typing import Dict, List

from . import capabilities as capabilities_module
from . import environment as environment_module
from . import instrument as instrument_module
from . import plan as plan_module
from .compare import BOTH_FAILED, COMPARABLE, NEUTRAL, Comparison, MetricDelta, ScenarioComparison, compare
from .environment import RunContext, ScenarioResult
from .report import render

_DISTRO_RELATIVE_SRC = "aws-opentelemetry-distro/src"


def _load_results(path: Path) -> Dict[str, ScenarioResult]:
    results: Dict[str, ScenarioResult] = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        record = json.loads(line)
        results[record["scenario_id"]] = ScenarioResult(**record)
    return results


def _cmd_environments(_args: argparse.Namespace) -> int:
    names = environment_module.available()
    if not names:
        print("no environments found under benchmarks/environments/")
        return 1
    for env_name in names:
        env = environment_module.load(env_name)
        print(env_name)
        for key, value in sorted(env.capabilities().items()):
            print(f"    {key}: {value}")
    return 0


def _cmd_prepare(args: argparse.Namespace) -> int:
    """Create a detached worktree for a ref and print the distro source path to use with exec."""
    target = Path(args.dir).resolve()
    if target.exists():
        print(f"{target} already exists; remove it or pass a different --dir", file=sys.stderr)
        return 1
    subprocess.run(
        ["git", "worktree", "add", "--detach", str(target), args.ref],
        check=True,
        cwd=Path(__file__).resolve().parents[2],
    )
    src = target / _DISTRO_RELATIVE_SRC
    if not src.is_dir():
        print(f"warning: {src} not found in worktree for {args.ref}", file=sys.stderr)
    print(src)
    return 0


def _cmd_merge_plan(args: argparse.Namespace) -> int:
    """Fold the user's base scenarios into a generated plan, then validate the result."""
    import yaml  # local: only this command needs it

    generated = yaml.safe_load(Path(args.generated).read_text())
    merged = plan_module.merge_base(generated, Path(args.base))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(yaml.safe_dump(merged, sort_keys=False), encoding="utf-8")
    loaded = plan_module.load(out)  # fail loudly here rather than mid-run
    print(f"wrote {out} ({len(loaded.scenarios)} scenarios, {len(loaded.metrics)} metrics)")
    return 0


def _run_plan(
    plan,
    plan_path: Path,
    distro_src: Path,
    out: Path,
    run_id: str,
    ref_tag: str,
    ref: str,
    only: set,
    use_coverage: bool = False,
    quiet: bool = False,
) -> int:
    """Execute a plan against one ref. Returns the number of scenarios that failed to run."""
    sys.path.insert(0, str(distro_src))  # the worktree's distro wins over any installed copy

    env = environment_module.load(plan.environment)
    tool = instrument_module.load(plan.instrument_path(plan_path))
    instrument_module.supports(tool, env.name)

    context = RunContext(
        run_id=run_id, ref_tag=ref_tag, ref=ref or ref_tag, distro_src=distro_src, out_dir=out.parent
    )
    out.parent.mkdir(parents=True, exist_ok=True)
    coverage_session = _start_coverage(distro_src, out.parent) if use_coverage else None

    failures = 0
    environment_module.setup(env, context)
    try:
        with out.open("w", encoding="utf-8") as handle:
            for scenario in plan.scenarios:
                if only and scenario.id not in only:
                    continue
                result = _run_scenario(env, tool, scenario, context)
                handle.write(json.dumps(dataclasses.asdict(result)) + "\n")
                handle.flush()  # a killed run keeps what already finished
                if not quiet:
                    status = "ok" if result.ok else f"ERROR {result.error}"
                    print(f"[{env.name}] {scenario.id}: {status}", flush=True)
                if not result.ok:
                    failures += 1
    finally:
        # Always tear down, including after an exception: an environment may hold cloud resources
        removed = environment_module.teardown(env, context)
        if removed and not quiet:
            print(f"torn down: {', '.join(str(r) for r in removed)}", flush=True)
        if coverage_session:
            _finish_coverage(coverage_session, plan, out.parent, quiet)
    return failures


def _run_scenario(env, tool, scenario, context) -> ScenarioResult:
    """Environment produces raw output; the instrument says what it means."""
    try:
        raw = env.execute(tool, scenario, context)
        metrics = dict(raw.get("metrics", {}))
        metrics.update(tool.observe(raw, scenario))
        metrics.update(capabilities_module.describe(raw.get("_capabilities", {})))
        labels = {"environment": env.name}
        if raw.get("function_name"):
            labels["function"] = raw["function_name"]
        return ScenarioResult(scenario_id=scenario.id, ok=True, metrics=metrics, labels=labels)
    except Exception as exc:  # pylint: disable=broad-except
        return ScenarioResult(
            scenario_id=scenario.id, ok=False, error=f"{type(exc).__name__}: {exc}"
        )


def _cmd_exec(args: argparse.Namespace) -> int:
    plan_path = Path(args.plan)
    plan = plan_module.load(plan_path)
    distro_src = Path(args.distro_src).resolve()
    if not distro_src.is_dir():
        print(f"--distro-src {distro_src} is not a directory", file=sys.stderr)
        return 1

    out = Path(args.out)
    failures = _run_plan(
        plan,
        plan_path,
        distro_src,
        out,
        run_id=args.run_id,
        ref_tag=args.ref_tag,
        ref=args.ref,
        only=set(args.only or []),
        use_coverage=args.coverage,
    )
    print(f"wrote {out}")
    return 1 if failures and args.strict else 0


def _cmd_conformance(args: argparse.Namespace) -> int:
    """Measure one ref twice and require every verdict to be neutral.

    A generated instrument is assumed wrong until this passes. Comparing a commit against itself must
    produce no signal; anything else means the instrument is noisy, miscounting, or measuring the
    harness rather than the code. This is the gate that makes throwaway instruments safe to trust.
    """
    plan_path = Path(args.plan)
    plan = plan_module.load(plan_path)
    distro_src = Path(args.distro_src).resolve()
    only = set(args.only or [])

    with tempfile.TemporaryDirectory(prefix="conformance-") as tmp:
        runs = []
        for index in ("a", "b"):
            out = Path(tmp) / f"{index}.jsonl"
            _run_plan(
                plan,
                plan_path,
                distro_src,
                out,
                run_id=f"conf-{index}",
                ref_tag=f"conf-{index}",
                ref=args.ref or "same-ref",
                only=only,
                quiet=args.quiet,
            )
            runs.append(_load_results(out))

        # Compare only what was actually run: with --only, unselected scenarios are absent from both
        # sides and would otherwise be reported as "missing", i.e. as instrument noise.
        scoped = plan
        if only:
            scoped = dataclasses.replace(plan, scenarios=[s for s in plan.scenarios if s.id in only])
        comparison = compare(scoped, runs[0], runs[1])

    noisy = []
    for scenario in comparison.scenarios:
        # BOTH_FAILED on identical refs is consistent, not noisy -- a scenario that deterministically
        # fails is fine. What must not happen is the same commit landing in different categories.
        if scenario.category not in (COMPARABLE, BOTH_FAILED):
            noisy.append((scenario.scenario_id, f"inconsistent category={scenario.category}", ""))
            continue
        for delta in scenario.deltas:
            if delta.verdict != NEUTRAL:
                noisy.append(
                    (
                        scenario.scenario_id,
                        delta.key,
                        f"{delta.baseline} -> {delta.head} ({delta.verdict})",
                    )
                )

    if not noisy:
        print(f"conformance PASS: {len(comparison.scenarios)} scenario(s), no signal between identical refs")
        return 0
    print(f"conformance FAIL: {len(noisy)} non-neutral result(s) measuring the same commit twice")
    for scenario_id, key, detail in noisy:
        print(f"    {scenario_id}: {key} {detail}")
    print(
        "The instrument reports differences where there are none. Fix it before trusting a real run:\n"
        "    - raise repeats so percentiles settle, or widen the noise threshold for that metric\n"
        "    - mark genuinely variable metrics informational so they carry no verdict\n"
        "    - check the instrument is not measuring the harness instead of the code"
    )
    return 1


def _cmd_compare(args: argparse.Namespace) -> int:
    plan = plan_module.load(Path(args.plan))
    comparison = compare(plan, _load_results(Path(args.baseline)), _load_results(Path(args.head)))
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dataclasses.asdict(comparison), indent=2), encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _rehydrate(data: dict) -> Comparison:
    scenarios = []
    for raw in data["scenarios"]:
        deltas = [MetricDelta(**d) for d in raw.pop("deltas", [])]
        scenarios.append(ScenarioComparison(deltas=deltas, **raw))
    return Comparison(
        pr=data["pr"],
        environment=data["environment"],
        instrument=data.get("instrument", ""),
        baseline_ref=data["baseline_ref"],
        head_ref=data["head_ref"],
        scenarios=scenarios,
    )


def _cmd_report(args: argparse.Namespace) -> int:
    plan = plan_module.load(Path(args.plan))
    comparison = _rehydrate(json.loads(Path(args.comparison).read_text()))
    markdown = render(comparison, caveats=list(args.caveat or []), notes=plan.notes)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(markdown, encoding="utf-8")
    print(f"wrote {out}")
    return 0


def _start_coverage(distro_src: Path, out_dir: Path):
    try:
        import coverage
    except ImportError:
        print("--coverage requested but the coverage package is not installed; skipping", file=sys.stderr)
        return None
    session = coverage.Coverage(branch=True, source=[str(distro_src)], data_file=str(out_dir / ".coverage"))
    session.start()
    return session


def _finish_coverage(session, plan, out_dir: Path, quiet: bool = False) -> None:
    """Report branch coverage, highlighting the files the plan's scenarios claim to target."""
    session.stop()
    session.save()
    report_path = out_dir / "coverage.json"
    try:
        session.json_report(outfile=str(report_path))
    except Exception as exc:  # pylint: disable=broad-except
        print(f"coverage report failed: {exc}", file=sys.stderr)
        return
    if quiet:
        return

    data = json.loads(report_path.read_text())
    targeted = {t.split(":")[0] for s in plan.scenarios for t in s.targets}
    rows = []
    for path, entry in sorted(data.get("files", {}).items()):
        if any(Path(path).name == target for target in targeted):
            summary = entry["summary"]
            covered = summary.get("covered_branches", 0)
            total = summary.get("num_branches", 0)
            rows.append((Path(path).name, covered, total, 100.0 * covered / total if total else 0.0))

    if not rows:
        print(f"coverage written to {report_path} (no plan targets matched measured files)")
        return
    print("branch coverage of files the plan targets:")
    for name, covered, total, pct in rows:
        print(f"    {name}: {covered}/{total} branches ({pct:.1f}%)")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmarks.runner.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("environments", help="list persisted environments and the capabilities each offers")

    prepare = sub.add_parser("prepare", help="create a worktree for a ref and print its distro src path")
    prepare.add_argument("--ref", required=True)
    prepare.add_argument("--dir", required=True)

    merge = sub.add_parser("merge-plan", help="fold base scenarios into a generated plan and validate it")
    merge.add_argument("--generated", required=True)
    merge.add_argument("--base", required=True)
    merge.add_argument("--out", required=True)

    conformance = sub.add_parser(
        "conformance", help="measure one ref twice; any non-neutral result means the instrument is unsound"
    )
    conformance.add_argument("--plan", required=True)
    conformance.add_argument("--distro-src", required=True)
    conformance.add_argument("--ref", default="")
    conformance.add_argument("--only", nargs="*")
    conformance.add_argument("--quiet", action="store_true")

    execute = sub.add_parser("exec", help="run a plan's scenarios against one ref's distro source")
    execute.add_argument("--plan", required=True)
    execute.add_argument("--distro-src", required=True, help="path to <worktree>/aws-opentelemetry-distro/src")
    execute.add_argument("--out", required=True, help="JSON lines output, flushed per scenario")
    execute.add_argument("--run-id", default="local", help="groups resources and artifacts for one run")
    execute.add_argument("--ref-tag", default="head", help="short label for this ref, e.g. baseline or head")
    execute.add_argument("--ref", default="", help="the commit being measured, recorded for the report")
    execute.add_argument("--only", nargs="*", help="run only these scenario ids")
    execute.add_argument("--strict", action="store_true", help="exit non-zero if any scenario errored")
    execute.add_argument(
        "--coverage",
        action="store_true",
        help="measure branch coverage of the distro source and report it for the files the plan targets",
    )

    comparison = sub.add_parser("compare", help="diff baseline and head results")
    comparison.add_argument("--plan", required=True)
    comparison.add_argument("--baseline", required=True)
    comparison.add_argument("--head", required=True)
    comparison.add_argument("--out", required=True)

    report = sub.add_parser("report", help="render a comparison as markdown")
    report.add_argument("--plan", required=True)
    report.add_argument("--comparison", required=True)
    report.add_argument("--out", required=True)
    report.add_argument("--caveat", action="append", help="repeatable; rendered under 'How to read this'")

    return parser


def main(argv: List[str] = None) -> int:
    args = build_parser().parse_args(argv)
    handlers = {
        "environments": _cmd_environments,
        "prepare": _cmd_prepare,
        "merge-plan": _cmd_merge_plan,
        "conformance": _cmd_conformance,
        "exec": _cmd_exec,
        "compare": _cmd_compare,
        "report": _cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
