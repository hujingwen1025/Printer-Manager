FROM python:3.12-slim-bookworm

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PM_DATA_DIR=/data/app \
    PM_CUPS_SERVER=/run/cups/cups.sock \
    PM_PORT=80 \
    SANE_CONFIG_DIR=/data/app/sane

RUN set -eu; \
    sed -i 's|http://deb.debian.org|https://deb.debian.org|g' /etc/apt/sources.list.d/debian.sources; \
    apt-get -o Acquire::Retries=5 update; \
    install_attempt=1; \
    until DEBIAN_FRONTEND=noninteractive apt-get -o Acquire::Retries=5 install -y --no-install-recommends \
        cups cups-client cups-filters libcups2-dev gcc python3-dev \
        sane-utils sane-airscan hplip libsane-hpaio \
        libreoffice-core libreoffice-writer libreoffice-calc libreoffice-impress \
        file libmagic1 fonts-dejavu-core supervisor tini curl ca-certificates; do \
      if [ "$install_attempt" -ge 3 ]; then exit 1; fi; \
      install_attempt=$((install_attempt + 1)); \
      apt-get -o Acquire::Retries=5 update; \
    done; \
    rm -rf /var/lib/apt/lists/* \
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

EXPOSE 80
ENTRYPOINT ["/usr/bin/tini", "--", "/app/docker/entrypoint.sh"]
CMD ["/usr/bin/supervisord", "-c", "/app/docker/supervisord.conf"]
