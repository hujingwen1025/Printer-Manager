from django.apps import AppConfig
from django.db.backends.signals import connection_created


def configure_sqlite(sender, connection, **kwargs):
    if connection.vendor == "sqlite":
        cursor = connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL;")
        cursor.execute("PRAGMA foreign_keys=ON;")


class ManagerConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "manager"

    def ready(self):
        connection_created.connect(configure_sqlite)
