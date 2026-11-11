# docker-fallback-orchestrator

Automatic backup recording for Opencast lecture venues. When a venue's capture
agent is offline or unhealthy, this controller starts a temporary
[PyCA](https://github.com/opencast/pyCA) capture agent that records the venue's
camera instead, and removes it once the agent has proven stable again.

## How it works

1. **Watch.** Every minute the controller reads the Opencast schedule (the next
   day) and the state of every capture agent, and remembers each agent's
   health over time.
2. **Start a backup.** A lecture gets a backup if its capture agent is offline
   or unhealthy now, **or was at any point in the 24 hours before the
   lecture** - even if it has since been fixed.
3. **Record.** A backup is a small PyCA stack (5 Docker containers) for that
   venue. It records the venue's camera stream (taken from the agent's
   registration in Opencast) for every lecture on that agent's schedule.
   Backups **never ingest on their own**: an operator reviews a recording and
   ingests it from the backup's PyCA web page.
4. **Stop the backup** only when all of these are true:
   - the capture agent has been healthy for 24 hours without interruption;
   - 4 days have passed since the last lecture that needed the backup;
   - the backup isn't recording or ingesting right now.

   Recordings waiting to be ingested don't keep a backup up: they stay on
   the server. To ingest one after its backup has stopped, press **Start** on
   the dashboard - a backup started by hand stays up (marked *Manual*) until
   someone stops it.
5. **Clean up.** Recordings are deleted 7 days after they were made, whether
   or not they were ingested. Operators can change that per recording, or
   delete one straight away, on the Recordings page.

Everything the controller does is written to an audit log.

**Recordings** are stored on the server as fragmented MP4 (playable even if
cut off mid-recording):

```
/data/pyca-recordings/<venue>/recording-<start>-<mediapackage id>/recording-....mp4
```

**Dashboard** at `http://<host>:8090/`:

| Page | Shows |
|---|---|
| Instances | Every venue's backup, its status, disk use; start, stop, restart, delete |
| Audit log | Every action, who or what did it, and why |
| Recordings | Every recording, whether it was ingested, when it will be deleted; change retention or delete |

## Defaults

All behaviour is configurable through environment variables (`.env`).

| Setting | Default | What it controls |
|---|---|---|
| `POLL_INTERVAL_SECONDS` | `60` | How often Opencast is checked |
| `SCHEDULE_LOOKAHEAD_DAYS` | `1` | How far ahead lectures are considered |
| `AGENT_OFFLINE_SECONDS` | `300` | Silence after which an agent counts as offline |
| `AGENT_STABLE_HOURS` | `24` | Look-back for "was down recently", and healthy time needed before a backup stops |
| `BACKUP_RETENTION_DAYS` | `4` | How long a backup stays after the last lecture that needed it |
| `RECORDING_RETENTION_DAYS` | `7` | Days before a recording is deleted (changeable per recording) |
| `RECORDING_CLEANUP_INTERVAL_MINUTES` | `60` | How often the clean-up runs |
| `RECORDINGS_DIR` | `/data/pyca-recordings` | Where recordings are stored |
| `INSTANCES_DIR` | `./instances` | Each backup's config files and PyCA state |
| `UI_PORT_RANGE_START` / `_END` | `9000` / `9100` | Ports for the backups' PyCA web pages |
| `PYCA_PORTS_FILE` | `config/pyca-ports.yaml` | Fixed PyCA page port per venue (see `config/pyca-ports.example.yaml`); unlisted venues get a free port from the range |
| `COMPOSE_TIMEOUT_SECONDS` | `300` | Give up on a stuck `docker compose` command |
| `PYCA_IMAGE` | `quay.io/opencast/pyca` | PyCA image used for backups |

Agent states counted as healthy: `idle`, `capturing`, `uploading`.

## Running it

You need Docker, a MySQL database, and an Opencast system account. Copy
`.env.example` to `.env` and fill in at least:

| Setting | Example |
|---|---|
| `OPENCAST_HOST`, `OPENCAST_PROTOCOL` | `opencast.example.ac.za`, `https` |
| `OPENCAST_USERNAME`, `OPENCAST_PASSWORD` | the system account |
| `MYSQL_HOST`, `MYSQL_PORT`, `MYSQL_DATABASE`, `MYSQL_USER`, `MYSQL_PASSWORD` | the database |

The app refuses to start while a password is still `CHANGE_ME`. Its tables are
created automatically on first start.

### With Docker (recommended)

```bash
docker compose up -d --build controller
```

The dashboard is then on port 8090. The controller starts the backups through
the host's Docker, so:

- it mounts the Docker socket;
- `INSTANCES_HOST_DIR` and `RECORDINGS_HOST_DIR` must be **absolute paths**,
  mounted at the same path inside the container (defaults: `./instances`
  next to `docker-compose.yml`, and `/data/pyca-recordings`);
- `localhost` inside the container means the container itself - point it at
  services on the host or in compose with `CONTROLLER_MYSQL_HOST` and
  `CONTROLLER_OPENCAST_HOST`.

`docker-compose.yml` also includes a MySQL service for development
(`docker compose up -d mysql`).

### Locally, for development

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements-dev.txt
cp .env.example .env              # fill in Opencast + MySQL
docker compose up -d mysql        # a local database
uvicorn app.main:app --reload --port 8090
```

For local development set `RECORDINGS_DIR=./recordings` (git-ignored). If
your dev passwords really are `CHANGE_ME`, add `ALLOW_PLACEHOLDER_SECRETS=true`.

### Tests

```bash
pytest -q                               # no MySQL or Docker needed
ruff check . && ruff format --check .
```

CI runs both on every push and pull request.

To test a deployment end to end against its Opencast with a fake venue, run
`scripts/demo_test.sh run` on the controller's host, then
`scripts/demo_test.sh cleanup` (details at the top of the script).

## Important

- **There is no login.** Anyone who can reach port 8090 can start, stop and
  delete backups and recordings. Keep it on a trusted network.
- **Run exactly one controller.** Two would start the same backups twice.
- **The Docker socket gives root on the host.** Treat the controller's host
  accordingly.

## Monitoring

- `GET /monitoring/health` - the process is up.
- `GET /monitoring/ready` - 503 unless the database answers and Opencast was
  polled successfully in the last 3 minutes. Alert on this one.

API errors are JSON (`{"detail": ...}`); unexpected ones include an
`error_id` that matches the logged traceback. The full API is documented at
`/docs`.
