# ads2026 — word-count service

Architecture and interfaces: `docs/architecture.md`.

## Setup

Client runs on the host in a Python 3.12 env with `client/requirements.txt`; everything else is Docker.

`.env` (copy to `project/.env`):

```
SERVER_PORT=18861
MINIO_ROOT_USER=ads
MINIO_ROOT_PASSWORD=passads42
MINIO_BUCKET=texts
```

```
make up                 # scenario 1: redis, minio, server-1   (make up CACHE=off for the no-cache baseline)
make seed               # upload texts/gutenberg/ into MinIO (once; the volume persists)
make query K=the REF=mansfield-park
make logs SVC=server-1
make cache-flush
make down
```

MinIO console: http://localhost:19001

## Corpus

100 Project Gutenberg novels, 50k–200k words each, in `texts/gutenberg/` (`manifest.tsv` lists id, reference, word count).
Re-fetch with `python texts/fetch_gutenberg.py 100`.

## Experiment (Phase 2)

```
python -m client.loadgen --rate 70 --duration 20 --warmup 5 --label cache-on --out results/x.csv
experiments/sweep.sh results/phase2      # cache off/on x 50 70 90 110 130 req/s, cold cache each run
python -m client.plot results/phase2     # avg.png, p99.png, *-log.png, summary.tsv
```

`loadgen` is open-loop: every 1/rate seconds a new independent user connects, sends one `get_count`, disconnects.
CSV columns: `conn_ms` (connect), `rpc_ms` (call), `total_ms` = both. Figures plot `total_ms`.
For Phase 3 point it at the balancer with `--host/--port`.

## Where to work

- Phase 2 (service, cache, experiment): `server/`, `client/`, `compose.yml`, `results/phase2/`
- Phase 3 (load balancer): `lb/` (Dockerfile + proxy), `compose.cluster.yml` — `make up-cluster`
- Phase 4 (health checks, failover): `lb/`, `make replica-stop N=` / `make replica-start N=`
