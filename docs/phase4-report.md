# Phase 4 contribution for the group report

Status: implementation and measured-results draft for joint group review. The
12-run matrix is complete and reconciled; actual terminal screenshots remain
pending. Integrate and shorten this contribution within the shared IEEE
two-column report: the three-page text limit covers all phases together.
Detailed qualifications and traceable evidence are in
`results/phase4/matrix-20261007/analysis.md`. No publication is implied.

## Failure model and mechanism

The Phase 3 TCP balancer continued to select a stopped replica. In our
preliminary manual demonstration, stopping server-2 caused two of six queries
to fail with `EOFError: connection closed by peer` during RPyC connection
initialization. The balancer could not connect to the selected replica and
closed the client socket. Replication alone therefore did not prevent requests
from reaching unavailable replicas.

We added pull-based ping/echo detection with a bounded timeout, following the
course's discussion of physical redundancy, timeout-based suspicion and
ping/echo (Fault Tolerance slides 22, 27 and 28). Each replica exposes a small TCP
health endpoint in the application process. It answers `PING` with `PONG` only
after initialization and activation of the RPC listener. Independent balancer
tasks probe each replica every second, allowing 0.5 s for the entire exchange.
Two consecutive failures exclude a replica; two successes admit it. These
configurable values are experimental choices. Unknown replicas are ineligible,
and excluded replicas continue to be probed without user traffic.

LC and LRT select only healthy replicas. Health traffic does not enter their
connection or latency measurements. A failed backend connection immediately
excludes the replica and permits another healthy, untried replica to be tried
before any application bytes are forwarded. Established sessions remain pinned
and are never replayed. Health generations reject stale asynchronous results;
recovery preserves active-session counts and clears the returning replica's
LRT estimate so it can obtain a fresh sample.

## Experimental method and measured results

We compared LC/LRT with FT off/on over three repetitions per condition: 12 runs,
14,400 queries. Each run used fresh application containers and the same images,
with a warmed shared cache for `the / mansfield-park` (expected count 6208).
At 20 new client sessions/s, each run comprised 20 s normal operation, 20 s
replica outage and 20 s recovery. The most-selected replica in the preceding
five seconds was killed with SIGKILL and subsequently restarted. Ties were
resolved by name; book storage persisted.

| Algorithm | FT | Errors in r1 / r2 / r3 | Total errors / queries |
| --- | --- | --- | --- |
| LC | off | 139 / 139 / 1 | 279 / 3,600 |
| LC | on | 0 / 0 / 0 | 0 / 3,600 |
| LRT | off | 0 / 198 / 245 | 443 / 3,600 |
| LRT | on | 0 / 0 / 0 | 0 / 3,600 |

No normal-stage queries failed. LC/off produced 266 outage and 13 recovery
errors; LRT/off produced 408 outage and 35 recovery errors. All transition errors
are included. All six FT-on runs passed: 7,200 correct replies, no unhealthy
backend selections, no failures in the 2,303 observed stable-down requests,
and 752 correct replies on recovered targets.

Five FT-on runs excluded the replica on a failed backend connection and retried
one query successfully before forwarding; one LRT run detected failure via
periodic probes. First-observed exclusion delays were 0.333–0.369 s for the
connection-triggered cases and 2.994 s for the probe-triggered case. All returns
used PING/PONG; observed readmission took 3.016–3.041 s. These delays start at
the Docker command, include command/polling uncertainty, and are not detection
bounds. Polling was nominally 1 Hz; maximum gaps reached 1.879 s.

Baseline variation matters. LC/off r3 had one 4 s client timeout while a backend
connection stayed pending for 36 s; its active reservation incidentally steered
LC toward idle survivors. In LRT/off r1, the preferred backend changed before
SIGKILL, so the historical target rule did not fault the current winner.
LRT/off r2–r3 later reselected the stopped replica using its stale latency
estimate and failed. All repetitions are retained; these differences qualify
the comparison rather than establish reliability of the FT-off algorithms.

## Scope and limits

The health path demonstrates process responsiveness and RPC-listener readiness,
not correctness of every word-count operation or availability of Redis/MinIO.
A timeout can also reflect delay. Failure detection cannot prevent every
transition-time error or migrate an in-flight TCP session. Following the RPC
retry ambiguity discussed in slides 66–68, no application request is replayed
after forwarding and no exactly-once guarantee is claimed. The balancer, Redis
and MinIO remain outside the tested replica-failure model.
