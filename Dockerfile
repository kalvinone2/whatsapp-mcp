FROM golang:1.26.8-bookworm AS connector
WORKDIR /build
COPY whatsapp-bridge/go.mod whatsapp-bridge/go.sum ./
RUN go mod download
COPY whatsapp-bridge/*.go ./
RUN CGO_ENABLED=1 go build -o /arc-connector .

FROM python:3.11-slim-bookworm
RUN groupadd --gid 1001 arc && useradd --uid 1001 --gid arc --no-create-home arc && mkdir /data && chown arc:arc /data
COPY --from=connector /arc-connector /usr/local/bin/arc-connector
WORKDIR /app
COPY whatsapp-mcp-server/api.py whatsapp-mcp-server/whatsapp.py whatsapp-mcp-server/service.py ./
ENV ARC_DATA_DIR=/data ARC_CONTEXT_DB=/data/store/context.db ARC_BIND=0.0.0.0 PYTHONDONTWRITEBYTECODE=1 PYTHONUNBUFFERED=1
USER arc
EXPOSE 8080
CMD ["python", "service.py"]
