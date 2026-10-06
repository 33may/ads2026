# Phase 4 report contribution

[IEEE LaTeX section and standalone preview](phase4-report.tex).
The section below is the contribution of **ADS Group 44** for integration into
the group's combined report.
Detailed measurements are in [the experiment analysis](../results/phase4/matrix-20261007/analysis.md);
live figures and captions are in [the screenshot guide](../results/phase4/live-demo-2026-10-07/README.md).

## Load Balancing with Fault Tolerance

### Failure detection and recovery

Without health monitoring, the Phase 3 balancer could select a stopped replica,
causing `EOFError` during RPyC connection setup. We added bounded ping/echo
detection, following the course's treatment of replication, timeouts and
ping/echo (Fault Tolerance slides 22, 27–28).

Each replica process answers TCP `PING` with `PONG` once its RPC listener is
ready. Independent checks run every 1 s with a 0.5 s exchange timeout. Two
consecutive failures exclude a replica; two successes admit it. Unknown
replicas are ineligible, and excluded replicas remain under observation.
Least Connections (LC) and Least Response Time (LRT) select only healthy
replicas; probes do not affect their load measurements.

A failed backend connection also triggers immediate exclusion. Connection
attempts time out after 0.5 s; another healthy replica may be tried only before
application data is forwarded. Recovery preserves active counts and resets
the replica's LRT latency estimate, enabling a fresh sample.

### Evaluation

We ran LC/LRT with fault tolerance (FT) off/on, three repetitions each:
12 runs and 14,400 queries. Each run used fresh application containers, fixed
images and a warmed cache for `the / mansfield-park` (expected count 6208).
At 20 queries/s, runs comprised 20 s normal operation, 20 s outage and 20 s
recovery. We killed the replica most selected in the preceding five seconds,
then restarted it. The table includes all transition errors.

| Policy | FT | Failures r1 / r2 / r3 | Total / 3,600 |
| --- | --- | --- | ---: |
| LC | off | 139 / 139 / 1 | 279 |
| LC | on | 0 / 0 / 0 | 0 |
| LRT | off | 0 / 198 / 245 | 443 |
| LRT | on | 0 / 0 / 0 | 0 |

All 7,200 FT-on queries returned 6208; no unhealthy replica was selected.
Every recovered target served new queries. Five runs excluded the target on
a backend connection failure; one did so through periodic probes. All
readmissions used PING/PONG. Separate live Docker screenshots show failure
and recovery for both policies.

Baseline variation limits interpretation: a pending connection incidentally
steered LC away from its failed target in one run. In one LRT run, the preferred
replica changed before the fault; later runs reselected a stopped replica using
its stale latency estimate. All repetitions are retained.

### Scope

These results cover one replica failure under the stated workload. Health checks
indicate process responsiveness, not dependency correctness. Requests already
forwarded are not replayed, and transition errors remain possible. Balancer,
Redis and MinIO failures are outside scope.
