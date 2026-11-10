# The controller: FastAPI app + decision engine + recordings clean-up, in one process.
#
# It drives each venue's PyCA stack with `docker compose` against the host's Docker
# daemon (via the mounted socket), so the image carries the Docker CLI and Compose
# plugin. See docker-compose.yml for how it must be run - in particular, INSTANCES_DIR
# has to be mounted at the same absolute path it has on the host.

FROM docker:29.7.2-cli AS docker-cli

FROM python:3.12-slim

COPY --from=docker-cli /usr/local/bin/docker /usr/local/bin/docker
COPY --from=docker-cli /usr/local/libexec/docker/cli-plugins/docker-compose /usr/local/libexec/docker/cli-plugins/docker-compose

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    TZ=Africa/Johannesburg \
    DEBIAN_FRONTEND=noninteractive \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY app ./app
COPY templates ./templates
COPY static ./static

EXPOSE 8090

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://localhost:8090/monitoring/health', timeout=4)"

# One process only: the decision engine runs inside it, and two would start the same backups.
CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8090", "--workers", "1"]
