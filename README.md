# docker-fallback-orchestrator

A FastAPI controller that dynamically manages backup [PyCA](https://github.com/opencast/pyCA)
instances for Opencast capture agents. It watches the Opencast scheduler and
capture-admin APIs, and when a venue's hardware capture agent is offline (or
about to miss a scheduled recording), it spins up a disposable PyCA container
stack to record that venue's RTSP stream instead - then tears it down once
it's no longer needed.

Sibling project to [fallback-capture-agent](../fallback-capture-agent), which
solves the same problem by recording RTSP directly with ffmpeg. This project
takes the heavier but more compliant route of running a real PyCA agent per
venue, on demand.

## Status

Core pieces exist and are wired together:

- `app/opencast_client.py` - capture-admin + scheduler API client (digest auth)
- `app/decision_engine.py` - polling loop that decides when a backup is needed
- `app/pyca_config.py` + `templates/pyca.conf.template` - per-instance `pyca.conf` generation
- `app/compose_manager.py` + `templates/docker-compose.yaml.j2` - per-instance compose stack generation and lifecycle
- `app/port_allocator.py` - assigns each instance a free `pyca-ui` port
- `app/models.py` - SQLAlchemy `Instance` model (MySQL)
- `app/routers/instances.py` - CRUD + start/stop/restart API
- `app/routers/monitoring.py` - status summary + recordings disk usage
- `app/routers/web.py` + `templates/web/index.html` + `static/` - a Bootstrap
  5 dashboard for monitoring and managing instances, served at `/`: stat
  cards (instance counts, "needs attention", disk used), a
  searchable/filterable/sortable instance table with a per-row "Actions"
  dropdown, add/details/confirm modals, and toast/stale-data feedback on
  poll failure. The 5s poll (`static/app.js`'s `refresh()`) does a keyed diff
  against existing `<tr>` elements rather than replacing `tbody.innerHTML`,
  so open dropdown/focus/scroll state survives a background refresh. Baseline
  accessibility pass: skip link, landmarks, `aria-sort`, focus-visible
  outlines, `prefers-reduced-motion` support, accessible form validation.
  Verified against the running app (CRUD calls exercised, response shapes
  confirmed) but not in an actual browser - visual rendering, focus-trap
  behavior, phone-width layout, and contrast are unconfirmed.

The full loop has been run end-to-end against a real Opencast + PyCA
instance (see `dev/opencast-stack/`, a local test fixture), twice, with a
real (non-testsrc) synthetic RTSP camera feed: a scheduled event created via
the External API, the capture agent taken offline, `decision_engine`
detecting it and provisioning a real 5-container backup PyCA stack, an
actual RTSP recording completing successfully, the resulting recording
manually ingested (PyCA's own "paused after recording" -> "finished
recording" API, mirroring [fallback-capture-agent](../fallback-capture-agent)'s
manual Ingest button), and Opencast fully processing the result. Confirmed
via the HTTP API too: `GET /instances`, `GET /instances/{id}` (live
container status), start/stop, and `GET /monitoring/summary` (disk usage)
all work as expected.

That testing surfaced and fixed four real bugs, each with more detail in its
commit and in the relevant module:

- `provision_and_start()` now refuses to start a backup with no RTSP source
  resolved for the agent, instead of silently generating a capture command
  missing its `-i` argument.
- `pyca.conf`'s `[server] url` now renders from a separate
  `OPENCAST_CONTAINER_HOST`/`OPENCAST_CONTAINER_PROTOCOL` setting (falling
  back to `OPENCAST_HOST`/`OPENCAST_PROTOCOL`), since a PyCA container often
  can't reach Opencast the same way the controller process does (e.g.
  `localhost` on the host vs. `host.docker.internal` in a container).
- `get_scheduled_events()` now filters client-side on `technical_end >= now`
  instead of the API's `startDate >= now`, so an in-progress recording stays
  counted as "still needed" through its whole window instead of being torn
  down the instant its start time passes.
- `decision_engine.has_pending_ingest()` now checks the instance's own PyCA
  UI before tearing it down, so a completed recording sitting in PyCA's
  "paused after recording" state (awaiting manual ingest - backup-mode
  agents never auto-ingest) doesn't get torn down out from under an operator.
  UI credentials are persisted on the `Instance` row so this - and an
  operator's own login - survive restarts.

Two things hit during testing were confirmed to be artifacts of this local
test rig, not app bugs - see "Testing against a real Opencast + PyCA" below.

## Testing against a real Opencast + PyCA

`dev/opencast-stack/` has a local test fixture (adapted from the official
[opencast-docker](https://github.com/opencast/opencast-docker) reference
compose files): a real Opencast allinone instance, OpenSearch, and a stock
PyCA registered as capture agent `pyca-container`.

```bash
# Build the OpenSearch image with the analysis-icu plugin Opencast's index
# requires - `docker compose`'s own inline `build:` needs a working buildx
# setup this environment didn't have, so build it separately first:
docker build -f dev/opencast-stack/opensearch.Dockerfile -t opencast/opensearch:1 dev/opencast-stack

docker compose -f dev/opencast-stack/compose.yaml up -d
# Opencast admin UI: http://localhost:8080 (admin/opencast) - JVM startup
# takes a few minutes, longer under amd64 emulation on Apple Silicon.
```

Environment quirks hit while getting this running (all specific to this
local rig, not app bugs):

- Opencast's `opencast/allinone` and `pyca` images are amd64-only; under
  Docker Desktop's Apple Silicon emulation, Opencast's JVM was OOM-killed
  (exit 137) at the default ~2GB VM memory limit. 6-8GB fixed it.
- `docker compose`/`docker build` failing with
  `error getting credentials - err: exec: "docker-credential-desktop"...`
  means that binary (`~/.docker/bin/docker-credential-desktop`) isn't on
  `PATH` in that shell - add it or invoke docker with an updated `PATH`.
- Point `pyca-orchestrator`'s own `.env` at this stack with
  `OPENCAST_HOST=localhost:8080`, `OPENCAST_PROTOCOL=http`, and (PyCA
  containers don't share the controller's `localhost`)
  `OPENCAST_CONTAINER_HOST=host.docker.internal:8080`,
  `OPENCAST_CONTAINER_PROTOCOL=http`.
- Opencast's `ORG_OPENCASTPROJECT_SERVER_URL` (`http://opencast:8080` here)
  is what it hands back for its own registered service endpoints (scheduler,
  ingest, etc), not just the URL used to reach it initially - and a backup
  PyCA stack, being a separate `docker compose` project, is on a different
  Docker network by default and can't resolve that hostname. Workaround:
  `docker network connect opencast-stack_default <container>` for each
  `pyca-*` container. A real deployment wouldn't hit this, since that
  setting would be a real, publicly-resolvable hostname there.
- The `quay.io/opencast/pyca` image's bundled `ffmpeg` is a statically
  linked glibc build, a known case where hostname resolution skips
  `/etc/hosts` and does a raw DNS query only - so it can't resolve
  `host.docker.internal` even though the container's own shell (`getent
  hosts`) resolves it fine. Use the actual gateway IP (from `docker exec
  <container> cat /etc/hosts`) in a test RTSP source instead of the
  hostname. A real camera with a routable IP wouldn't hit this.

## Known gaps / next steps

- **No migrations yet.** `Base.metadata.create_all()` runs on startup instead
  of Alembic, and only creates missing *tables*, not missing *columns* on an
  existing one - so an existing `instances` table needs a manual `ALTER
  TABLE` for any new column (e.g. `ui_username`/`ui_password`). Fine for
  iterating on the schema now, not for production.
- **No auth on the FastAPI app itself.** Anyone who can reach it can start/stop
  instances. Needs at least a shared-secret or basic auth layer before this
  is reachable from anywhere but localhost.
- **Bind-mount permissions.** The PyCA image runs as uid 800 (see its
  Dockerfile). The generated compose file bind-mounts `./data` to
  `/var/lib/pyca` (chosen over a named volume so the controller can read
  recording sizes directly) - on Linux hosts that directory needs to be
  writable by uid 800, or PyCA will fail to write its sqlite db/recordings.
- **Controller runs on the host, not containerized**, because it shells out
  to `docker compose` with paths relative to `INSTANCES_DIR`; running the
  controller itself inside a container (Docker-outside-of-Docker) means
  those relative paths resolve against the wrong filesystem unless remapped.
  See the comment in `docker-compose.yml`.
- **Decision engine is single-pass per venue.** It doesn't yet distinguish
  "agent came back mid-recording" from "event ended" - both currently just
  tear the instance down on the next poll once the agent drops out of the
  scheduled-events window (teardown itself now correctly waits for any
  pending manual ingest first - see `has_pending_ingest()`).
- **No recording handoff/ingest verification.** `has_pending_ingest()` stops
  the decision engine from tearing an instance down while PyCA still shows
  a recording "paused after recording", but that only confirms PyCA has
  *started* ingest, not that Opencast's workflow actually succeeded - a
  recording could finish uploading and then fail Opencast-side processing
  with nothing here noticing.
- **`GET /monitoring/summary`'s disk-usage walk doesn't scale.**
  `_dir_size_bytes()` in `app/routers/monitoring.py` does a synchronous
  `Path.rglob("*")` + `stat()` over every instance's `data/recordings`
  directory, for every instance, on every call to `/monitoring/summary` - and
  since the dashboard's `static/app.js` polls that endpoint every 5s
  (`REFRESH_MS`), every open dashboard tab re-triggers the full walk on that
  cadence. Fine at today's scale (1-2 test instances), but both instance
  count and per-instance recording volume make this worse linearly, and
  it's on the request path with no caching. Needs either a cached/debounced
  size (e.g. refreshed on a slower background timer) or moving the
  computation off the synchronous request path before instance count or
  recording volume grows meaningfully.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in Opencast + MySQL credentials

docker compose up -d mysql   # just the DB dependency

uvicorn app.main:app --reload --port 8090
```

Dashboard: http://localhost:8090/ (8080 is taken by Opencast itself if you're
also running the local test stack below)

## Configuration

See [.env.example](.env.example) for all settings: Opencast connection,
MySQL connection, the `pyca-ui` port range to allocate from, and the
thresholds the decision engine uses (poll interval, recording lead time,
agent-offline timeout).

## API

- `GET /instances` / `POST /instances` - list / manually register an instance
- `GET /instances/{id}` - DB record + live container status
- `POST /instances/{id}/start` / `/stop` / `/restart`
- `DELETE /instances/{id}` - remove a stopped instance
- `GET /monitoring/summary` - counts by status + recordings disk usage per instance
- `GET /monitoring/health` - liveness check
