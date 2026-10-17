# Recorded Phase 4 results

Recorded runs: 12 / 12. Only a complete matrix supports the planned comparison.

Generated from requests.csv and recorded control/health observations. Detection/recovery are first-observed delays from the corresponding command start; polling is nominally 1 Hz. Inspect metrics.jsonl for actual gaps and command times in events.jsonl. This is not millisecond-accurate failure detection latency.

All request errors, including transitions, are included. Latency includes connection setup, GETROOT, the query and connection cleanup. LRT's internal first-byte EWMA is a different metric.

| Run | Target | Requests | Errors | Detection (s) | Recovery (s) | Acceptance |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| least-connections-ft-off-r1 | server-2 | 1200 | 139 | n/a | n/a | baseline |
| least-connections-ft-off-r2 | server-2 | 1200 | 139 | n/a | n/a | baseline |
| least-connections-ft-off-r3 | server-3 | 1200 | 1 | n/a | n/a | baseline |
| least-connections-ft-on-r1 | server-1 | 1200 | 0 | 0.336 | 3.041 | pass |
| least-connections-ft-on-r2 | server-1 | 1200 | 0 | 0.335 | 3.020 | pass |
| least-connections-ft-on-r3 | server-1 | 1200 | 0 | 0.333 | 3.037 | pass |
| lrt-ft-off-r1 | server-2 | 1200 | 0 | n/a | n/a | baseline |
| lrt-ft-off-r2 | server-2 | 1200 | 198 | n/a | n/a | baseline |
| lrt-ft-off-r3 | server-2 | 1200 | 245 | n/a | n/a | baseline |
| lrt-ft-on-r1 | server-2 | 1200 | 0 | 0.336 | 3.030 | pass |
| lrt-ft-on-r2 | server-3 | 1200 | 0 | 0.369 | 3.023 | pass |
| lrt-ft-on-r3 | server-3 | 1200 | 0 | 2.994 | 3.016 | pass |

The stable-down interval starts at the first observed unhealthy state and ends at restart command start. Empty intervals never pass acceptance. Successful requests on the returned target are required, but equal LRT distribution is not required. Consult phases.csv, summary.csv and each timeline.png before drawing conclusions.
