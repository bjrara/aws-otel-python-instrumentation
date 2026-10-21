---
name: benchmark-pr
description: Benchmark a pull request against its base commit and report the difference. Use when asked to benchmark a PR, measure the performance impact of a change, compare before/after behaviour of a diff, or run the benchmark harness under benchmarks/. Trigger: /benchmark-pr <pr-number>.
---

# Benchmark a PR

You design the benchmark; the runner under `benchmarks/` executes it. Keep that split: you decide what
to measure and what it means, the runner decides nothing. Never put model judgement inside a measurement
loop — the baseline and head runs must do identical work or the comparison is worthless.

Two halves meet in a run. The **environment** is persisted and change-agnostic — a customer provisions
one once and reuses it across many PRs — and you pick one, you do not write one. The **instrument** is
change-specific, and you *generate* it per run into the run directory; it is thrown away afterwards and
must never be committed. Because it is generated, it is assumed wrong until a conformance run says
otherwise.

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

**An instrument.** Copy `benchmarks/templates/instrument_template.py` into the run directory and fill
it in for *this diff only* — do not generalise it. It declares `REQUIRES` (capability keys the
measurement depends on), `SUPPORTS` (environment names), and implements `workload`, `observe`, and for
in-process environments `drive`. Read the template's rules first; they encode measurement bugs this
harness has already shipped.

Pick the environment by what the change needs measured:

```
python -m benchmarks.runner.cli environments      # names and the capabilities each offers
```

`local_process` is fast and deterministic but offers `real_telemetry_backend: false`, so anything that
depends on a real receiver cannot be judged there. `lambda_python` deploys the change and costs minutes
plus AWS resources. If no environment can represent what the change needs, say so and stop — propose a
new environment rather than measuring in one that cannot support the claim.

Write the plan to `benchmarks/results/<runid>/generated.yaml` — it names `environment` and the
`instrument` path alongside the scenarios and metrics — then fold in the user's base scenarios:

```
python -m benchmarks.runner.cli merge-plan \
    --generated benchmarks/results/<runid>/generated.yaml \
    --base benchmarks/plan/base.yaml \
    --out benchmarks/results/<runid>/plan.yaml
```

Generate the plan **once** and use that same file for both refs. Results are ephemeral; `results/` is
gitignored and nothing there needs preserving.

## Step 3 — Prove the instrument is quiet

```
python -m benchmarks.runner.cli conformance --plan $R/plan.yaml --distro-src <head src> --ref <headRefOid>
```

This measures **one commit twice** and requires every verdict to come back neutral. A generated
instrument that reports differences where there are none is measuring noise or measuring the harness,
and its real numbers would be fiction. Do not run a real benchmark until this passes.

When it fails, the fix is usually to demote the offending metric to `informational` **and say why in its
label** — not to loosen the gate. This has already caught `delivery_rate_pct` swinging 9.2 to 4.8 on
identical code, and `export_latency_p99_ms`, `cpu_total_s` and `peak_rss_bytes` exceeding the 5%
threshold run to run. `both-failed` on identical refs is consistent, not noise, and passes.

## Step 4 — Prepare both refs

```
python -m benchmarks.runner.cli prepare --ref <baseRefOid> --dir /tmp/bench-base
python -m benchmarks.runner.cli prepare --ref <headRefOid> --dir /tmp/bench-head
```

Each prints the `--distro-src` path to use. Confirm the change is actually present in one and absent in
the other before spending time on a run.

## Step 5 — Execute

Run each ref with the **same** plan. `--run-id` groups the artifacts and any cloud resources;
`--ref-tag` labels the ref. Add `--coverage` to get branch coverage of the files your scenarios claim to
target — that is how "cover the new branches" becomes a number instead of an intention.

**`local_process` (in-process).** Needs a host that allows 65,507 byte datagrams. **macOS caps this at
9216** and the environment refuses to measure rather than return a wrong number, so run it in Linux:

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

**`lambda_python` (real Lambda).** Runs on the host, not in a container, and **creates AWS resources** — get
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
`python -c "from benchmarks.environments.lambda_python import stack; print(stack.find_orphans())"`.

## Step 6 — Compare and report

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

## Step 7 — Publish (ask first)

Posting to a PR is public and outward-facing. **Show the user the report and get explicit agreement
before posting.** When agreed:

```
gh pr comment <pr> --repo <owner>/<repo> --body-file $R/report.md
```

This posts as the user, not as an app. Say so.

## Stop and ask

- Creating AWS resources — the `lambda_python` environment does this. Say what will be created and where, and
  wait for agreement. Afterwards, confirm teardown actually removed it.
- Posting anything to GitHub.
- Running a privileged container, or changing a host sysctl.
- Anything that would make a service reachable beyond the local host.

## Do not

- Do not use base *branch names* as the baseline.
- Do not regenerate the plan between the baseline and head runs.
- Do not hardcode metrics into the instrument; they belong in the plan.
- Do not commit a generated instrument, and do not reuse one from a previous run.
- Do not skip conformance because the numbers "look reasonable" — that is exactly when it catches things.
- Do not report a delta against a failed baseline.
- Do not present a harness-bound number as a production prediction.
