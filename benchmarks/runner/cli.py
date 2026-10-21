# Copyright Amazon.com, Inc. or its affiliates. All Rights Reserved.
# SPDX-License-Identifier: Apache-2.0
"""Deterministic executor for an agent-designed benchmark plan.

The agent designs the plan and interprets the results; this CLI only measures, and it measures the same
way every time. Nothing here calls a model: a model deciding timing or sampling mid-run would make the
baseline and head runs incomparable.

Results are written as JSON lines as each scenario finishes, so a run that is interrupted -- or whose
terminal goes away -- keeps everything completed up to that point.

    python -m benchmarks.runner.cli probes
    python -m benchmarks.runner.cli prepare --ref main --dir /tmp/bench-main
    python -m benchmarks.runner.cli exec --plan plan.yaml --distro-src <src> --out baseline.jsonl
    python -m benchmarks.runner.cli compare --plan plan.yaml --baseline b.jsonl --head h.jsonl --out c.json
    python -m benchmarks.runner.cli report --plan plan.yaml --comparison c.json --out report.md
"""
import argparse
import dataclasses
import json
import subprocess
import sys
from pathlib import Path
from typing import Dict, List

from . import plan as plan_module
from . import probe as probe_module
from .compare import Comparison, MetricDelta, ScenarioComparison, compare
from .probe import ScenarioResult
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


def _cmd_probes(_args: argparse.Namespace) -> int:
    names = probe_module.available()
    if not names:
        print("no probes found under benchmarks/probes/")
        return 1
    for name in names:
        loaded = probe_module.load(name)
        print(f"{name}")
        for key, description in loaded.describe_params().items():
            print(f"    {key}: {description}")
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


def _cmd_exec(args: argparse.Namespace) -> int:
    plan = plan_module.load(Path(args.plan))
    distro_src = Path(args.distro_src).resolve()
    if not distro_src.is_dir():
        print(f"--distro-src {distro_src} is not a directory", file=sys.stderr)
        return 1
    # Prepend so the worktree's distro wins over any installed copy
    sys.path.insert(0, str(distro_src))

    selected = probe_module.load(plan.probe)
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    only = set(args.only or [])

    context = probe_module.RunContext(
        run_id=args.run_id,
        ref_tag=args.ref_tag,
        ref=args.ref or args.ref_tag,
        distro_src=distro_src,
        out_dir=out.parent,
    )

    # Started before the loop: the probe imports distro modules lazily inside run(), so nothing of
    # interest is imported yet and module-level lines are still counted.
    coverage_session = _start_coverage(distro_src, out.parent) if args.coverage else None

    failures = 0
    probe_module.setup(selected, context)
    try:
        with out.open("w", encoding="utf-8") as handle:
            for scenario in plan.scenarios:
                if only and scenario.id not in only:
                    continue
                result = selected.run(scenario, context)
                handle.write(json.dumps(dataclasses.asdict(result)) + "\n")
                handle.flush()  # a killed run keeps what already finished
                status = "ok" if result.ok else f"ERROR {result.error}"
                print(f"[{plan.probe}] {scenario.id}: {status}", flush=True)
                if not result.ok:
                    failures += 1
    finally:
        # Always tear down, including after an exception: a probe may have left cloud resources running
        removed = probe_module.teardown(selected, context)
        if removed:
            print(f"torn down: {', '.join(str(r) for r in removed)}", flush=True)
        if coverage_session:
            _finish_coverage(coverage_session, plan, out.parent)

    print(f"wrote {out}")
    return 1 if failures and args.strict else 0


def _start_coverage(distro_src: Path, out_dir: Path):
    try:
        import coverage
    except ImportError:
        print("--coverage requested but the coverage package is not installed; skipping", file=sys.stderr)
        return None
    session = coverage.Coverage(
        branch=True,
        source=[str(distro_src)],
        data_file=str(out_dir / ".coverage"),
    )
    session.start()
    return session


def _finish_coverage(session, plan, out_dir: Path) -> None:
    """Report branch coverage, highlighting the files the plan's scenarios claim to target."""
    session.stop()
    session.save()
    report_path = out_dir / "coverage.json"
    try:
        session.json_report(outfile=str(report_path))
    except Exception as exc:  # pylint: disable=broad-except
        print(f"coverage report failed: {exc}", file=sys.stderr)
        return

    data = json.loads(report_path.read_text())
    targeted = {t.split(":")[0] for s in plan.scenarios for t in s.targets}
    rows = []
    for path, entry in sorted(data.get("files", {}).items()):
        if any(Path(path).name == name for name in targeted):
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
        probe=data["probe"],
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


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="benchmarks.runner.cli", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("probes", help="list available probes and the scenario params they accept")

    prepare = sub.add_parser("prepare", help="create a worktree for a ref and print its distro src path")
    prepare.add_argument("--ref", required=True)
    prepare.add_argument("--dir", required=True)

    merge = sub.add_parser("merge-plan", help="fold base scenarios into a generated plan and validate it")
    merge.add_argument("--generated", required=True)
    merge.add_argument("--base", required=True)
    merge.add_argument("--out", required=True)

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
        "probes": _cmd_probes,
        "prepare": _cmd_prepare,
        "merge-plan": _cmd_merge_plan,
        "exec": _cmd_exec,
        "compare": _cmd_compare,
        "report": _cmd_report,
    }
    return handlers[args.command](args)


if __name__ == "__main__":
    sys.exit(main())
