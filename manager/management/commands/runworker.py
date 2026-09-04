import signal
import time

from django.core.management.base import BaseCommand

from manager.task_processor import cleanup_expired, process_one, reconcile_scanner_validations, sync_print_jobs


class Command(BaseCommand):
    help = "Run the persistent Printer Manager task worker"

    def handle(self, *args, **options):
        running = True

        def stop(*_args):
            nonlocal running
            running = False

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        reconcile_scanner_validations()
        last_maintenance = 0
        while running:
            worked = process_one()
            now = time.monotonic()
            if now - last_maintenance >= 15:
                sync_print_jobs()
                cleanup_expired()
                last_maintenance = now
            if not worked:
                time.sleep(1)
