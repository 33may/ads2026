# Manual Phase 4 demonstrations — 6 October 2026

Source: terminal output pasted by the operator into the project conversation.
The text files preserve selected commands and output, with shell prompts removed
and whitespace normalized. They are operator-provided transcripts, not direct
log exports, screenshots or output from the controlled experiment script.
The date is the conversation date; container log times have no timezone label.

| Demonstration | Observed failure behavior | Observed recovery behavior |
| --- | --- | --- |
| LC, FT on, server-2 stopped | Six of six queries returned 6208. Attempts changed from 1/0/0 to 4/0/3; the stopped replica stayed at zero. | Before new queries, server-2 became healthy with eight consecutive successful probes. Six subsequent queries returned 6208; two new server-2 log entries confirm it handled queries again. |
| LRT, FT on, server-1 stopped | Six of six queries returned 6208. Attempts changed from 4/1/1 to 4/7/1; all six new queries went to server-2. | Before new queries, server-1 became healthy with seven consecutive successful probes. Six subsequent queries returned 6208; attempts changed from 4/7/1 to 5/12/1, and a new server-1 log entry confirms reuse. |

The operator reported waiting manually instead of executing `sleep` during the
LRT recovery check. A shell sleep is only a convenience: the relevant condition
is observing `healthy` before the next batch of queries.

These demonstrations support healthy-only routing, recovery without user
traffic, and reuse of a recovered LRT replica. They do not measure detection
latency: command times and waiting intervals were not systematically recorded.
The reported 10.1 s `stop` duration is a Docker command duration, not the
balancer's detection time. Queries were issued after observing exclusion;
these transcripts therefore do not establish error-free behavior during the
failure transition or transparent migration of active sessions.

Still pending: the full 12-run LC/LRT × FT off/on matrix, genuine terminal
screenshots for all six planned scenarios, and measured results in the group
report. No images have been created from these transcripts or claimed as
screenshots. Earlier FT-off observations remain preliminary; these two files
specifically document the FT-on manual demonstrations.
