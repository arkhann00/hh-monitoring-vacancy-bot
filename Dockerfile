FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    STATE_FILE=/data/state.json

WORKDIR /app

RUN useradd --create-home --uid 10001 monitor \
    && mkdir /data \
    && chown monitor:monitor /data

COPY --chown=monitor:monitor main.py /app/main.py

USER monitor

VOLUME ["/data"]

CMD ["python", "/app/main.py"]
