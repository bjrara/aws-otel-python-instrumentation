# Integration: running the PR benchmark from a coding agent

What another agent or operator needs in order to run this. The authoritative prompt is
[`.claude/skills/benchmark-pr/SKILL.md`](../.claude/skills/benchmark-pr/SKILL.md) — for Claude Code it is
invocable as `/benchmark-pr <pr>`; for any other agent, paste its body as the instruction.

Design state and open gaps: [`GAPS.md`](GAPS.md).

## The split

The agent designs and interprets; the runner measures. The runner contains **no model call** — a model
deciding timing or sampling mid-run would make the baseline and head runs incomparable.

| Decided by the agent | Fixed in the runner |
|---|---|
| Which metrics matter for this diff | How a scenario is executed |
| Which scenarios reach the new branches | Datagram/byte/latency/CPU/RSS accounting |
| What the caveats are | Outcome categorisation and delta arithmetic |
| Whether to post the report | Markdown rendering |

## Prompt to hand to an agent

> Benchmark PR <N> in <owner>/<repo>. Follow the instructions in
> `.claude/skills/benchmark-pr/SKILL.md` exactly. Resolve the baseline from the PR's `baseRefOid`, not
> from a branch name. Design the metric set and the scenarios from the diff, generate the plan once and
> use it for both refs, run the execution in a Linux container, and write the caveats yourself after
> reading the raw metrics. Show me the report and wait for my agreement before posting anything to the PR.

## Commands

```bash
python -m benchmarks.runner.cli probes          # probes and the scenario params each accepts
python -m benchmarks.runner.cli prepare     --ref <sha> --dir <dir>
python -m benchmarks.runner.cli merge-plan  --generated <g.yaml> --base benchmarks/plan/base.yaml --out <plan.yaml>
python -m benchmarks.runner.cli exec        --plan <plan.yaml> --distro-src <worktree>/aws-opentelemetry-distro/src --out <ref>.jsonl [--only id ...] [--strict]
python -m benchmarks.runner.cli compare     --plan <plan.yaml> --baseline <b.jsonl> --head <h.jsonl> --out <c.json>
python -m benchmarks.runner.cli report      --plan <plan.yaml> --comparison <c.json> --out <report.md> --caveat "..."
```

## Environment

| Requirement | Why |
|---|---|
| Linux host or container | macOS caps `net.inet.udp.maxdgram` at 9216; the `udp_export` probe refuses to measure below 65,507 rather than return a wrong number |
| `pip install -r benchmarks/requirements.txt` | Runner and probe deps. The distro is **not** installed — it comes from a worktree via `--distro-src` |
| Python 3.10+ | Matches the distro's supported runtimes |
| `gh` authenticated | Reading the PR's refs, and posting the report. Posts as the user, not as an app |
| `udp_export` only | Loopback sockets. No AWS credentials, no network egress |

### Additionally for the `lambda_udp` probe

| Requirement | Why |
|---|---|
| AWS credentials for `us-east-1` | Creates a role, layer version, function and log group per ref, then destroys them |
| IAM actions | `lambda:CreateFunction`/`DeleteFunction`/`Invoke`/`PublishLayerVersion`/`DeleteLayerVersion`/`ListLayers`/`ListFunctions`, `iam:CreateRole`/`DeleteRole`/`AttachRolePolicy`/`DetachRolePolicy`/`ListAttachedRolePolicies`/`PassRole`/`ListRoles`, `logs:FilterLogEvents`/`DeleteLogGroup`, `cloudwatch:ListMetrics`/`GetMetricData`, `xray` write via the function's managed policy |
| Docker, authenticated to ECR Public | The layer build pulls SAM base images. `aws ecr-public get-login-password --region us-east-1 \| docker login --username AWS --password-stdin public.ecr.aws` |
| ~5 GB free in the Docker VM | The three SAM base images. `docker builder prune -f` if the build fails with "no space left on device" |

No API Gateway and no public endpoint: the function is driven by `lambda:Invoke`. If B2 (HTTP-level
throughput) is ever added, the decided auth posture is `AWS_IAM` on the method — see `GAPS.md` C3.

## Artifacts

All under `benchmarks/results/<runid>/`, gitignored and ephemeral:

| File | Contents |
|---|---|
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

## Adding a probe for a different change

A probe is a package under `benchmarks/probes/<name>/` exposing a module-level `PROBE` with `run(scenario)`
and `describe_params()`. It owns everything specific to one change path; `benchmarks/runner/` stays
unaware of it and is never edited to add one. Scenario `params` are free-form and validated by the probe,
so a probe for a JVM heap fix or an EC2 request path needs no change to the plan schema.

Verifying that boundary against a genuinely different PR is the next piece of work — see `GAPS.md` A3.

## Verification status

Both probes have been run end to end against PR #1 (`59a7f29` → `bcf1bfd`).

- `udp_export`: 11 scenarios in a `python:3.12-slim` container — 8 newly-working, 2 comparable, 1 failed
  on both refs. With `--coverage`, four lite scenarios reach 50% of the branches in
  `_udp_span_payload.py`.
- `lambda_udp`: deployed, invoked, collected Application Signals RED, and tore down cleanly. Verified no
  `udpbench*` resources remain afterwards.

Unit tests cover the comparison logic: `python -m unittest discover -s benchmarks/tests -t .`
