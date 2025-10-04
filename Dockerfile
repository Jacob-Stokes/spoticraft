FROM python:3.12-slim AS base

ENV PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /app

COPY pyproject.toml README.MD ./
COPY spotifreak/ spotifreak/
COPY .spotifreak-sample/ .spotifreak-sample/

RUN pip install --upgrade pip \
    && pip install '.[web]'

ENV SPOTIFREAK_CONFIG_DIR=/config \
    SPOTIFREAK_STATE_DIR=/state \
    SPOTIFREAK_LOG_FILE=/logs/spotifreak.log

VOLUME ["/config", "/state", "/logs"]

ENTRYPOINT ["spotifreak"]
CMD ["serve", "--config-dir", "/config"]
