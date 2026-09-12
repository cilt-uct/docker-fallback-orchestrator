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

Early scaffold. The core pieces exist and are wired together, but this has
not been run against a live Opencast instance yet:

- `app/opencast_client.py` - capture-admin + scheduler API client (digest auth)
- `app/decision_engine.py` - polling loop that decides when a backup is needed
- `app/pyca_config.py` + `templates/pyca.conf.template` - per-instance `pyca.conf` generation
- `app/compose_manager.py` + `templates/docker-compose.yaml.j2` - per-instance compose stack generation and lifecycle
- `app/port_allocator.py` - assigns each instance a free `pyca-ui` port
- `app/models.py` - SQLAlchemy `Instance` model (MySQL)
- `app/routers/instances.py` - CRUD + start/stop/restart API
- `app/routers/monitoring.py` - status summary + recordings disk usage

## Known gaps / next steps

- **No migrations yet.** `Base.metadata.create_all()` runs on startup instead
  of Alembic. Fine for iterating on the schema now, not for production.
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
  scheduled-events window.
- **No recording handoff/ingest verification.** Once PyCA ingests a capture,
  nothing here confirms Opencast actually received it before tearing the
  instance down.

## Running locally

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env   # fill in Opencast + MySQL credentials

docker compose up -d mysql   # just the DB dependency

uvicorn app.main:app --reload --port 8080
```

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
