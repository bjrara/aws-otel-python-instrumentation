# PR Benchmark Harness — status and open gaps

Status as of **2026-10-21**. Built and run end to end against
`bjrara/aws-otel-python-instrumentation` PR #1.

**This file tracks only the persisted parts of the harness** — `runner/`, `environments/`, the skill
and the docs. Gaps about a generated instrument are deliberately not tracked: instruments are written
into a run's sandbox, used once and discarded, so a TODO list for one would contradict the design.
Anything change-specific belongs with the run or in the PR comment.

The reasoning behind the safeguards — what each was added after, and which measurements lied — lives
in [`DESIGN.md`](DESIGN.md) under "Why the safeguards exist". That is rationale, not a backlog.

## Goal

Manually-triggered benchmark that runs against a PR, compares before/after, and publishes a
report as a PR comment. Requirements as stated:

| # | Requirement | Status |
|---|---|---|
| 1 | Benchmark against the PR, manual trigger | **done** — `/benchmark-pr <pr>`, no automatic trigger |
| 2 | Base tests (optional) + dynamic tests (AI-designed for the PR) | **done** — `plan/base.yaml` merged with a generated plan; 11 scenarios verified, 2 of them base |
| 3 | Run twice — before and after the change | **done** — one worktree and one `exec` per ref; both `baseline.jsonl` and `head.jsonl` carry measured data |
| 4 | If there's an API, test throughput | **done differently** — API Gateway dropped by decision; throughput from concurrent `lambda:Invoke` (7.16 → 5.87 req/s). No HTTP-level throughput, see G6 |
| 5 | Compare CPU, memory, change-path metrics; API p99 | **done, partly informational** — CPU/RSS from `local_process`, max memory from Lambda `REPORT`, p99 in both. Conformance demoted CPU, RSS and p99 in `local_process` as noisy there |
| 6 | Generate a report and publish as a comment | **done** — posted to PR #1 as comment `6307433353` |
| 7 | Application Signals RED + runtime metrics | **RED done** (`Latency`/`Error`/`Fault`, count from `SampleCount`). **Runtime metrics not built**, see G5 |
| 8 | Record integration work for a coding agent | **done** — `SKILL.md`, `INTEGRATION.md`, `DESIGN.md` |

## Scope

Reference PR: **PR #1** — `fix: split and truncate UDP span exports that exceed the datagram limit`,
head `bcf1bfd`, base `59a7f29` (the PR's `baseRefOid`; **not** local `main`, which had drifted to
contain the change).

Two environments, both persisted:

- **`local_process`** — loopback sink with pinned `SO_RCVBUF`, drives `export()` directly. Publishes
  `real_telemetry_backend: false`. Covers lite and full mode.
- **`lambda_python`** — a real function and layer per ref, driven by `lambda:Invoke`. No API Gateway,
  no public endpoint. Lite mode only, see G2.

## Decisions locked (do not relitigate)

- Region **us-east-1**, pinned in code. The shell has `AWS_REGION=us-west-2` while `aws configure get
  region` says `us-east-1`; the env var wins in the SDK, so anything unpinned lands in the wrong place.
- Deploy with **boto3**, not Terraform.
- **Always destroy** at end of run, pass or fail.
- Instruments are **generated per run and discarded**, but generated **once per invocation and shared
  by both refs**, so before/after compares identical work.
- Branch coverage of the changed files is reported as a number.
- Resources are prefixed `udpbench-<runid>-<ref>-*` and tagged `benchmark-run=<runid>`; teardown
  asserts that prefix before every delete.
- If HTTP-level throughput is ever added, the auth posture is **`AWS_IAM`** on the method — simplest
  and strongest, and API keys are metering rather than authentication.

## Layout

```
benchmarks/README.md                 # how to run it
benchmarks/DESIGN.md                 # architecture, dataflow, why the safeguards exist
benchmarks/INTEGRATION.md            # handing it to another agent; env and IAM requirements
benchmarks/GAPS.md                   # this file
benchmarks/requirements.txt
benchmarks/plan/base.yaml            # optional, user-written base scenarios
benchmarks/runner/{cli,plan,capabilities,environment,instrument,
                   compare,report,resources}.py
benchmarks/environments/local_process/{__init__,host,udp_sink}.py
benchmarks/environments/lambda_python/{__init__,layer,stack,invoke,signals}.py
benchmarks/templates/instrument_template.py
benchmarks/tests/test_compare.py
benchmarks/results/<runid>/          # gitignored: plan, instrument, jsonl, report
.claude/skills/benchmark-pr/SKILL.md # the entry point
lambda-layer/sample-apps/function/benchmark_handler.py
```

## Verified

Verified by having been run, not by inspection. Reference run: PR #1, `59a7f29` → `bcf1bfd`.

| What | Evidence |
|---|---|
| Before/after measurement | One worktree and one `exec` per ref; `baseline.jsonl` and `head.jsonl` both populated |
| `local_process` | 11 scenarios in a `python:3.12-slim` container: 8 newly-working, 2 comparable, 1 failed on both, 0 metric regressions |
| `lambda_python` | Three real functions created and destroyed (CloudTrail `CreateFunction`/`DeleteFunction` at 16:49, 16:53, 16:58 on 2026-10-20). Baseline logged 22 `EMSGSIZE`, head none, at ~18% throughput and ~13% p99 |
| Conformance gate | PASS on all 11 scenarios, after it forced four metrics to informational |
| Branch coverage | 69.7% of `_udp_span_payload.py`, 75% of `otlp_udp_exporter.py`, 38.9% of `opentelemetry_lite_sdk.py` |
| Teardown | `find_orphans()` returns empty after every run |
| Comparison logic | 15 unit tests |
| Publishing | PR #1 comment `6307433353` |

## Resolved

Predicted before implementation, now closed:

- **A1 — Metric specs belong in the plan**, not in code; what matters is a property of the diff.
- **A2 — Scenarios must reach the new branches**, and branch coverage makes that measurable.
- **A3 — The core/instrument boundary holds** — adding `lambda_python` required no edit to `runner/`
  beyond the `RunContext` + `setup`/`teardown` contract change.
- **A4 — Baseline must come from the PR's `baseRefOid`.** Two independent ways branch names break: a PR
  based on a feature branch, and — as happened here — a local `main` that had drifted to contain the
  change, making `main` and `fix-udp` the same commit. Benchmarking by name would have compared
  identical code and reported "no difference".
- **A5 — API metrics are conditional** on the change path intersecting the request path.
- **A8 — Failure is a reported outcome**, not a runner error.
- **A11 — Categories before arithmetic** — a percentage delta against a baseline that could not run
  is meaningless, so `newly-working` is its own category.

## Open

**C1. No scoped deploy role.** Credentials are an IAM user with `AdministratorAccess` attached
directly. Works, but broader than this needs.

**C2. The account is shared, not empty.** ~15 unrelated Lambda functions and layers exist
(`JavaAppBuilderStack-*`, `TrafficGeneratorBuilderSt-*`, `cwsyn-pc-*` canaries, `js-sample-app`).
"Sandbox" means an isolated *namespace*, enforced by the run prefix and the teardown guard. Never run
a blanket destroy.

**A6. The build step should be an adapter.** `lambda_python` builds a Python layer. A JVM change needs
a Gradle agent-jar build plus `-javaagent:` attach, and Java builds are slow enough that caching
matters.

**A7. The deploy target should be an adapter.** `REPORT`-line scraping, cold-start warming and
`OTEL_AWS_LAMBDA_FAST_START` are Lambda-specific. EC2 needs instance provisioning, agent install, a
service unit and the CloudWatch agent.

**A9. JVM-specific rigor is absent.** Pinned `-Xmx`, same collector, warmup to steady state, multiple
forks. A single run's heap number is noise.

**A10. Prefer a repo's own mock collector over a custom sink** where one exists — e.g.
`appsignals-tests/images/mock-collector` in the Java repo.

**G1. `lambda_python` has no conformance run behind it.** Its throughput and p99 deltas are therefore
directional only — nothing has shown them to exceed run-to-run noise, and `local_process` taught us
that four metrics there looked meaningful and were not. The most misleading gap on this list, because
the Lambda numbers are the ones most likely to be quoted. Fixing it costs two more deployments per
conformance run.

**G2. Full mode is unmeasured on real Lambda.** `lambda_python.setup` hardcodes `lite_mode=True`
(`__init__.py:50`), so only the lite path is exercised there. `local_process` covers both, so the
planned `{lite, full} x {baseline, head}` matrix is half-built.

**G4. No preflight command.** A missing prerequisite surfaces mid-run rather than up front. The ECR
Public token expiring after ~12h is the likeliest; `layer.py` prints the remedy in its error, so it is
self-diagnosing but not early.

**G5. EC2 runtime metrics unbuilt.** `OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED` metrics
(`process.runtime.*.memory`, `*.gc_count`, `*.thread_count`, `system.network.connections`) remain
unexercised. Meaningless on Lambda; needs an EC2 environment.

**G6. HTTP-level throughput.** Deliberately dropped with API Gateway. Not a defect — recorded so it is
not mistaken for one.

**G7. The abstraction has only been tested on one change.** A6, A7, A9 and A10 stay open until an
instrument and environment pair are built for something genuinely different. The Java/EC2 heap fix in
[aws-otel-java-instrumentation#1468](https://github.com/aws-observability/aws-otel-java-instrumentation/pull/1468)
is the intended second case: it transfers the orchestration but replaces every change-specific part,
which is exactly the test the boundary needs.
