# Recorded terminal evidence views

These six text files summarize recorded experiment results. Each identifies
its source run and UTC timestamps.

| Scenario | LC | LRT |
| --- | --- | --- |
| FT-off failure | [lc-off-failure.txt](lc-off-failure.txt), r1 | [lrt-off-failure.txt](lrt-off-failure.txt), r2 |
| FT-on failure | [lc-on-failure.txt](lc-on-failure.txt), r1 | [lrt-on-failure.txt](lrt-on-failure.txt), r1 |
| FT-on recovery | [lc-on-recovery.txt](lc-on-recovery.txt), r1 | [lrt-on-recovery.txt](lrt-on-recovery.txt), r1 |

The baseline view uses the first repetition containing an outage failure. LRT
off/r1 had no failures and remains in every aggregate and plot. The choice of
illustration does not exclude that run. See `selection.json` for the manifest.

To inspect an individual view from the repository root:

```bash
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lc --scenario off-failure --repetition 1
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lc --scenario on-failure --repetition 1
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lc --scenario on-recovery --repetition 1
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lrt --scenario off-failure --repetition 2
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lrt --scenario on-failure --repetition 1
.venv/bin/python experiments/phase4_evidence.py results/phase4/matrix-20261007 --policy lrt --scenario on-recovery --repetition 1
```

Terminal screenshots are pending. Keep the recorded-evidence label visible
when capturing these summaries. For live-demo screenshots, follow
`docs/phase4.md` and identify the demo in the image manifest. Save captures
under the session's `screenshots/` directory.
