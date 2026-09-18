# Phase 1 — Architecture diagram (text source of truth)

Style: client-server. Every box is a component (well-defined interface), every
line is a connector (labelled with mechanism + protocol). Vocabulary from
ADS-ArchtecturalStyles.pdf slides 24–27.

## Boxes

### Client  (outside the server boundary)
- Role: end-user program; sends keyword queries.
- Provides: nothing.
- Requires: Client API of the Word-count service.
- Rule: talks to the service only — never to Redis or the file store.

### Developer client  (outside the server boundary)
- Role: developer/operator tool; uploads texts, inspects usage.
- Provides: nothing.
- Requires: Developer API of the Word-count service.
- Same rule: service only.

### Boundary: Logical server  (one hostname:port from the client's view)
Contains the three components below. Phase 3 adds a load balancer + 2 more
service replicas inside this boundary; the boundary itself does not change.

#### Word-count service  (Python process, RPyC server)
- Role: counts keyword occurrences; owns all business logic.
- Provides — Client API (RPyC):
  - `get_count(keyword, reference) -> int | Error(no such reference)`
  - `list_references(name_query?) -> list[str]`
- Provides — Developer API (same RPyC service, same port):
  - `upload_text(reference, text)`
  - `list_references(name_query?) -> list[str]`
  - `get_text(reference) -> str`
  - `hot_keywords(n) -> list[(keyword, count)]`
- Requires: Redis (cache), File store (texts).
- Instances: 1 in Phase 2; 3 replicas in Phase 3.

#### Cache  (Redis container)
- Role: in-memory store shared by all service instances.
- Data:
  - `(reference, keyword) -> count`  — cached results
  - sorted set `hot_keywords`: `keyword -> request count`
- Provides: Redis commands (GET/SET, ZINCRBY/ZREVRANGE).
- Requires: nothing.

#### File store  (S3-like object store container)
- Role: single source of truth for texts.
- Provides (HTTP):
  - `GET /texts/<ref>` -> text body | 404
  - `PUT /texts/<ref>`  (body = text)
  - `GET /texts?q=<name_query>` -> list of refs
- Requires: nothing.

## Connectors

| # | From → To | Mechanism | Protocol / transport | Pattern |
|---|---|---|---|---|
| C1 | Client → Word-count service | RPC (RPyC) | RPyC over TCP | synchronous request-reply |
| C2 | Word-count service → Cache | request-reply | RESP over TCP | synchronous |
| C3 | Word-count service → File store | request-reply | HTTP/1.1 over TCP | synchronous, GET-by-key / 404 |
| C4 | Developer client → Word-count service | RPC (RPyC) | RPyC over TCP, same port as C1 | synchronous request-reply |

C1 is mandated by the assignment. C2 and C3 are internal to the logical server.
All connectors synchronous by design: each request is a chain of data
dependencies (cache answer → fetch? → count), so async buys nothing per request;
concurrency across clients comes from the RPyC server handling connections in
parallel. Hot-keyword bump could be fire-and-forget; kept synchronous for
simplicity (sub-ms). Asynchrony is discussed in the pub-sub trade-off (part 3).

## Behaviour on the diagram (one annotated flow)

`get_count(k, ref)`:
1. C2: `ZINCRBY hot_keywords 1 k`  (always)
2. C2: `GET (ref, k)` → hit: return count
3. miss → C3: `GET /texts/<ref>` → 404: return Error(no such reference)
4. count occurrences of k in body
5. C2: `SET (ref, k) = count`; return count

## Primitive → store call map (developer API)

| Service primitive | Backing call |
|---|---|
| `upload_text(ref, text)` | C3 `PUT /texts/<ref>` |
| `get_text(ref)` | C3 `GET /texts/<ref>` |
| `list_references(q)` | C3 `GET /texts?q=` |
| `hot_keywords(n)` | C2 `ZREVRANGE hot_keywords 0 n-1 WITHSCORES` |

## Not yet decided
- Further observability primitives (latency, health) — after building.
- (decided 17 Sep) Diagram language: **Mermaid**, source `docs/diagrams/phase1.mmd`.
  Render: `mmdc -i phase1.mmd -o phase1-mermaid.png -s 2 -b white --iconPacks @iconify-json/lucide`
  Style follows the course slides (26, 45, 63): box = name + one-line role,
  edge = connector id + mechanism, API signatures live in this file / the
  report table, not on the picture. Clients drawn as Lucide icon nodes.
