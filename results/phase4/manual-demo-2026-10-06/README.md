# Manual Phase 4 demonstrations — 6 October 2026

Selected terminal commands and output from the manual demonstrations recorded
on 6 October 2026. Computer names and shell prompts were removed; spacing was
normalized. Container log timestamps do not specify a timezone.

| Demonstration | Observed failure behavior | Observed recovery behavior |
| --- | --- | --- |
| LC, FT on, server-2 stopped | Six of six queries returned 6208. Attempts changed from 1/0/0 to 4/0/3; the stopped replica stayed at zero. | Before new queries, server-2 became healthy with eight consecutive successful probes. Six subsequent queries returned 6208; two new server-2 log entries confirm it handled queries again. |
| LRT, FT on, server-1 stopped | Six of six queries returned 6208. Attempts changed from 4/1/1 to 4/7/1; all six new queries went to server-2. | Before new queries, server-1 became healthy with seven consecutive successful probes. Six subsequent queries returned 6208; attempts changed from 4/7/1 to 5/12/1, and a new server-1 log entry confirms reuse. |

During the LRT recovery check, queries resumed after the target was observed
as `healthy`.

These demonstrations support healthy-only routing, recovery without user
traffic, and reuse of a recovered LRT replica. They do not measure detection
latency: command times and waiting intervals were not systematically recorded.
The reported 10.1 s `stop` duration is a Docker command duration, not the
balancer's detection time. Queries were issued after observing exclusion;
these transcripts therefore do not establish error-free behavior during the
failure transition or transparent migration of active sessions.

These two files document the FT-on manual demonstrations. The subsequent
12-run LC/LRT × FT off/on matrix and measured results are available in
[matrix-20261007](../matrix-20261007/analysis.md).
