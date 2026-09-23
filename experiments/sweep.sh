#!/usr/bin/env bash
# Phase 2 latency sweep: cache on/off x every rate, cold cache before each run.
# Usage: experiments/sweep.sh [out_dir]      (run from project/)
set -euo pipefail
OUT=${1:-results/phase2}
MODES=${MODES:-"off on"}
RATES=${RATES:-"50 70 90 110 130"}
DURATION=${DURATION:-20}; WARMUP=${WARMUP:-5}; MIX=${MIX:-uniform}
PY=$(conda info --base)/envs/ads_client/bin/python
mkdir -p "$OUT"
for mode in $MODES; do
  make up CACHE=$mode >/dev/null
  sleep 2
  for rate in $RATES; do
    $PY -m client.devcli cache-flush
    sleep 1
    $PY -m client.loadgen --rate $rate --duration $DURATION --warmup $WARMUP --mix $MIX \
        --label cache-$mode --out "$OUT/$mode-$rate.csv"
  done
done
