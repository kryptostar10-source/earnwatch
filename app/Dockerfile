FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    EW_DATA_DIR=/data

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt \
 && useradd --system --uid 10001 --home-dir /nonexistent --shell /usr/sbin/nologin earnwatch \
 && mkdir -p /data && chown 10001:10001 /data && chmod 700 /data

COPY earnwatch/ ./earnwatch/

USER 10001
EXPOSE 8080
HEALTHCHECK --interval=60s --timeout=5s --start-period=20s \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8080/healthz',timeout=4).status==200 else 1)"

# Nur Caddy (interner Docker-Netz) spricht mit der App -> Proxy-Header vertrauen
CMD ["uvicorn", "earnwatch.main:app", "--host", "0.0.0.0", "--port", "8080", \
     "--proxy-headers", "--forwarded-allow-ips", "*", "--no-server-header", "--log-level", "warning"]
