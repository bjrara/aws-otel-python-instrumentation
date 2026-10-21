# PR benchmark harness

Benchmarks a pull request against its base commit and reports the difference as a PR comment.

**Entry point:** `/benchmark-pr <pr>` — the agent-facing prompt lives in
[`.claude/skills/benchmark-pr/SKILL.md`](../.claude/skills/benchmark-pr/SKILL.md). For other agents or
for CI, see [`INTEGRATION.md`](INTEGRATION.md). Design decisions and open gaps are in
[`GAPS.md`](GAPS.md).

## How it is split

The agent designs the benchmark and interprets the results. The runner measures and decides nothing —
there is **no model call anywhere in `runner/` or `probes/`**. That split is deliberate: a model
choosing timing or sampling mid-run would make the baseline and head runs incomparable.

```
runner/     repo- and PR-agnostic: plan model, probe loading, comparison, report, CLI
probes/     one package per change path; owns everything specific to what is being measured
plan/       optional hand-written base scenarios that run every time
results/    per-run artifacts, gitignored and ephemeral
tests/      unit tests for the comparison logic
```

A probe is selected by name from the plan, so adding one never means editing `runner/`.

## Probes

| Probe | Measures | Needs |
|---|---|---|
| `udp_export` | The UDP exporter in-process against a local receiver — datagram counts, truncation, drops, latency, CPU, RSS | A Linux host. Nothing else |
| `lambda_udp` | The same change on a real Lambda function against the real X-Ray daemon — throughput, p99, memory, Application Signals RED | AWS credentials. Creates and destroys a function, layer, role and log group |

```bash
python -m benchmarks.runner.cli probes   # names and the scenario params each accepts
```

## Running it by hand

```bash
# 1. resolve the real base commit -- never a branch name, see GAPS.md A4
gh pr view 1 --json baseRefOid,headRefOid

# 2. one worktree per ref
python -m benchmarks.runner.cli prepare --ref <baseRefOid> --dir /tmp/bench-base
python -m benchmarks.runner.cli prepare --ref <headRefOid> --dir /tmp/bench-head

# 3. write a plan, then fold in the base scenarios
python -m benchmarks.runner.cli merge-plan \
    --generated benchmarks/results/<runid>/generated.yaml \
    --base benchmarks/plan/base.yaml \
    --out benchmarks/results/<runid>/plan.yaml

# 4. measure each ref with the same plan
python -m benchmarks.runner.cli exec --plan <plan> --distro-src <worktree>/aws-opentelemetry-distro/src \
    --run-id <runid> --ref-tag baseline --ref <baseRefOid> --out <runid>/baseline.jsonl [--coverage]

# 5. compare and render
python -m benchmarks.runner.cli compare --plan <plan> --baseline <b.jsonl> --head <h.jsonl> --out <c.json>
python -m benchmarks.runner.cli report  --plan <plan> --comparison <c.json> --out <report.md> --caveat "..."
```

`udp_export` must run on Linux — macOS caps `net.inet.udp.maxdgram` at 9216 bytes and the probe refuses
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
used for. The two that keep recurring: `udp_export`'s absolute delivery rate is bounded by the harness
receiver and does not predict the X-Ray daemon, and `host_max_datagram_bytes` below 65,507 invalidates
the run entirely.

## Tests

```bash
python -m unittest discover -s benchmarks/tests -t .
```

## Cost and safety

`udp_export` is loopback-only and touches no cloud. `lambda_udp` creates resources in
`us-east-1`, all named `udpbench-<runid>-<ref>-*` and tagged `benchmark-run=<runid>`; teardown asserts
that prefix before every delete and runs in a `finally`, so an exception cannot leave a function behind.
Nothing is given a public endpoint — the function is driven by `lambda:Invoke`. Costs are a few thousand
invocations, well under a dollar.
