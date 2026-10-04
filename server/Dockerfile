# The Konduktor server: one library (Traktor or Rekordbox), held where its audio
# lives, opened from the desktop app as a "Remote" library.
#
# Build from the REPOSITORY ROOT (the version is read from frontend/package.json):
#   docker build -f server/Dockerfile -t konduktor-server .
#
# Configuration is the environment — see server/.env.example.
FROM python:3.12-slim AS build
RUN apt-get update \
 && apt-get install -y --no-install-recommends git \
 && rm -rf /var/lib/apt/lists/*
COPY backend/requirements-server.txt /tmp/requirements-server.txt
RUN pip install --no-cache-dir --prefix=/install -r /tmp/requirements-server.txt

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    KONDUKTOR_DATA_DIR=/data \
    KONDUKTOR_PORT=8765
COPY --from=build /install /usr/local
# `konduktor/__init__.py` reads the app version from <repo>/frontend/package.json,
# two levels above the package.
COPY frontend/package.json /app/frontend/package.json
COPY backend/konduktor /app/backend/konduktor
WORKDIR /app/backend
# History, export sets and the stems ledger.
VOLUME ["/data"]
EXPOSE 8765
CMD ["python", "-m", "konduktor.server"]
