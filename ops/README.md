# AVA local observability platform (B-022)

CPU-only Docker Compose project for AVA's local operational dependencies,
separate from the GPU analytics worker and the RTSP replay harness
(ADR-15). Services: **PostgreSQL** (events), **Prometheus** (metrics), **Grafana** (dashboards),
**Loki** (logs), **Alloy** (Docker log discovery/forwarding).

## Lifecycle

```bash
cp ops/.env.example ops/.env        # set GRAFANA_ADMIN_PASSWORD and POSTGRES_PASSWORD
docker compose -f ops/docker-compose.yml up -d
docker compose -f ops/docker-compose.yml ps          # health status
docker compose -f ops/docker-compose.yml logs -f alloy   # collection debug
docker compose -f ops/docker-compose.yml down         # stop (volumes persist)
```

State persists in named volumes (`ops-postgres`, `ops-prometheus`, `ops-loki`, `ops-grafana`).
`down` without `-v` keeps data; restarts reload it.

## Ports (all bound to 127.0.0.1 only)

| Service    | Host port | Purpose                          |
|------------|-----------|----------------------------------|
| PostgreSQL | 5432      | Event persistence                |
| Grafana    | 3000      | Dashboards, log search           |
| Prometheus | 9090      | Metrics queries, target health   |
| Loki       | 3100      | Log API (used by Grafana/Alloy)  |

Service-to-service traffic uses the `ava-ops` project network; no service is
reachable from the LAN.
The optional worker Compose override joins that network for PostgreSQL access;
see the root README for its invocation.

## Local identity provider

Keycloak and its dedicated PostgreSQL server form the separate `ava-identity`
Compose project in `ops/keycloak-compose.yml`. The identity database is separate
from the `ava-ops` PostgreSQL server used for events. Starting identity starts
both Keycloak and its database; the replay harness and observability stack have
independent lifecycles. Keycloak is exposed on `127.0.0.1:18080`; the identity
PostgreSQL server has no host port.

Create `ops/.env` from the example only if it does not already exist, and set
`KEYCLOAK_ADMIN_PASSWORD` and `KEYCLOAK_DB_PASSWORD` to local values. Then use:

```bash
docker compose --env-file ops/.env -f ops/keycloak-compose.yml up -d
docker compose --env-file ops/.env -f ops/keycloak-compose.yml ps
docker compose --env-file ops/.env -f ops/keycloak-compose.yml down
```

`down` preserves the named identity volumes. Do not use `down -v` unless the
identity data is intentionally being discarded. PostgreSQL stores realm, user
and configuration state; include it in any backup/restore plan. The imported
realm is a development fixture with hardcoded tenant, roles and credentials,
not a deployment authorization configuration. Startup import skips a realm
that already exists, so editing the import file does not update an existing
realm. Verify fresh imports using an isolated project and volumes.

Configure AVA with the exact issuer in the realm's OIDC discovery document,
the associated JWKS endpoint, and audience `ava-api`. The issuer advertised by
Keycloak must be reachable and match the token issuer from the API's network
location; container `localhost` refers to that container, not the host. Verify
host and container routing and the advertised URL for each deployment. The
Compose service's current healthcheck checks Java availability, not Keycloak
readiness; confirm the discovery endpoint and token flow separately.

The separate database keeps the local identity stack self-contained. A later
deployment may use the same PostgreSQL server as AVA events with a dedicated
Keycloak database and account, after agreeing on isolation, credentials,
backups, recovery and migration. No migration or shared-server configuration
is implemented here. The current Keycloak `start-dev` setup is for local use;
a networked deployment requires an appropriate production configuration and
TLS.

## AVA scrape target

The live analytics worker runs on the host and exposes `/metrics` when
`live.telemetry.prometheus_port` is set in the run config (B-021). Prometheus
reaches it through `host.docker.internal` (compose maps it to
`host-gateway`) at port 9464 — the canonical AVA metrics port, kept in sync
with `live.telemetry.prometheus_port`. If you change that value, update the
`ava` job target in `ops/prometheus/prometheus.yml` too.

To produce scrape evidence:

```bash
# host: start the harness + a live run with telemetry enabled
docker compose -f harness/docker-compose.yml up -d
# add to the live run config:
#   live:
#     telemetry:
#       prometheus_port: 9464
ava live --config configs/run-e3-live.yaml --gpu 0
```

Then check `http://127.0.0.1:9090/targets` for the `ava` job and open the
provisioned **AVA Live Analytics** dashboard in Grafana.

## Log pipeline

Alloy tails logs of containers whose names match `^ava-.*` via the Docker
socket (mounted **read-only**) and pushes them to Loki with bounded labels
(`job=ava`, `service=ava`, plus container/image identity from the source).
Structured JSON fields emitted by AVA (`run_id`, `camera_id`, `event_id`, ...)
remain searchable in log content; they are never used as Loki labels.

### Docker socket risk (ADR-15)

Read-only socket access still allows listing containers and reading their
logs — treat Alloy as a privileged component of the study stack. It runs only
on this development host, binds no public ports, and is not a production
security template.

## Pinned images

| Service    | Version | Digest (sha256)                                                        |
|------------|---------|------------------------------------------------------------------------|
| Prometheus | 3.15.1  | `fa3302279710be2f21a8301ec17a8b4f2301fd829bd113839b3b6b02b6f191a2`     |
| Grafana    | 13.2.4  | `dbbc38ea21103cefb50396340cd88afec779c7e070b0a7bf5a75b4ce46651d3f`     |
| Loki       | 3.7.9   | `0db216b630ae1f348a1584c67f3cead8b4c1fc839169a39501db664744158e28`     |
| Alloy      | 1.21.2  | `89a5a320168387336b9ac3c850609c3ba7757eadd4ae5f1d819deb35e9854b18`     |

Re-pin after an upstream update: pull the new image, verify it, replace the
`@sha256:` reference and this table.

## Measuring host overhead

```bash
# idle CPU/RAM of the four containers (repeat for a few minutes)
docker stats --no-stream $(docker compose -f ops/docker-compose.yml ps -q)
# disk growth of the state volumes
du -sh /var/lib/docker/volumes/ops-prometheus /var/lib/docker/volumes/ops-loki /var/lib/docker/volumes/ops-grafana
```

Record results in `docs/progress/YYYY-MM-DD.md`. Retention is 7 days for both
Prometheus TSDB and Loki (provisional; B-024 owns retention policy).

## Outage semantics

Loki, Grafana or Prometheus failure must not stop analytics: the runtime has
no dependency on any of them (B-021). Stop any subset with
`docker compose -f ops/docker-compose.yml stop loki grafana` and verify a live
run still completes.
