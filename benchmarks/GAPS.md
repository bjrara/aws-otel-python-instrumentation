# PR Benchmark Harness — status and gaps

Status as of **2026-10-21**. The harness is built and has been run end to end against
`bjrara/aws-otel-python-instrumentation` PR #1. This file records what is verified, what is not, and
the findings worth not rediscovering.

Sections A-E were written before implementation and are kept as the record of what was predicted
versus what running it actually exposed. **Sections F and G describe the current state** -- read those
first, and treat A-E as history where they conflict.

## Goal

Manually-triggered benchmark that runs against a PR, compares before/after, and publishes a
report as a PR comment. Requirements as stated:

| # | Requirement | Status |
|---|---|---|
| 1 | Benchmark against the PR, manual trigger | **done** -- `/benchmark-pr <pr>`, no automatic trigger |
| 2 | Base tests (user-predefined, optional) + dynamic tests (AI-designed for the PR) | **done** -- `plan/base.yaml` merged with a generated plan; 11 scenarios in the verified run, 2 of them base |
| 3 | Run twice -- before and after the change | **done** -- one worktree and one `exec` per ref; both `baseline.jsonl` and `head.jsonl` carry measured data |
| 4 | If there's an API, test throughput | **done differently** -- API Gateway dropped by decision; throughput comes from concurrent `lambda:Invoke` (7.16 -> 5.87 req/s measured). No HTTP-level throughput |
| 5 | Compare CPU, memory, critical change-path metrics; API p99 | **done, partly informational** -- CPU/RSS from `local_process`, max memory from Lambda `REPORT`, p99 in both. Conformance demoted CPU, RSS and p99 to informational in `local_process` because they are noisy there |
| 6 | Generate a report and publish as a comment | **done** -- posted to PR #1 as comment `6307433353` |
| 7 | Application Signals RED + runtime metrics | **RED done** (`Latency`/`Error`/`Fault`, request count from `SampleCount`). **Runtime metrics not built** -- meaningless on Lambda, and the EC2 source is unimplemented |
| 8 | Record integration work (prompt/code for a coding agent) | **done** -- `.claude/skills/benchmark-pr/SKILL.md`, `INTEGRATION.md`, `DESIGN.md` |

## Scope

Reference PR: **`bjrara/aws-otel-python-instrumentation` PR #1** --
`fix: split and truncate UDP span exports that exceed the datagram limit`, head `bcf1bfd`,
base `59a7f29` (the PR's `baseRefOid`; **not** local `main`, which had drifted to contain the change).

The "Tier A / Tier B" naming below was superseded by the environment split in section F:

- **Tier A** is now the `local_process` environment -- loopback sink with pinned `SO_RCVBUF`, drives
  `export()` directly. Publishes `real_telemetry_backend: false`.
- **Tier B** is now the `lambda_python` environment -- a real function and layer per ref, driven by
  `lambda:Invoke`. No API Gateway, no public endpoint.

The planned `{lite, full} x {baseline, head}` matrix was **not** fully built: `local_process` covers
both lite and full mode, but `lambda_python` deploys lite mode only (`lite_mode=True` is hardcoded in
its `setup`), so two deployments per run rather than four. Full mode on real Lambda is unmeasured.

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

## Actual layout

The layout planned here originally put everything behind per-change "probes"; section F explains why
that boundary moved. What exists now:

```
benchmarks/README.md                 # how to run it
benchmarks/DESIGN.md                 # architecture and dataflow diagrams
benchmarks/INTEGRATION.md            # handing it to another agent
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
lambda-layer/sample-apps/function/benchmark_handler.py   # existing handler untouched
```

---

## A. Structural gaps (predicted before implementation)

Most are now resolved -- see the resolution list in section F. Found by evaluating the pre-build design
against
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

## B. Assumptions, and what testing did to them

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

## C. Environment and access gaps (C3 decided; C1 still open)

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

## E. Findings from running it (historical names: Tier A = local_process)

Discovered by executing the harness, not by designing it. Both would have produced confidently
wrong numbers.

**E1. macOS cannot measure this at all.** `net.inet.udp.maxdgram` defaults to **9216** bytes, so
every datagram the splitter produces fails with `EMSGSIZE` regardless of the code under test.
The first run reported `ok` with 0% delivery on *both* refs — a plausible-looking result that was
purely the host. `environments/local_process/host.py` binary-searches the real limit, publishes it as
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

## F. Restructure: environments vs. instruments

The original split was plan-vs-probe, which bundled a reusable deployment harness together with a
throwaway datagram counter. The boundary that matters is **infrastructure vs. instrumentation**:

- **Persisted** (`benchmarks/environments/`) — where a benchmark runs. A customer provisions one once
  and reuses it across many PRs: `local_process` (loopback sink, resource sampling, host datagram
  limit) and `lambda_python` (layer build, deploy, invoke, logs, RED, teardown). Change-agnostic.
- **Generated and discarded** (`results/<runid>/instrument.py`) — what one diff does. Declares
  `REQUIRES`/`SUPPORTS`, implements `workload`/`drive`/`observe`. Never committed, never reused.

Two mechanisms keep the knowledge when the instrument is thrown away:

**Capabilities.** Environments publish what they can represent (`udp_datagram_bytes`,
`real_telemetry_backend`, ...) and refuse an instrument whose `REQUIRES` they cannot meet. The macOS
9216-byte lesson from E1 now lives in `local_process` rather than in change-specific code, so a
regenerated instrument cannot lose it.

**Conformance** (`cli conformance`). Measures one commit twice and requires every verdict to be
neutral. A generated instrument is assumed wrong until it passes. What it caught immediately:

- `delivery_rate_pct` swung **9.2 to 4.8 on identical code** — confirming E2 empirically. The earlier
  Tier A report gave this metric an "improved" verdict it never deserved; it is now informational.
- `export_latency_p99_ms`, `cpu_total_s` and `peak_rss_bytes` all exceeded the 5% threshold run to
  run at these repeat counts, so they too carry no verdict.
- A flaw in the gate itself: `both-failed` on identical refs is *consistent*, not noisy, and must
  pass. Conformance now requires category *consistency*, not comparability.

After demoting those four metrics the post-refactor report shows **0 metric regressions** and claims
verdicts only for `export_failures`, `message_too_long` and `dropped_spans` — the things actually
measured rather than the things that happened to move.
## G. Current state

Everything below is verified by having been run, not by inspection. Reference run: PR #1,
`59a7f29` -> `bcf1bfd`.

### Verified

| What | Evidence |
|---|---|
| Before/after measurement | One worktree and one `exec` per ref; `baseline.jsonl` and `head.jsonl` both populated |
| `local_process` environment | 11 scenarios in a `python:3.12-slim` container: 8 newly-working, 2 comparable, 1 failed on both, 0 metric regressions |
| `lambda_python` environment | Three real functions created and destroyed (CloudTrail `CreateFunction`/`DeleteFunction` at 16:49, 16:53, 16:58 on 2026-10-20). Baseline logged 22 `EMSGSIZE`, head none, at ~18% throughput and ~13% p99 |
| Conformance gate | PASS on all 11 scenarios after forcing four metrics to informational |
| Branch coverage | 69.7% of `_udp_span_payload.py`, 75% of `otlp_udp_exporter.py`, 38.9% of `opentelemetry_lite_sdk.py` |
| Teardown | `find_orphans()` returns empty after every run |
| Comparison logic | 15 unit tests |
| Publishing | PR #1 comment `6307433353` |

### Resolved from section A

A1 (metric specs live in the plan), A2 (branch coverage makes it measurable), A3 (the boundary moved
and was validated -- adding `lambda_python` needed no edit to `runner/` beyond the contract change),
A4 (`baseRefOid`, not branch names), A5 (API Gateway dropped; throughput from `lambda:Invoke`),
A8 (crash/failure is a reported category), A11 (categories before arithmetic), C3 (`AWS_IAM` decided
if HTTP throughput is ever added).

### Open

**G1. `lambda_python` has no conformance run behind it.** Its throughput and p99 deltas are therefore
directional only -- nothing has shown them to exceed run-to-run noise, and `local_process` taught us
that four metrics there looked meaningful and were not. This is the most misleading gap on the list,
because the Lambda numbers are the ones most likely to be quoted. Fixing it means tolerating two more
deployments per conformance run.

**G2. Full mode is unmeasured on real Lambda.** `lambda_python.setup` hardcodes `lite_mode=True`
(`__init__.py:50`), so only the lite path is exercised there. `local_process` covers both. The planned
`{lite, full} x {baseline, head}` matrix is therefore half-built.

**G3. No example instrument ships.** Generated instruments are gitignored by design, so a fresh agent
has `templates/instrument_template.py` and the SKILL rules but nothing concrete to pattern-match.
Either commit one as a documented example or have the skill generate-and-conformance-check on first
run.

**G4. No preflight command.** A missing prerequisite surfaces mid-run rather than up front. The ECR
Public token expiring after ~12h is the likeliest; `layer.py` prints the remedy in its error, so it is
self-diagnosing but not early.

**G5. EC2 runtime metrics unbuilt.** `OTEL_AWS_APPLICATION_SIGNALS_RUNTIME_ENABLED` metrics
(`process.runtime.*.memory`, `*.gc_count`, `*.thread_count`, `system.network.connections`) remain
unexercised. Meaningless on Lambda; needs an EC2 environment.

**G6. HTTP-level throughput.** Deliberately dropped with API Gateway. Not a defect, recorded so it is
not mistaken for one.

**G7. The abstraction has only been tested on one change.** A6, A7, A9 and A10 stay open until an instrument and environment pair
are built for something genuinely different -- the Java/EC2 heap fix in
[aws-otel-java-instrumentation#1468](https://github.com/aws-observability/aws-otel-java-instrumentation/pull/1468)
is the intended second case.
