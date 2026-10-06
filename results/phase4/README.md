# Phase 4 evidence

The complete controlled matrix is in
[matrix-20261007](matrix-20261007/analysis.md): 12 runs, 14,400 queries, all raw
request/control/health records and per-run timelines. All six FT-on runs passed
the declared acceptance checks with 7,200 correct replies and no errors.
Read the baseline qualifications before interpreting the aggregate totals.

- [Analysis and limitations](matrix-20261007/analysis.md)
- [All-repetition comparison](matrix-20261007/comparison.png)
- [Per-run summary](matrix-20261007/summary.csv)
- [Independent reconciliation](matrix-20261007/validation.json)
- [Six recorded terminal text views](matrix-20261007/terminal-evidence/README.md)

Terminal screenshots and integration into the group report are pending.

`practice-20261007/` is the separate, successful LC/FT-on trial used to check the
runner before the matrix. Its 1,200 queries are excluded from matrix totals.

Manual FT-on failure/recovery demos for LC and LRT are documented in
[manual-demo-2026-10-06](manual-demo-2026-10-06/README.md). The selected terminal
transcripts demonstrate routing and recovery; timing measurements come from
the controlled matrix.

Run `make experiment-phase4` to create a new timestamped session with
raw CSV/JSON, Docker evidence, settings, source provenance, summary tables and
timelines. See `docs/phase4.md` for the protocol and six terminal screenshots.
Keep practice runs clearly identified and use a full 12-run session for the report.
Do not copy or overwrite Phase 2–3 results here.
