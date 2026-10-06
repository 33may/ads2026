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

Actual terminal screenshots are still pending. Text records and charts have
not been represented as screenshots. No new remote push or PR was made for
this recorded session; evidence and report integration await joint review.

`practice-20261007/` is the separate, successful LC/FT-on trial used to check the
runner before the matrix. Its 1,200 queries are excluded from matrix totals.

The operator completed manual FT-on failure/recovery demos for LC and LRT.
Selected pasted terminal transcripts and their interpretation are preserved in
[manual-demo-2026-10-06](manual-demo-2026-10-06/README.md). These are textual
evidence, not terminal screenshots or controlled timing measurements.

Run `make experiment-phase4` to create a new timestamped session with
raw CSV/JSON, Docker evidence, settings, source provenance, summary tables and
timelines. See `docs/phase4.md` for the protocol and six terminal screenshots.
Keep practice runs clearly identified and use a full 12-run session for the report.
Do not copy or overwrite Phase 2–3 results here.
