FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PM_DATA_DIR=/data/app \
    PM_CUPS_SERVER=/run/cups/cups.sock \
    PM_PORT=8080 \
    SANE_CONFIG_DIR=/data/app/sane

RUN apt-get update && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
      cups cups-client cups-filters libcups2-dev gcc python3-dev \
      sane-utils sane-airscan \
      libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
      file libmagic1 fonts-dejavu-core supervisor tini curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && (getent group scanner >/dev/null || groupadd --system scanner) \
    && (getent group lpadmin >/dev/null || groupadd --system lpadmin) \
    && groupadd --system printermanager \
    && useradd --system --gid printermanager --groups lp,lpadmin,scanner --home-dir /nonexistent --shell /usr/sbin/nologin printermanager

WORKDIR /app
COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY . /app
RUN chmod +x /app/docker/entrypoint.sh \
    && mkdir -p /data/app /run/cups \
    && chown -R printermanager:printermanager /data/app \
    && python manage.py collectstatic --noinput

EXPOSE 8080
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/docker/supervisord.conf"]
