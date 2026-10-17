# Recorded Phase 4 results

Recorded runs: 1 / 1. Only a complete matrix supports the planned comparison.

Generated from requests.csv and recorded control/health observations. Detection/recovery are first-observed delays from the corresponding command start; polling is nominally 1 Hz. Inspect metrics.jsonl for actual gaps and command times in events.jsonl. This is not millisecond-accurate failure detection latency.

All request errors, including transitions, are included. Latency includes connection setup, GETROOT and the query. LRT's internal first-byte EWMA is a different metric.

| Run | Target | Requests | Errors | Detection (s) | Recovery (s) | Acceptance |
| --- | --- | ---: | ---: | ---: | ---: | --- |
| least-connections-ft-on-r1 | server-3 | 1200 | 0 | 0.998 | 3.036 | pass |

The stable-down interval starts at the first observed unhealthy state and ends at restart command start. Empty intervals never pass acceptance. Successful requests on the returned target are required, but equal LRT distribution is not required. Consult phases.csv, summary.csv and each timeline.png before drawing conclusions.
