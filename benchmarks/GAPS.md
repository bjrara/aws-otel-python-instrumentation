# PR Benchmark Harness — Iteration 1 Gaps

Status as of **2026-10-20**. Nothing is implemented yet; this records the design, the
decisions already made, and the open gaps so the work can be resumed.

**Resume order:** build the Lambda/Python tier first (see [Scope](#scope)), validate it against
`bjrara/aws-otel-python-instrumentation` PR #1, *then* work the structural gaps in section A by
adding the Java/EC2 adapter as the second implementation.

## Goal

Manually-triggered benchmark that runs against a PR, compares before/after, and publishes a
report as a PR comment. Requirements as stated:

| # | Requirement | Status |
|---|---|---|
| 1 | Benchmark against the PR, manual trigger | designed |
| 2 | Base tests (user-predefined, optional) + dynamic tests (AI-designed for the PR) | designed; see A1/A2 |
| 3 | Run twice — before and after the change | designed |
| 4 | If there's an API, test throughput | designed; see A5 |
| 5 | Compare CPU, memory, critical change-path metrics; API p99 | designed; see A1 |
| 6 | Generate a report and publish as a comment | designed |
| 7 | Application Signals RED + runtime metrics | designed; see B1 |
| 8 | Record integration work (prompt/code for a coding agent) | designed, not written |

## Scope

Reference PR for iteration 1: **`bjrara/aws-otel-python-instrumentation` PR #1** —
`fix: split and truncate UDP span exports that exceed the datagram limit`, head `bcf1bfd`,
base `main`.

Two tiers:

- **Tier A (local, no AWS)** — UDP receiver on loopback with pinned `SO_RCVBUF`, drives
  `export()` directly. Measures delivery rate, datagram count, truncation, flush latency.
- **Tier B (deployed)** — sample app + API Gateway in AWS; throughput, p50/p90/p99,
  Lambda duration and max memory, Application Signals RED.

Matrix: `{lite, full} x {baseline, head} x scenarios` — lite and full mode are separate code
paths in this PR, so four Tier B deployments per run.

## Decisions already locked (do not relitigate)

- Region **us-east-1**, pinned explicitly in code. The shell has `AWS_REGION=us-west-2` while
  `aws configure get region` says `us-east-1`; the env var wins in the SDK, so anything
  unpinned silently lands in the wrong region.
- Deploy with a **boto3 script**, not Terraform. Terraform is not installed on this machine, and
  the repo's modules pull `terraform-aws-modules/lambda`.
- **Always destroy** at end of run, pass or fail.
- Dynamic tests are **ephemeral** — not committed, not preserved across runs — but generated
  **once per invocation and shared by both refs**, so before/after compares identical work.
- Branch coverage of the changed files is reported as a number (`coverage --branch`), so
  "cover branches as much as possible" is measurable rather than aspirational.
- Resources are prefixed `udpbench-<runid>-<ref>-*` and tagged `benchmark-run=<runid>`;
  teardown walks that prefix only.

## Planned layout (not yet created)

```
benchmarks/README.md
benchmarks/GAPS.md                   # this file
benchmarks/requirements.txt          # requests, psutil, coverage
benchmarks/plan/base.yaml            # optional, user-written base tests
benchmarks/runner/{cli,scenarios,udp_receiver,exporter_bench,
                   api_bench,aws_stack,resources,compare,report}.py
benchmarks/INTEGRATION.md            # handoff doc for a coding agent
benchmarks/agent/benchmark-pr.md     # SKILL.md-shaped, droppable into .claude/skills/
lambda-layer/sample-apps/function/benchmark_handler.py   # new; existing handler untouched
```

---

## A. Structural gaps

Found by evaluating the iteration-1 design against
[aws-otel-java-instrumentation#1468](https://github.com/aws-observability/aws-otel-java-instrumentation/pull/1468)
("resolve profiler OOM bug, interning functions taking too much memory" — Java, EC2-testable,
merged, +2045/-197 in `instrumentation/serviceevents`). The orchestration layer transferred;
everything PR-specific did not.

**A1. Change-path metrics are hardcoded. This is the real design flaw.**
Iteration 1 baked in datagrams / delivery-rate / truncation as "the critical metrics". For an
OOM fix the critical metrics are live-heap-after-GC, GC count and pause time, interning table
size, profile payload bytes, `HeapGuard` pressure trips and abandoned windows, and OOM
occurrences. **`MetricSpec` must be generated from the diff alongside the scenarios**, not
be a constant in the code. Fix this regardless of which repo runs next.

**A2. Scenario generation needs a "how do I reach this branch" concept.**
For UDP it was a scalar — exceed ~49 KB. For #1468 it is a *load shape*: sustained heap
pressure plus deep, varied stack frames to inflate interning. `HeapGuard` has two thresholds
(`START_LIMIT_FRACTION`, `PRESSURE_FRACTION`) that scenarios must straddle on both sides.
Picking a number is easy; synthesizing pressure is not.

**A3. Refactor into three layers.**
Repo-agnostic core (orchestration, baseline/head, compare, report, publish, integration doc) +
per-repo adapters (build, deploy target, metric sources) + per-PR generated plan (scenarios
*and* metric specs).

**A4. Baseline ref must come from the PR's `baseRefOid` — resolved, and worse than it looked.**
Iteration 1 defaulted to `--baseline main`. Two independent ways that breaks: PR #1468's base is
`feat/otlp-profiler`, a feature branch; and in *this* repo local `main` had drifted 2 commits
ahead of `origin/main` and already contained the UDP change, so `main` and `fix-udp` pointed at
the same commit. Benchmarking by branch name would have compared identical code and reported
"no difference" — a confident wrong answer. Fixed: `prepare` takes a SHA and the skill requires
`baseRefOid`.

**A5. API metrics must be conditional on the change path intersecting the request path.**
Requirement 4/5 assume an API is on the change path. In #1468 there is an API (Spring MVC
sample app) but the change is a *background profiler*, so p99 is a side-effect guard
("did overhead regress"), not the primary measure.

**A6. Build step must be an adapter.**
`pip install -e` per worktree vs Gradle agent-jar build plus `-javaagent:` attach. Java builds
take minutes, so the 4-cell matrix x 2 refs gets expensive — caching matters.

**A7. Deploy target must be an adapter.**
Everything Lambda-specific does not generalize: `REPORT`-line memory scraping, cold-start
warming, `OTEL_AWS_LAMBDA_FAST_START`. EC2 needs instance provisioning, agent install, a
service unit, and the CloudWatch agent for host metrics.

**A8. Crash / OOM is a first-class outcome, not a runner error.**
An OOM kills the process. Needs crash detection plus log capture as a result category.

**A9. JVM-specific benchmarking rigor.**
Pinned `-Xmx`, same collector, warmup to steady state, multiple forks. A single run's heap
number is noise.

**A10. Prefer an existing mock collector over a custom sink.**
`appsignals-tests/images/mock-collector` in the Java repo (which #1468 extends with
`MockCollectorProfilesService`) is the direct analog of the planned `udp_receiver`. Target the
repo's own mock collector where one exists.

**A11. The "baseline-failed" category is load-bearing, keep it.**
Already in the design and it survived the Java evaluation. On `main`, oversized payloads fail
with `EMSGSIZE` and deliver 0%; in #1468 the baseline OOMs. Report three categories —
regression, improvement, newly-working — never a percentage delta against a crash.

## B. Unverified assumptions

**B1. Is Application Signals RED datapoint completeness a valid dropped-datagram proxy? — tested, NO.**
The hypothesis was that because lite-mode RED derives from spans crossing the UDP path under test,
missing RED datapoints would be a better delivery signal than any synthetic rate. Measured on a real
function: `red_request_count` was 11 (10 invocations + 1 warm-up) in **every** cell — including the
baseline `oversized-split`, where the function's own logs show 22 `EMSGSIZE` errors and all oversized
spans lost. `red_datapoint_shortfall_pct` stayed 0. **RED request count does not fall when spans are
dropped**, so it is not a usable proxy; the metric is retained only as a sanity check that traffic
reached Application Signals at all.

One weaker signal did correlate: `red_dimension_sets` was 1 on the baseline `oversized-split` versus
3 everywhere else, i.e. fewer dimension granularities got published when spans were lost. Suggestive,
single observation, not something to build a check on yet.

Confirmed in-repo: metric names `Error`, `Fault`, `Latency` (ms) at
`aws_span_metrics_processor_builder.py:13-15`, namespace `ApplicationSignals`
(`aws_opentelemetry_configurator.py:644,713`). There is **no separate Call/Count metric** —
request rate comes from `Latency`'s `SampleCount`.

Runtime metrics: gated by `OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED`, scope
`opentelemetry.instrumentation.system_metrics`, instruments `system.network.connections`,
`process.open_file_descriptor.count`, `process.runtime.*.memory`, `process.runtime.*.gc_count`,
`process.runtime.*.thread_count`. Not meaningful on Lambda; exercised once the EC2 adapter
lands (A7).

**B2. Absolute delivery rate is not predictive of Lambda.**
Loss depends on the receiver's socket receive buffer, which the real X-Ray daemon controls and
this repo does not. Tier A pins `SO_RCVBUF` for comparability only. The report must say this
rather than let the number look authoritative.

**B3. Application Signals ingestion delay.**
Metrics aggregate on ~1-minute boundaries with ingestion lag. Each Tier B run needs an explicit
configurable settle wait before `GetMetricData`, not a sleep buried in the code.

**B4. Lambda CPU scales with configured memory.**
Memory size, runtime, and architecture must be pinned identically across refs, and cold starts
warmed out or measured separately — otherwise CPU/memory deltas are noise.

## C. Environment and access gaps

**C1. No scoped deploy role.** Current credentials are
`arn:aws:iam::633750930120:user/k8s-admin` with `AdministratorAccess` attached directly.
Confirmed a personal development account. A scoped deploy role would be better than the admin
user; not created.

**C2. The account is not empty.** ~15 Lambda functions and layers already exist —
`JavaAppBuilderStack-*`, `TrafficGeneratorBuilderSt-*`, `TestSetupStack-*`, `cwsyn-pc-*`
Synthetics canaries, `js-sample-app`, `top-observation-poc`. "Empty sandbox" means an isolated
*namespace*, enforced by the run prefix and tag. Never run a blanket destroy.

**C3. Public API Gateway endpoint during the run.** Short-lived and destroyed at the end, but
internet-reachable while up. API key or IAM auth not added — decide before first deploy.

**C4. Required IAM actions** (Tier B): `lambda:*` incl. `PublishLayerVersion`, `apigateway:*`,
`iam:CreateRole` / `AttachRolePolicy` / `PassRole`, `s3` for the layer artifact,
`logs:FilterLogEvents`, `xray`, `cloudwatch:GetMetricData`, `cloudwatch:ListMetrics`, optionally
`application-signals:ListServices`.

**C5. Function env vars**: `AWS_LAMBDA_EXEC_WRAPPER=/opt/otel-instrument`,
`OTEL_AWS_APPLICATION_SIGNALS_ENABLED=true`, `OTEL_AWS_LAMBDA_FAST_START` toggled per matrix
cell, `OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED=false` on Lambda.

**C6. GitHub.** The existing `gh` token has `repo` scope — enough to post the report comment,
and it posts as the user, not as an app. A `workflow_dispatch` variant would instead need
`permissions: pull-requests: write` and the built-in `GITHUB_TOKEN`.

**C7. `performance-tests/` is dead and not reusable.** Java/Gradle + k6 + testcontainers
measuring distro overhead through a collector. Its sample app was deleted in `2db7a00`, CI only
runs `spotlessCheck` on it (`pr-build.yml:360`), and no results directory exists. New tree.

**C8. The sample app is too small.** `lambda-layer/sample-apps/function/lambda_function.py`
emits a handful of small spans and never crosses the multi-datagram boundary. Needs a separate
`benchmark_handler.py` with configurable span volume; leave the existing handler alone.

## D. Related AWS DevOps Agent findings

Context from investigating whether DevOps Agent could do this natively. It cannot, which is why
this harness exists.

**D1. Automated PR review is private-repos-only.** Confirmed in docs and by testing. Manual
PR-scoped review works on public repos but **cannot post to GitHub** — the chat path has no
GitHub write tool. Verified by asking it directly; it declined and explained.

**D2. Verification testing never produced results.** Both reviews requested it
(`skipAutomatedTesting: false`) but every finding came back `source: static_analysis` with no
build or test records. Likely cause: release testing targets deployed web apps and REST APIs,
and this repo is a library. Enabling vended `APPLICATION_LOGS` on the agent space would
confirm whether an environment was ever provisioned. Not enabled.

**D3. Docs inconsistency, unreconciled.** The code-review page describes verification testing as
cloning and building your code in an **AWS-managed** environment; the release-testing page
describes running against a **deployed instance** in a **customer-provisioned** environment with
a target URL. These read as two distinct mechanisms and the docs do not reconcile them.

**D4. GitHub Action name inconsistency.** Prose says
`aws-actions/devops-agent-release-testing@v1`, the example YAML says
`aws-actions/devops-agent-qa@v1`. Verify against the marketplace before relying on either.
This action *does* post a Check Run to a PR, so it is the one documented path to PR-visible
results on a public repo — but it still needs a deployed app.

**D5. Repo indexing was incomplete when the reviews ran.** The repo was connected at 19:56 UTC
and both reviews ran within ~16 min, inside the documented 1-2 hour indexing window. Both
reports state "No repository knowledge or dependency graph was available". A re-run after
indexing would be a better baseline for comparing against this harness.

Useful identifiers: agent space `c031e16b-4437-4c5f-a458-fc59beb84bb0`, GitHub service
`53ebc66a-367f-4b06-8977-df91d5fdf50b`, repo association
`95faa2f2-336a-4733-b7fc-a36f4140619a`. Reading `capabilities` on an association requires
botocore >= 1.43.116 (1.42.92 renders the release-management config as `SDK_UNKNOWN_MEMBER`).

## E. Findings from running Tier A

Discovered by executing the harness, not by designing it. Both would have produced confidently
wrong numbers.

**E1. macOS cannot measure this at all.** `net.inet.udp.maxdgram` defaults to **9216** bytes, so
every datagram the splitter produces fails with `EMSGSIZE` regardless of the code under test.
The first run reported `ok` with 0% delivery on *both* refs — a plausible-looking result that was
purely the host. `probes/udp_export/host.py` now binary-searches the real limit, records it as
`host_max_datagram_bytes`, and refuses to measure below 65,507 unless
`params.allow_undersized_host` is set. Tier A runs in a `python:3.12-slim` container.

**E2. Absolute delivery rate is harness-bound, not just buffer-bound.** Head delivers ~5% on the
200x6 KB split scenarios against the PR's reported 89-98%. Two causes: `net.core.rmem_max` silently
caps `SO_RCVBUF` (asked 8 MB, granted 425,984), and the receiver is a single Python thread that
cannot drain 50 x 48 KB fast enough — delivery stayed ~5% even with 8.76 datagrams of headroom.
So the bottleneck is the receiver, not the buffer. `rcvbuf_datagram_headroom` and
`effective_rcvbuf_bytes` are recorded to make this visible. **Delivery rate is usable only as a
relative signal between refs.** The trustworthy metrics are datagram counts, `EMSGSIZE` counts,
truncation and drop counts, latency, CPU, and RSS. Raising `net.core.rmem_max` under Docker
Desktop needs a privileged container, which was not done. A multi-threaded or C receiver would
raise the ceiling if an absolute number is ever needed.

**E3. The layer build has two environmental failure modes, both hit on the first attempt.**
`public.ecr.aws` returned `403 Forbidden` for all three SAM base images until Docker was
authenticated (`aws ecr-public get-login-password --region us-east-1 | docker login --username AWS
--password-stdin public.ecr.aws`) — the same registry problem behind this PR's red CI and commit
`59a7f29`. Then the build filled the Docker VM ("no space left on device"); `docker builder prune -f`
freed 20.9 GB. `layer.py` now detects both in the error output and prints the remedy, and cleans up
its build context in a `finally`. First successful build took 190s; the second ref took 24s on cache.

**E4. Application Signals publishes the same metric at several dimension granularities.**
Summing `Latency` `SampleCount` across every dimension set `ListMetrics` returns gave a request count
of 33 for 11 invocations. Dimension sets are now reduced with `max()` rather than `sum()`, and
`red_dimension_sets` records how many were found. This matters directly for B1: the whole point of
`red_datapoint_shortfall_pct` is comparing datapoints against invocations, and a triple-counted
numerator silently clamps the shortfall to zero — it would have reported "no spans lost" regardless
of the truth.

## F. Progress

**Built and verified (Tier A).** Run end to end against PR #1 (`59a7f29` -> `bcf1bfd`),
11 scenarios, in a Linux container: 8 newly-working, 2 comparable, 1 failed on both refs.

```
benchmarks/runner/{plan,probe,resources,compare,report,cli}.py   # repo- and PR-agnostic core
benchmarks/probes/udp_export/{__init__,bench,receiver,spans,host}.py
benchmarks/plan/base.yaml          # optional user-written base scenarios
benchmarks/requirements.txt
benchmarks/INTEGRATION.md
.claude/skills/benchmark-pr/SKILL.md   # the entry point: /benchmark-pr <pr>
```

The core/probe split from A3 was pulled forward rather than deferred: PR-specific code lives in
`benchmarks/probes/<name>/` behind `PROBE.run(scenario)`, and `Scenario.params` is free-form so the
plan schema does not change per probe. A1 is also done — metric specs live in the plan.

**Also built and verified (Tier B, as the `lambda_udp` probe).** Deploys the change to a real Lambda
function and drives it with `lambda:Invoke` — no API Gateway, nothing with a public endpoint. Run
end to end: deployed, invoked, collected Application Signals RED, tore down cleanly with no
`udpbench*` resources left behind.

```
benchmarks/probes/lambda_udp/{__init__,layer,stack,invoke,signals}.py
lambda-layer/sample-apps/function/benchmark_handler.py
benchmarks/tests/test_compare.py      # 15 tests over categorisation and delta arithmetic
benchmarks/README.md
```

Branch coverage is wired into `exec --coverage` and reports per-file branch coverage for the files a
plan's scenarios target, so A2's "cover the branches" is now measurable. Four lite scenarios reach
50% of the branches in `_udp_span_payload.py`.

The probe contract gained `RunContext` plus optional `setup`/`teardown`, because Tier B needs
per-ref deployment around the scenario loop. `exec` calls teardown in a `finally` so an exception
cannot leave a deployed function running. This validated the A3 boundary: adding `lambda_udp`
required no edit to `benchmarks/runner/` beyond that contract change.

**Resolved.** A1, A3, A4, A5 (API metrics: dropped API Gateway entirely; throughput and p99 now come
from concurrent `lambda:Invoke`), A8 for this probe, C3 (decided: `AWS_IAM` if B2 is ever built).

**Still not built.** HTTP-level throughput through API Gateway (B2 — deliberately dropped). The EC2
runtime-metric source, so `OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED` metrics remain unexercised.
A6, A7, A9, A10 stay open and are best addressed by building a probe for a genuinely different
change, which is what would really test the abstraction.
