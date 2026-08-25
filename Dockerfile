# Multi-stage build
FROM python:3.12-slim AS builder

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir --prefix=/install -r requirements.txt

# Runtime
FROM python:3.12-slim
WORKDIR /app

COPY --from=builder /install /usr/local
COPY . .

ENV PYTHONPATH=/app \
    PYTHONUNBUFFERED=1

RUN addgroup --system --gid 1001 app && \
    adduser --system --uid 1001 --gid 1001 app && \
    chown -R app:app /app

USER app
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python3 -c "import http.client;c=http.client.HTTPConnection('localhost',8000);c.request('GET','/health');r=c.getresponse();exit(0) if r.status==200 else exit(1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]