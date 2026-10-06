# Live Phase 4 demonstrations — 7 October 2026

Terminal screenshots from live FT-on demonstrations. These demonstrations
supplement the [controlled experiment](../matrix-20261007/analysis.md).
FT-off comparisons are documented in the experiment's results and timelines.

| Algorithm | Scenario | Screenshot |
| --- | --- | --- |
| Least Connections | Replica failure | [lc-ft-on-failure.png](screenshots/lc-ft-on-failure.png) |
| Least Connections | Replica recovery | [lc-ft-on-recovery.png](screenshots/lc-ft-on-recovery.png) |
| Least Response Time | Replica failure | [lrt-ft-on-failure.png](screenshots/lrt-ft-on-failure.png) |
| Least Response Time | Replica recovery | [lrt-ft-on-recovery.png](screenshots/lrt-ft-on-recovery.png) |

Additional preparation evidence:
[LRT normal operation](screenshots/lrt-ft-on-normal.png).

## Least Connections: replica failure

Docker shows server-2 as `Exited (137)` while server-1 and server-3 are running.
The balancer reports Least Connections with fault tolerance enabled and marks
server-2 `unhealthy`. Six queries for `the / mansfield-park` return the expected
count of 6208. Cumulative connection attempts are 4, 0 and 3 for server-1,
server-2 and server-3 respectively; the failed replica has no user attempts.
The health history includes server-2's `healthy -> unhealthy` transition.

## Least Connections: replica recovery

Docker shows all three replicas running, including the restarted server-2.
The balancer reports all three as `healthy`, with a recorded server-2
`unhealthy -> healthy (ping/pong)` transition. Six queries again return 6208.
Cumulative attempts rise from 4/0/3 in the failure screenshot to 6/2/5;
each replica receives two additional attempts. Two server-2 log entries show
successful `get_count('the', 'mansfield-park') = 6208` responses after recovery.

## Least Response Time: normal operation

All three replicas are healthy with FT enabled. Six queries return 6208;
cumulative attempts increase from 0/0/0 to 4/1/1. Server-1 is selected as the
failure target because it received the most attempts in this preparation batch.
LRT does not require equal distribution among healthy replicas.

## Least Response Time: replica failure

Docker shows server-1 as `Exited (137)` while server-2 and server-3 are running.
The balancer marks server-1 `unhealthy`; its cumulative attempts remain at 4.
Six queries return 6208. Compared with the preparation screenshot, cumulative
attempts change from 4/1/1 to 4/1/7, showing all six additional attempts went
to healthy server-3. This unequal distribution is consistent with LRT selection.
The health history records server-1's `healthy -> unhealthy` transition.

## Least Response Time: replica recovery

Docker shows all three replicas running, including the restarted server-1.
The balancer reports all three as `healthy` and records server-1's
`unhealthy -> healthy (ping/pong)` transition. All six queries return 6208.
Cumulative attempts change from 4/1/7 to 5/1/12: the returned server-1 receives
one new attempt, while server-3 receives the other five. A new server-1
`get_count('the', 'mansfield-park') = 6208` log entry confirms it serves a query
after restarting. LRT readmits the replica without requiring equal traffic.

The four primary screenshots show successful queries after exclusion and
readmission for both algorithms. Detection time and transition-time error
rates are measured separately in the controlled
experiment. Screenshot filenames and SHA-256 checksums are listed in
[manifest.json](screenshots/manifest.json).
