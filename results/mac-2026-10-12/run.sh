#!/bin/sh
# Full Phase 2 + Phase 3 grid on Anton's Mac, latency = total_ms.
set -e
cd "$(dirname "$0")/../.."
PY=/opt/homebrew/Caskroom/miniconda/base/envs/ads_client/bin/python
export MPLCONFIGDIR=/tmp/matplotlib
OUT=results/mac-2026-10-12
date; $PY experiments/phase2.py --out $OUT/phase2
date; $PY experiments/phase3.py --out $OUT/phase3
date; echo ALL DONE
