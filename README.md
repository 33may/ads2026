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
make up            # scenario 1: redis, minio, server-1
make seed          # upload texts/ into MinIO
make query K=eindhoven REF=eindhoven
make logs SVC=server-1
make down
```

MinIO console: http://localhost:19001

## Where to work

- Phase 2 (service, cache, experiment): `server/`, `client/`, `compose.yml`, `results/phase2/`
- Phase 3 (load balancer): `lb/` (Dockerfile + proxy), `compose.cluster.yml` — `make up-cluster`
- Phase 4 (health checks, failover): `lb/`, `make replica-stop N=` / `make replica-start N=`
