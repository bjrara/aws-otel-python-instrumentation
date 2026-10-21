---
name: benchmark-pr
description: Benchmark a pull request against its base commit and report the difference. Use when asked to benchmark a PR, measure the performance impact of a change, compare before/after behaviour of a diff, or run the benchmark harness under benchmarks/. Trigger: /benchmark-pr <pr-number>.
---

# Benchmark a PR

You design the benchmark; the runner under `benchmarks/` executes it. Keep that split: you decide what
to measure and what it means, the runner decides nothing. Never put model judgement inside a measurement
loop — the baseline and head runs must do identical work or the comparison is worthless.

Read `benchmarks/GAPS.md` first. It records decisions already made and gaps already found; do not
relitigate them or rediscover them.

## Step 1 — Resolve the refs from the PR, not from branch names

```
gh pr view <pr> --repo <owner>/<repo> --json baseRefName,baseRefOid,headRefOid,title,body
```

Use **`baseRefOid`** as the baseline and **`headRefOid`** as the head. Do not use the base *branch name*:
a local branch of that name may have drifted or may already contain the change, in which case you would
benchmark identical code and report "no difference". This has already happened once in this repo.

## Step 2 — Read the diff and decide what matters

```
git diff <baseRefOid>...<headRefOid>
```

Then produce two things:

**Metrics.** Which measurements actually speak to this change. This is a property of the diff, not of the
harness — a datagram-splitting change cares about delivery and truncation; a memory fix cares about live
heap and GC. Write them as `metrics` entries with an honest `direction`
(`higher_is_better` / `lower_is_better` / `informational`). Mark a metric `informational` when "more" is
neither good nor bad, so the report does not claim a verdict it cannot support.

**Scenarios.** Cases that reach the branches the diff introduces. Aim for coverage of the *new decision
points*, not for volume. For each scenario state an `intent` in one line and list the `targets` it is
meant to exercise as `file.py:start-end`. Work out from the code what input actually reaches each branch —
a threshold to cross, a load shape to build, a boundary to straddle — and prefer several small scenarios
over one that tries to hit everything.

Check what params the probe accepts before writing them:

```
python -m benchmarks.runner.cli probes
```

If no probe fits the change, say so and stop. Writing a new probe is real work: a probe package under
`benchmarks/probes/<name>/` exposing `PROBE` with `run(scenario)` and `describe_params()`. Propose it and
get agreement rather than forcing the change into a probe that measures the wrong thing.

Write the plan to `benchmarks/results/<runid>/generated.yaml`, then fold in the user's base scenarios:

```
python -m benchmarks.runner.cli merge-plan \
    --generated benchmarks/results/<runid>/generated.yaml \
    --base benchmarks/plan/base.yaml \
    --out benchmarks/results/<runid>/plan.yaml
```

Generate the plan **once** and use that same file for both refs. Results are ephemeral; `results/` is
gitignored and nothing there needs preserving.

## Step 3 — Prepare both refs

```
python -m benchmarks.runner.cli prepare --ref <baseRefOid> --dir /tmp/bench-base
python -m benchmarks.runner.cli prepare --ref <headRefOid> --dir /tmp/bench-head
```

Each prints the `--distro-src` path to use. Confirm the change is actually present in one and absent in
the other before spending time on a run.

## Step 4 — Execute

Run each ref with the **same** plan. `--run-id` groups the artifacts and any cloud resources;
`--ref-tag` labels the ref. Add `--coverage` to get branch coverage of the files your scenarios claim to
target — that is how "cover the new branches" becomes a number instead of an intention.

**`udp_export` (in-process).** Needs a host that allows 65,507 byte datagrams. **macOS caps this at
9216** and the probe refuses to measure rather than return a wrong number, so run it in Linux:

```
docker run --rm -v "$PWD":/work -v /tmp/bench-base:/base:ro -v /tmp/bench-head:/head:ro \
  -w /work python:3.12-slim bash -c '
    pip install -q -r benchmarks/requirements.txt
    R=benchmarks/results/<runid>
    python -m benchmarks.runner.cli exec --plan $R/plan.yaml --distro-src /base/aws-opentelemetry-distro/src \
        --run-id <runid> --ref-tag baseline --ref <baseRefOid> --out $R/baseline.jsonl --coverage
    python -m benchmarks.runner.cli exec --plan $R/plan.yaml --distro-src /head/aws-opentelemetry-distro/src \
        --run-id <runid> --ref-tag head --ref <headRefOid> --out $R/head.jsonl --coverage
  '
```

**`lambda_udp` (real Lambda).** Runs on the host, not in a container, and **creates AWS resources** — get
confirmation first (see Stop and ask). One function, layer, role and log group per ref, all prefixed
`udpbench-<runid>-<ref>` and destroyed in a `finally`. Nothing is given a public endpoint.

```
python -m benchmarks.runner.cli exec --plan $R/plan.yaml \
    --distro-src /tmp/bench-base/aws-opentelemetry-distro/src \
    --run-id <runid> --ref-tag baseline --ref <baseRefOid> --out $R/baseline.jsonl
```

Two things make this slow: the layer build is a multi-stage Docker build pulling SAM base images from
`public.ecr.aws` (authenticate first if it 403s or 429s — the command is in the error message), and each
scenario waits out Application Signals ingestion. Keep the first run to one or two scenarios.

Results are written as JSON lines and flushed per scenario, so an interrupted run keeps what finished.
For a long run, launch it detached (`setsid nohup ... &`) so it survives the terminal going away. If a
`lambda_udp` run dies before teardown, check for leftovers:
`python -c "from benchmarks.probes.lambda_udp import stack; print(stack.find_orphans())"`.

## Step 5 — Compare and report

```
python -m benchmarks.runner.cli compare --plan $R/plan.yaml --baseline $R/baseline.jsonl --head $R/head.jsonl --out $R/comparison.json
python -m benchmarks.runner.cli report --plan $R/plan.yaml --comparison $R/comparison.json --out $R/report.md --caveat "..." --caveat "..."
```

Scenarios land in categories rather than all getting deltas. **"Newly working" means the baseline could
not run at all** — present it as such, never as a percentage improvement over a crash.

The `--caveat` flags are yours to write and they are not optional. Read the raw metrics before you write
them and state, in plain terms, what each number cannot be used for. Things that have already bitten this
harness and must be surfaced whenever they apply:

- An absolute rate measured against a harness stand-in does not predict production. Check
  `rcvbuf_datagram_headroom` and `effective_rcvbuf_bytes`: when the receiver cannot keep up, a low
  delivery rate is the harness, not the change.
- `host_max_datagram_bytes` below 65,507 means the environment, not the code, produced the result.
- Metrics inside the noise threshold (5%) are reported as neutral; do not narrate them as wins.

Then read `report.md` yourself and summarise the findings for the user — which of them are real, which
are artefacts, and what you would do about them.

## Step 6 — Publish (ask first)

Posting to a PR is public and outward-facing. **Show the user the report and get explicit agreement
before posting.** When agreed:

```
gh pr comment <pr> --repo <owner>/<repo> --body-file $R/report.md
```

This posts as the user, not as an app. Say so.

## Stop and ask

- Creating AWS resources — the `lambda_udp` probe does this. Say what will be created and where, and
  wait for agreement. Afterwards, confirm teardown actually removed it.
- Posting anything to GitHub.
- Running a privileged container, or changing a host sysctl.
- Anything that would make a service reachable beyond the local host.

## Do not

- Do not use base *branch names* as the baseline.
- Do not regenerate the plan between the baseline and head runs.
- Do not hardcode metrics into probe code; they belong in the plan.
- Do not report a delta against a failed baseline.
- Do not present a harness-bound number as a production prediction.
