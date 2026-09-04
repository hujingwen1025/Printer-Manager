#!/bin/sh
set -eu

if [ "${PM_DEBUG:-0}" != "1" ]; then
  if [ -z "${PM_SECRET_KEY:-}" ] && [ ! -r "${PM_SECRET_KEY_FILE:-/nonexistent}" ]; then
    echo "PM_SECRET_KEY or PM_SECRET_KEY_FILE is required" >&2
    exit 1
  fi
fi

mkdir -p /data/app/artifacts /data/app/static /data/app/sane /data/cups/etc /data/cups/spool /run/cups
if [ ! -f /data/cups/etc/cupsd.conf ]; then
  cp -a /etc/cups/. /data/cups/etc/
  cp /app/docker/cupsd.conf /data/cups/etc/cupsd.conf
fi
rm -rf /etc/cups
ln -s /data/cups/etc /etc/cups
rm -rf /var/spool/cups
ln -s /data/cups/spool /var/spool/cups
chown -R printermanager:printermanager /data/app
chown -R lp:lp /data/cups/spool

python manage.py migrate --noinput
python manage.py bootstrap
python manage.py collectstatic --noinput
chown -R printermanager:printermanager /data/app

exec "$@"
