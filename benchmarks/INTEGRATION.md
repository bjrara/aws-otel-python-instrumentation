# Integration: running the PR benchmark from a coding agent

What another agent or operator needs in order to run this. The authoritative prompt is
[`.claude/skills/benchmark-pr/SKILL.md`](../.claude/skills/benchmark-pr/SKILL.md) — for Claude Code it is
invocable as `/benchmark-pr <pr>`; for any other agent, paste its body as the instruction.

Design state and open gaps: [`GAPS.md`](GAPS.md).

## The split

The agent designs and interprets; the runner measures. The runner contains **no model call** — a model
deciding timing or sampling mid-run would make the baseline and head runs incomparable.

| Decided by the agent | Fixed in the harness |
|---|---|
| Which metrics matter for this diff | How a scenario is executed |
| Which scenarios reach the new branches | Outcome categorisation and delta arithmetic |
| The instrument that measures them, generated per run | The environment it runs in, persisted and reused |
| What the caveats are | Capability assertions and the conformance gate |
| Whether to post the report | Markdown rendering |

**Persisted vs. generated.** Environments under `benchmarks/environments/` are change-agnostic and
provisioned for the long term. Instruments are generated into `results/<runid>/`, used once, and never
committed — so the harness does not accumulate a datagram counter written for one PR. The knowledge
that would otherwise be lost lives in the environments' capabilities and in `conformance`.

## Prompt to hand to an agent

> Benchmark PR <N> in <owner>/<repo>. Follow the instructions in
> `.claude/skills/benchmark-pr/SKILL.md` exactly. Resolve the baseline from the PR's `baseRefOid`, not
> from a branch name. Design the metric set and the scenarios from the diff, generate the plan once and
> use it for both refs, run the execution in a Linux container, and write the caveats yourself after
> reading the raw metrics. Show me the report and wait for my agreement before posting anything to the PR.

## Commands

```bash
python -m benchmarks.runner.cli environments    # environments and the capabilities each offers
python -m benchmarks.runner.cli conformance  --plan <plan.yaml> --distro-src <src>   # same commit twice; must be silent
python -m benchmarks.runner.cli prepare     --ref <sha> --dir <dir>
python -m benchmarks.runner.cli merge-plan  --generated <g.yaml> --base benchmarks/plan/base.yaml --out <plan.yaml>
python -m benchmarks.runner.cli exec        --plan <plan.yaml> --distro-src <worktree>/aws-opentelemetry-distro/src --out <ref>.jsonl [--only id ...] [--strict]
python -m benchmarks.runner.cli compare     --plan <plan.yaml> --baseline <b.jsonl> --head <h.jsonl> --out <c.json>
python -m benchmarks.runner.cli report      --plan <plan.yaml> --comparison <c.json> --out <report.md> --caveat "..."
```

## Environment

| Requirement | Why |
|---|---|
| Linux host or container | macOS caps `net.inet.udp.maxdgram` at 9216; `local_process` publishes the real limit and refuses instruments requiring 65,507, rather than returning a wrong number |
| `pip install -r benchmarks/requirements.txt` | Runner, environment and instrument deps. The distro is **not** installed — it comes from a worktree via `--distro-src` |
| Python 3.10+ | Matches the distro's supported runtimes |
| `gh` authenticated | Reading the PR's refs, and posting the report. Posts as the user, not as an app |
| `local_process` only | Loopback sockets. No AWS credentials, no network egress |

### Additionally for the `lambda_python` environment

| Requirement | Why |
|---|---|
| AWS credentials for `us-east-1` | Creates a role, layer version, function and log group per ref, then destroys them |
| IAM actions | `lambda:CreateFunction`/`DeleteFunction`/`Invoke`/`PublishLayerVersion`/`DeleteLayerVersion`/`ListLayers`/`ListFunctions`, `iam:CreateRole`/`DeleteRole`/`AttachRolePolicy`/`DetachRolePolicy`/`ListAttachedRolePolicies`/`PassRole`/`ListRoles`, `logs:FilterLogEvents`/`DeleteLogGroup`, `cloudwatch:ListMetrics`/`GetMetricData`, `xray` write via the function's managed policy |
| Docker, authenticated to ECR Public | The layer build pulls SAM base images. `aws ecr-public get-login-password --region us-east-1 \| docker login --username AWS --password-stdin public.ecr.aws` |
| ~5 GB free in the Docker VM | The three SAM base images. `docker builder prune -f` if the build fails with "no space left on device" |

No API Gateway and no public endpoint: the function is driven by `lambda:Invoke`. If B2 (HTTP-level
throughput) is ever added, the decided auth posture is `AWS_IAM` on the method — see the locked decisions in `GAPS.md`.

## Artifacts

All under `benchmarks/results/<runid>/`, gitignored and ephemeral:

| File | Contents |
|---|---|
| `instrument.py` | The generated instrument. Ephemeral by design — regenerate, never reuse |
| `generated.yaml` | The agent's plan before base scenarios are merged |
| `plan.yaml` | The plan actually executed, for both refs |
| `baseline.jsonl`, `head.jsonl` | One JSON line per scenario, flushed as it completes |
| `comparison.json` | Categorised scenarios with per-metric deltas |
| `report.md` | What gets posted to the PR |

## Durability

`exec` flushes per scenario, so an interrupted run keeps everything that finished. For a long run, detach
it so it outlives the terminal:

```bash
setsid nohup python -m benchmarks.runner.cli exec ... > run.log 2>&1 &
```

The agent's reasoning does not survive its session. If a session ends mid-run, a fresh agent resumes from
`plan.yaml` plus whatever `*.jsonl` lines exist, and re-runs only the missing scenario ids with `--only`.

## Generating an instrument

Copy `benchmarks/templates/instrument_template.py` into the run directory and fill it in for one diff.
It declares `REQUIRES` (capability keys) and `SUPPORTS` (environment names), and implements `workload`,
`observe`, and `drive` for in-process environments. Then gate it with `conformance` before trusting it.

## Adding an environment

A package under `benchmarks/environments/<name>/` exposing `ENVIRONMENT` with `capabilities()` and
`execute(instrument, scenario, context)`, plus optional `setup`/`teardown` for anything it provisions.
`benchmarks/runner/` is never edited to add one. Verifying this boundary against a genuinely different
change — a JVM heap fix on EC2 — is the next piece of work; see `GAPS.md` G7.

## Verification status

Both environments have been run end to end against PR #1 (`59a7f29` → `bcf1bfd`).

- `local_process`: 11 scenarios in a `python:3.12-slim` container — 8 newly-working, 2 comparable, 1
  failed on both refs, 0 metric regressions. `--coverage` reaches 69.7% of the branches in
  `_udp_span_payload.py` and 75% in `otlp_udp_exporter.py`.
- `lambda_python`: deployed, invoked, collected Application Signals RED, tore down cleanly, and verified
  no `udpbench*` resources remain. On an oversized batch the baseline logged 22 `EMSGSIZE` errors and
  the head none, costing ~18% throughput and ~13% p99.
- `conformance`: passes on all 11 scenarios with no signal between identical refs — after it forced
  `delivery_rate_pct`, `export_latency_p99_ms`, `cpu_total_s` and `peak_rss_bytes` to be demoted to
  informational.

Unit tests cover the comparison logic: `python -m unittest discover -s benchmarks/tests -t .`
