FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# Непривилегированный пользователь; /data — volume с сессией MAX и файлом KILL_SWITCH.
# Файл сессии в образ не копируется никогда (см. .dockerignore).
RUN useradd --uid 10001 --create-home gateway \
    && mkdir -p /data/session \
    && chown -R gateway:gateway /data \
    && chmod 700 /data/session

WORKDIR /app
COPY pyproject.toml ./
COPY gateway ./gateway
RUN pip install .

COPY docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod 755 /usr/local/bin/docker-entrypoint.sh

USER gateway
VOLUME ["/data"]
ENTRYPOINT ["docker-entrypoint.sh"]
CMD ["run"]
