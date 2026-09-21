# ADR-0001: HTTP control plane and TCP data plane in one process

**Status:** accepted (Phase 1)

## Context
We want a standard FastAPI service: OpenAPI docs, validation, health probes and
a REST API. But a key-value store's hot path is tiny requests where overhead is
the cost. HTTP/1.1 adds header parsing, routing and a JSON body envelope to
every request, and it gives no natural way to pipeline requests on one
connection.

## Decision
Each node runs **both** in one process, on one event loop:
- a raw asyncio **TCP server** (the data plane) for clients and router-to-shard traffic;
- a **FastAPI app** (the control plane) for REST access, `/health`, `/ready`,
  admin endpoints and cluster views.

The TCP server starts and stops inside FastAPI's `lifespan`, so there is one
lifecycle, one config and one engine. The same `/v1/keys` endpoints run on
shards and on the router, because they depend only on the abstract `KVService`
(a local engine or a routed one).

## Consequences
- ✅ Fast path stays lean, and the TCP protocol can move to RESP (Phase 2)
  without touching the API.
- ✅ HTTP gives easy debugging and integration: curl, browsers, the Swagger UI.
- ✅ Phase 3 benchmarks can compare TCP and HTTP on the same engine.
- ⚠️ Two ports per node to configure and expose.
- ⚠️ Heavy HTTP traffic shares the event loop with the data plane. That is
  acceptable for a control plane, and it is a reason to keep bulk traffic on TCP.
