# syntax=docker/dockerfile:1
# python:3.12.16-slim, pinned by digest (update both together). The digest is the multi-arch
# index (linux/amd64, linux/arm64/v8, ...), so the same pin builds natively on arm64 hosts.
FROM python:3.12-slim@sha256:b18b261e1b4974b4f321451ad5251ded85abb22c2b8b262da527dda1d031ab30

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

RUN useradd --system --uid 10001 --no-create-home --shell /usr/sbin/nologin zipbot
# STATS_PATH's directory (docker-compose.yml mounts a named volume here). A new, empty named
# volume takes this directory's owner and mode, so the non-root user can write to it.
RUN mkdir /data && chown zipbot:zipbot /data && chmod 700 /data

WORKDIR /app
COPY requirements.lock ./
# Every download is hash-checked; the build backend (setuptools) comes from the lock too.
RUN pip install --root-user-action=ignore --require-hashes --no-deps -r requirements.lock
COPY pyproject.toml ./
COPY src ./src
RUN pip install --root-user-action=ignore --no-deps --no-build-isolation . && rm -rf /app/src /app/build

USER zipbot
EXPOSE 8080
HEALTHCHECK --interval=30s --timeout=3s --start-period=20s --retries=3 \
    CMD ["python", "-c", "import urllib.request,sys; sys.exit(urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=2).status != 200)"]
CMD ["python", "-m", "zipbot"]
