# Phase 4 contribution for the group report

Status: implementation text below is ready for editing. The controlled Docker
matrix and six terminal screenshots must still be collected by the operator.
Do not represent the preliminary manual observation as a controlled experiment.
Integrate this into the shared IEEE two-column report; its three-page text limit
covers all phases together. Replace the measured-results instructions with a
compact paragraph and selected evidence before submission.

Manual FT-on demos for LC and LRT have now been completed. Their operator-pasted
terminal transcripts are in `results/phase4/manual-demo-2026-10-06/`. Both showed
six correct queries after exclusion and reuse of the recovered replica; the LRT
demo showed the returning replica receive one new query before the next five
went to server-2. These observations establish the demonstrated behavior, not
detection-time measurements or the results of the full comparison matrix.

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

## Experimental method and results to insert

Compare LC and LRT with fault tolerance disabled/enabled, using three repetitions
per condition (12 runs). Warm the shared cache for `the / mansfield-park`, then
issue 20 queries/s through the existing client API. Each run includes 20 s normal
operation, 20 s replica outage and 20 s recovery. Select the most-used replica
in the last five seconds of the normal stage, resolving ties by name; stop it
with SIGKILL and subsequently restart it. Recreate replicas and balancer before
each run while retaining the book volume.

**After running:** report per-condition failures during normal, fault and recovery
stages; include transition failures. State the targets and repetitions, observed
detection/recovery delays, any stable-down failures, and successful queries on
the returned replica. Use the recorded `summary.csv`, `phases.csv` and timelines.
Explain any `needs-review` outcome instead of omitting it. The controller samples
nominally once per second; observed delays are measured from control-command
start and include polling and command-duration uncertainty. Report actual sample
gaps when material. LRT is not expected to divide traffic equally. Select six
genuine terminal screenshots across LC/LRT: baseline failure, fault-tolerant
operation during failure, and recovery.

## Scope and limits

The health path demonstrates process responsiveness and RPC-listener readiness,
not correctness of every word-count operation or availability of Redis/MinIO.
A timeout can also reflect delay. Failure detection cannot prevent every
transition-time error or migrate an in-flight TCP session. Following the RPC
retry ambiguity discussed in slides 66–68, no application request is replayed
after forwarding and no exactly-once guarantee is claimed. The balancer, Redis
and MinIO remain outside the tested replica-failure model.
