FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    FPA_HOME=/app

WORKDIR /app
COPY pyproject.toml README.md ./
COPY src ./src
RUN pip install ".[dashboard]"

COPY sql ./sql
COPY semantic ./semantic
COPY dashboard ./dashboard
COPY .streamlit ./.streamlit

RUN useradd --create-home --uid 10001 app
USER app
