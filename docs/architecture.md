# System architecture

## Scope

Architecture of the word-count service across all four phases of the lab assignment. It fixes the components, their interfaces, the connectors, the repository layout and the deployment model, so that phases can be built by different people without rework. Phase 2 is built first; Phases 3 and 4 get their place in the structure and their external contract here, internals are decided by whoever implements them.

## System overview

Client-server style. A client sees one logical server at one hostname:port. Inside the logical server:

- **Word-count service** — RPyC server, owns all logic, exposes the client API and the developer API on one port. One instance in Phase 2 (`server-1`), three explicitly named replicas from Phase 3 (`server-1`, `server-2`, `server-3`).
- **Cache** — Redis, shared by all replicas. Cached counts and the hot-keyword counter.
- **File store** — MinIO, S3 API, single source of truth for texts. Shared by all replicas.
- **Load balancer** — from Phase 3. Byte-level TCP proxy in front of the
  replicas; the client's hostname:port points at it instead of `server-1`.

Outside the logical server:

- **Client** — the `ads-client` Compose container uses one Python package for
  end-user queries and developer operations. The host-side experiment
  controller reuses that package to drive Docker and save measurements.

Phase 2 diagram: `diagrams/phase1-mermaid.png`. Phase 3 diagram:
`diagrams/phase3.svg`, adding the TCP load balancer and two replicas; connectors
C1–C4 retain the same semantics.

## Components and interfaces

**Word-count service** (`server/`, image `ads-server`, Python 3.12, RPyC)

Client API
- `get_count(keyword, reference) -> int | Error(no such reference)`
- `list_references(name_query?) -> list[str]`

Developer API, same RPyC service and port
- `upload_text(reference, text)`
- `get_text(reference) -> str`
- `list_references(name_query?) -> list[str]`
- `hot_keywords(n) -> list[(keyword, count)]`

Behaviour of `get_count`: bump `hot_keywords[keyword]`; look up `(reference, keyword)` in the cache, hit → return; miss → fetch text from the file store, 404 → error; count, store in cache, return. Every request is logged with the replica's name, the call, and hit/miss state, so a replica identifies itself in `docker compose logs`.

**Cache** (stock `redis` image). Keys: `count:<reference>:<keyword> -> int`; sorted set `hot_keywords`, member keyword, score request count. `hot_keywords(n)` = `ZREVRANGE hot_keywords 0 n-1 WITHSCORES`.

**File store** (community MinIO image, S3 API, bucket `texts`). Object key = reference, body = text. Mapping: `upload_text` → `PutObject`, `get_text` → `GetObject` (`NoSuchKey` → error), `list_references` → `ListObjectsV2` with `name_query` as prefix filter. Credentials from `.env`. Only the service talks to it.

**Load balancer** (`lb/`, image `ads-lb`, Phase 3). Contract fixed here, internals by the implementer:
- Listens on one TCP port, forwards bytes both ways to one of `server-1..3` without interpreting RPyC.
- Phase 3: three dynamic algorithms, selectable by name: Least Connections and Least Response Time and Combined.
- Phase 4 (opt-in): each replica exposes an internal TCP PING/PONG health
  endpoint in its application process. Independent monitors restrict all three
  policies to healthy replicas and detect recovery. Before forwarding any user
  bytes, a failed backend connection can retry another healthy replica. See
  [Phase 4 design and experiment guide](phase4.md) for thresholds and limits.
- Exposes the active algorithm, cumulative counters, and per-selection active
  connection snapshots through a separate read-only metrics socket.
- Phase 3 experiment connections begin with a reserved correlation-ID
  preamble. The balancer records and removes this instrumentation before
  forwarding; ordinary client connections remain byte-for-byte transparent,
  and the balancer never parses RPyC.

**Client** (`client/`, image `ads-client`, one package)
- `api.py` — the only place RPyC is imported; `connect(host, port)` returns the service proxy.
- `cli.py` — one-shot end-user commands: ping, query, and list.
- `devcli.py` — developer commands: seed, get, delete, hot, and cache flush.
- `loadgen.py` — open-loop generator at a fixed rate and one fresh connection
  per request; one CSV row per request includes schedule/completion timestamps,
  latency, workload, outcome, selected replica, and assignment-time load.
- `plot.py` — average and p99 latency vs rate; Phase 3 overlays 1-server and 3-server runs per algorithm.

## Connectors

| | From → To | Mechanism | Protocol | Phase |
|---|---|---|---|---|
| C1 | Client → Word-count service | RPC (RPyC), mandated | RPyC over TCP | 2 |
| C2 | Word-count service → Cache | request-reply | RESP over TCP | 2 |
| C3 | Word-count service → File store | request-reply | S3 API, HTTP over TCP | 2 |
| C4 | Developer client → Word-count service | RPC (RPyC), same port as C1 | RPyC over TCP | 2 |
| C5 | Client → Load balancer → replica | byte stream, transparent to C1 | TCP | 3 |

From Phase 3 the client's C1 endpoint is the load balancer; the balancer splices the client's TCP connection to a replica's, so C1 semantics are unchanged and the client code does not know whether a balancer is present.

All application connectors are synchronous request-reply: each request is a chain of data dependencies (cache → fetch? → count). Concurrency comes from the RPyC server handling connections in parallel and, in Phase 3, from the replicas.

**C1 connection model:** every measured request uses one fresh TCP/RPyC
connection. This gives the byte-stream balancer one scheduling decision per
keyword request.

## Repository structure

Everything lives under `project/` in the course repo (`ads2026`). Each phase's deliverable zip is built from it.

```
project/
  compose.yml          scenario 1: redis, minio, server-1
  compose.cluster.yml  scenario 2: redis, minio, server-1..3, lb (Phase 3)
  .env                 ports, MinIO credentials, defaults
  Makefile             every operator and demo command (see Operations)
  server/              image ads-server
    Dockerfile
    requirements.txt   rpyc, redis, boto3
    service.py         RPyC service, client + developer API, request logging
    cache.py           Redis access
    store.py           MinIO access
  lb/                  image ads-lb, Phase 3 — contents by the implementer
    Dockerfile
  client/              image ads-client and shared Python package
    Dockerfile
    requirements.txt   rpyc, matplotlib
    api.py             connect(host, port)
    cli.py             one-shot commands
    loadgen.py         open-loop load generator → CSV
    plot.py            figures from CSVs
  texts/               Gutenberg .txt files, seeded through upload_text
  results/
    phase2/            rate-<r>.csv, avg.png, p99.png
    Phase2+3/controlled/
      phase2/          repeated single-server raw runs and reports
      phase3/          repeated cluster raw runs and reports
      {avg,p99}*.png   combined Phase 2+3 figures
      summary.tsv      combined Phase 2+3 statistics
```

`results/` is committed so figures in the report are reproducible from the CSVs.

## Deployment

Two deployment scenarios, each a self-contained Compose file. They are never combined: `make down` tears down whichever is running before the other is started, so every run is a fresh start. Services are explicitly named containers; Compose `replicas` is not used.

| Service | Image | Scenario 1 `compose.yml` | Scenario 2 `compose.cluster.yml` | Host port |
|---|---|---|---|---|
| `client` | `ads-client` (built from `client/`) | ✓ | ✓ | none |
| `redis` | `redis` (stock) | ✓ | ✓ | none |
| `minio` | `ghcr.io/coollabsio/minio` | ✓ | ✓ | 9001 (console); S3 API 9000 internal |
| `server-1` | `ads-server` (built from `server/`) | ✓, published | ✓, internal only | 18861 (scenario 1) |
| `server-2`, `server-3` | `ads-server` | – | ✓, internal only | none |
| `lb` | `ads-lb` (built from `lb/`) | – | ✓ | 18861 (scenario 2) |

Inside Compose the client targets `server-1:18861` in scenario 1 and
`lb:18861` in scenario 2. The external experiment controller uses the
published `localhost:18861`; in either case the client API is unchanged.

`redis`, `minio` and the server definition are duplicated between the two files rather than shared through `extends`, so each file reads top to bottom as one complete deployment. Each server container receives its own name in `SERVER_NAME` and prints it in every request log line. Ports, MinIO credentials and the default balancing algorithm come from `.env`. The bucket `texts` is created when `minio` starts.

## Operations and demo

Everything an operator or a debrief needs is a `make` target, run from `project/`. The client targets are thin wrappers around the `client/` package.

Cluster
- `make up` / `make up-cluster` — start scenario 1 / scenario 2 (builds images if needed)
- `make down` — stop and remove whichever scenario is running
- `make ps` — running containers
- `make logs SVC=server-1` — follow one service; every request line names the server that handled it
- `make replica-stop N=2` / `make replica-start N=2` — Phase 4 failure and recovery, scenario 2 only

Data
- `make seed` — upload every file in `texts/` through `upload_text`
- `make list Q=` / `make get REF=` — verify the seed
- `make hot N=10` — hot keywords

Queries and experiment
- `make query K= REF=` — one `get_count`, prints count and client-side latency
- Load generation, plotting and Phase 3 balancer commands: defined once the experiments start.

Demo scenarios
- S1 cold vs warm — `make query` twice on the same pair; second one is a cache hit and visibly faster in the log line
- S2 latency sweep — a load run at five rates, then the two figures
- S3 load distribution (Phase 3) — a load run under scenario 2, `make logs` shows requests spread over `server-1..3`
- S4 hot keywords — after a load run, `make hot`
