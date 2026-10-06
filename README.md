# ads2026 — word-count service

Architecture and interfaces: `docs/architecture.md`.

Phase 4 replica failure detection, Docker demo, recorded experiments and group
handoff: [Phase 4 guide](docs/phase4.md). It is opt-in with `FT=on`; the default
`FT=off` keeps the Phase 3 comparison available.

## Setup

The client, server, load balancer, Redis, and MinIO are all declared as Compose
services. The experiment scripts run on the host as controllers so they can
restart containers and collect result files; the same `client` package is used
inside the `ads-client` container for normal queries.

`.env` (copy to `project/.env`):

```
SERVER_PORT=18861
MINIO_ROOT_USER=ads
MINIO_ROOT_PASSWORD=passads42
MINIO_BUCKET=texts
REDIS_MAX_CONNECTIONS=64
REDIS_POOL_TIMEOUT=5
```

Each server uses a bounded blocking Redis connection pool. During a short
burst, a request waits up to `REDIS_POOL_TIMEOUT` seconds for one of
`REDIS_MAX_CONNECTIONS` reusable connections instead of failing immediately
with `MaxConnectionsError`.

```
make up                 # scenario 1: redis, minio, server-1   (make up CACHE=off for the no-cache baseline)
make seed               # upload texts/gutenberg/ into MinIO (once; the volume persists)
make query K=the REF=mansfield-park
make logs SVC=server-1
make cache-flush
make down
```

## Load balancing (Phase 3)

### Why the load balancer is needed

A single RPyC server has finite CPU, storage-I/O, and concurrent-connection
capacity. As the offered request rate rises, work queues at that one process,
which increases latency and can cause timeouts. Replicating the service creates
additional capacity, but clients still need one stable endpoint and a dynamic
way to choose a replica. The load balancer provides that endpoint, spreads new
connections using current runtime measurements, and keeps the RPyC API
unchanged.

### Algorithms and implementation

The cluster uses **least connections** by default. Each client connection is
pinned to the replica with the fewest active connections; ties rotate fairly.
The count is incremented atomically when the asyncio accept handler assigns a
backend and decremented in `finally` when the proxied connection closes.

```sh
make up-cluster                              # least connections (default)
make up-cluster ALGORITHM=lrt                # least response time
make up-cluster ALGORITHM=least-response-time
make up-cluster ALGORITHM=combined           # EWMA response time x active load
```

Least response time uses an exponentially weighted moving average (EWMA) per
replica. A sample is the time from forwarding client bytes to receiving the
first response bytes. This measures current backend responsiveness without
parsing or changing the RPyC byte stream. Every replica receives an initial
bootstrap connection; subsequent connections select the lowest EWMA. Balancer
logs include the selected replica, active connections, and current
response-time values.
The combined policy scores each replica as `EWMA * (active connections + 1)`.
The balancer also exposes read-only JSON counters on localhost port 18860 for
the experiment runner; this does not alter the proxied RPyC stream.

The proxy can also be run directly with `python lb/load_balancer.py --algorithm lrt`.

Run the fast Phase 3 regression tests with `make test-phase3`. They verify the
default policy, LC load/tie behavior, LRT bootstrap/selection behavior, the
assignment's five-rate constraints, and byte-exact forwarding of a payload
larger than 64 KiB through real asyncio TCP sockets.

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

## Controlled Phase 3 experiment

The Phase 3 runner tests Least Connections and Least Response Time as required,
plus the optional combined policy, with both cache modes. Like Phase 2, every
cache mode is tested after 0, 100, and 500 successful warm-up requests, with
bounded retries. It flushes Redis and recreates all three servers and the
balancer before every repetition. Every
policy/cache/warm-up/rate combination runs at least three times and saves raw
CSVs, averaged per-condition reports, and a combined report.

```sh
./.venv/bin/python experiments/phase3.py
```

The default output is
`results/Phase2+3/controlled/warmup-0-100-500/phase3/`. The combined Phase 2+3
report is saved in the parent `warmup-0-100-500/` directory. Existing completed
CSVs are skipped so an interrupted sweep can resume; pass `--overwrite` to
replace them. Use `--overwrite` once after updating from an older CSV schema.

To replace only one result and then regenerate all summaries and figures, use
the singular filters. For example, rerun the three single-server repetitions at
70 requests/s with cache on and 100 successful warm-up requests:

```sh
./.venv/bin/python experiments/phase2.py \
  --cache-mode on --warmup-count 100 --rate 70 --overwrite
```

The equivalent targeted cluster run also selects a policy:

```sh
./.venv/bin/python experiments/phase3.py \
  --policy least-connections --cache-mode on \
  --warmup-count 100 --rate 70 --overwrite
```

Targeted runs overwrite only the selected CSV repetitions. They regenerate the
plots from all CSVs already in the results tree and leave the full-sweep
`experiment.json` unchanged.

To rebuild every combined Phase 2+3 table and figure from the CSVs without
running Docker or sending new requests, use:

```sh
make plot-results
```

This includes all `comparison-*-avg.png` and `comparison-*-p99.png` figures.
For a non-default results tree, pass it explicitly, for example:

```sh
make plot-results RESULTS_DIR=results/my-experiment
```
On a system where Docker requires sudo, the runner starts the Docker service and
prompts for sudo once before beginning the sweep.

Both runners also regenerate the total recursive report under
`results/Phase2+3/controlled/warmup-0-100-500/`. After Phase 2 and Phase 3 have
both completed, it contains all single-server and three-server results. For
each cache/warm-up condition, `comparison-*-avg.png` and
`comparison-*-p99.png` compare the same seeded workload on one server against
Least Connections, Least Response Time, and the optional combined policy.
Phase 3 also saves `evidence-ALGORITHM.log` samples from the balancer and all
three replicas. Each evidence file selects representative `get_count` and
balancer-assignment lines for every replica, avoiding the ambiguity of a short
tail of the logs.

## Where to work

- Phase 2 (service, cache, experiment): `server/`, `client/`, `compose.yml`, `results/phase2/`
- Phase 3 (load balancer): `lb/` (Dockerfile + proxy), `compose.cluster.yml` — `make up-cluster`
- Phase 4 (health checks, failover): `lb/`, `make replica-stop N=` / `make replica-start N=`
