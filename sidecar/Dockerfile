# The MyEditor membership sidecar. Build from the repository root:
#   docker build -f sidecar/Dockerfile -t myeditor-sidecar .
# Dockerfile.dockerignore next to this file limits what the build sees.
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
WORKDIR /app

COPY sidecar/requirements.txt sidecar/requirements.txt
RUN pip install --no-cache-dir -r sidecar/requirements.txt

# Only the pieces of the app the sidecar uses: Nostr event checks.
COPY nostr/__init__.py nostr/crypto.py nostr/events.py nostr/nip98.py nostr/
COPY sidecar/__init__.py sidecar/app.py sidecar/

RUN useradd --system --no-create-home sidecar
USER sidecar

EXPOSE 8021
HEALTHCHECK --interval=30s --timeout=5s CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8021/healthz', timeout=3).status == 200 else 1)"

# Proxy headers are trusted from anywhere because this port is reachable
# only from Caddy on the compose network (it is never published). Running
# the image with its port exposed to the internet would let clients choose
# their own address and slip past the per-address limit.
# --no-access-log: uvicorn's access log would record every client address.
# One process, no --workers, and one replica: the limits and the record of
# used signatures live in memory, so a second process would double every
# limit and could accept a signature a second time.
CMD ["uvicorn", "sidecar.app:app", "--host", "0.0.0.0", "--port", "8021", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", \
     "--no-access-log"]
