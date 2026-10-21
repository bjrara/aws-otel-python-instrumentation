# PR benchmark harness

Benchmarks a pull request against its base commit and reports the difference as a PR comment.

**Entry point:** `/benchmark-pr <pr>` — the agent-facing prompt lives in
[`.claude/skills/benchmark-pr/SKILL.md`](../.claude/skills/benchmark-pr/SKILL.md). For other agents or
for CI, see [`INTEGRATION.md`](INTEGRATION.md). The architecture and dataflow are in
[`DESIGN.md`](DESIGN.md); decisions already made and open gaps are in [`GAPS.md`](GAPS.md).

## How it is split

The agent designs the benchmark and interprets the results. The runner measures and decides nothing —
there is **no model call anywhere in `runner/` or `environments/`**. That split is deliberate: a model
choosing timing or sampling mid-run would make the baseline and head runs incomparable.

```
runner/        repo- and PR-agnostic: plan model, capabilities, comparison, report, CLI
environments/  PERSISTED, change-agnostic: where a benchmark runs and what it can represent
templates/     scaffold for generating an instrument
plan/          optional hand-written base scenarios that run every time
results/       per-run artifacts including the generated instrument; gitignored and ephemeral
tests/         unit tests for the comparison logic
```

The split is **infrastructure vs. instrumentation**. An environment is provisioned once and reused
across many PRs. An instrument knows what one diff does, is generated into the run directory, and is
thrown away with it — code that knows about a specific change has no business accumulating in a
harness.

What survives a discarded instrument is the knowledge: environments publish **capabilities** and refuse
runs they cannot represent, and `conformance` proves a fresh instrument is quiet before anyone believes
its numbers.

## Environments

| Environment | Represents | Needs |
|---|---|---|
| `local_process` | In-process export against a loopback sink — fast and deterministic, but `real_telemetry_backend: false` | A Linux host. Nothing else |
| `lambda_python` | A real function, real X-Ray daemon, real Application Signals | AWS credentials. Creates and destroys a function, layer, role and log group |

```bash
python -m benchmarks.runner.cli environments   # names and the capabilities each offers
```

## Conformance

A generated instrument is assumed wrong until it measures the **same commit twice** with no signal:

```bash
python -m benchmarks.runner.cli conformance --plan <plan> --distro-src <src>
```

It has already caught `delivery_rate_pct` varying 9.2 to 4.8 on identical code. The fix is to mark such
a metric `informational` and say why in its label, not to widen the gate. A scenario that fails on both
identical refs is consistent, not noise, and passes.

## Running it by hand

```bash
# 1. resolve the real base commit -- never a branch name, see GAPS.md "Resolved" A4
gh pr view 1 --json baseRefOid,headRefOid

# 2. write the instrument and the plan into the run directory
#    instrument: copy templates/instrument_template.py and fill it in for this diff only
#    plan: names `environment`, the `instrument` path, scenarios and metrics
python -m benchmarks.runner.cli environments            # pick one; check its capabilities
cp benchmarks/templates/instrument_template.py benchmarks/results/<runid>/instrument.py

# 3. fold in the optional base scenarios and validate
python -m benchmarks.runner.cli merge-plan \
    --generated benchmarks/results/<runid>/generated.yaml \
    --base benchmarks/plan/base.yaml \
    --out benchmarks/results/<runid>/plan.yaml

# 4. one worktree per ref
python -m benchmarks.runner.cli prepare --ref <baseRefOid> --dir /tmp/bench-base
python -m benchmarks.runner.cli prepare --ref <headRefOid> --dir /tmp/bench-head

# 5. prove the instrument is quiet BEFORE trusting any real numbers
python -m benchmarks.runner.cli conformance --plan <plan> \
    --distro-src /tmp/bench-head/aws-opentelemetry-distro/src --ref <headRefOid>

# 6. measure each ref with the same plan and the same instrument
python -m benchmarks.runner.cli exec --plan <plan> --distro-src <worktree>/aws-opentelemetry-distro/src \
    --run-id <runid> --ref-tag baseline --ref <baseRefOid> --out <runid>/baseline.jsonl [--coverage]

# 7. compare and render
python -m benchmarks.runner.cli compare --plan <plan> --baseline <b.jsonl> --head <h.jsonl> --out <c.json>
python -m benchmarks.runner.cli report  --plan <plan> --comparison <c.json> --out <report.md> --caveat "..."
```

`local_process` must run on Linux — macOS caps `net.inet.udp.maxdgram` at 9216 bytes and the environment refuses
to measure rather than return a number that reflects the host instead of the code:

```bash
docker run --rm -v "$PWD":/work -v /tmp/bench-head:/head:ro -w /work python:3.12-slim bash -c '
  pip install -q -r benchmarks/requirements.txt && python -m benchmarks.runner.cli exec ...'
```

## Reading a report

Scenarios are categorised before anything is subtracted, because a percentage delta against a baseline
that could not run is meaningless:

| Category | Meaning |
|---|---|
| Comparable | Both refs ran; per-metric deltas apply |
| Newly working | The baseline could not run at all. **Not** an improvement percentage |
| Newly broken | The head fails where the baseline worked |
| Failed on both | Neither ref ran; usually the scenario or the environment, not the change |

Changes under 5% are reported as neutral. Metrics marked `informational` never get a verdict.

Caveats are written by the agent per run and are not decoration — they state what each number cannot be
used for. The two that keep recurring: `local_process`'s absolute delivery rate is bounded by the harness
receiver and does not predict the X-Ray daemon, and `host_max_datagram_bytes` below 65,507 invalidates
the run entirely.

## Tests

```bash
python -m unittest discover -s benchmarks/tests -t .
```

## Cost and safety

`local_process` is loopback-only and touches no cloud. `lambda_python` creates resources in
`us-east-1`, all named `udpbench-<runid>-<ref>-*` and tagged `benchmark-run=<runid>`; teardown asserts
that prefix before every delete and runs in a `finally`, so an exception cannot leave a function behind.
Nothing is given a public endpoint — the function is driven by `lambda:Invoke`. Costs are a few thousand
invocations, well under a dollar.
