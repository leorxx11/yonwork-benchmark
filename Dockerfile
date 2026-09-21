FROM python:3.13-slim-bookworm

ARG APP_UID=1000
ARG APP_GID=1000

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY runner/requirements.txt /tmp/requirements.txt
RUN pip install --no-cache-dir -r /tmp/requirements.txt \
    && groupadd --gid "${APP_GID}" benchmark \
    && useradd --uid "${APP_UID}" --gid benchmark --create-home benchmark

COPY runner ./runner
COPY web ./web
COPY cases ./cases

RUN mkdir -p /app/results && chown -R benchmark:benchmark /app

USER benchmark

EXPOSE 8000

CMD ["python", "-m", "uvicorn", "web.api:app", "--host", "0.0.0.0", "--port", "8000"]
