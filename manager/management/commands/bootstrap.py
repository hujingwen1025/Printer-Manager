import os
from pathlib import Path

from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError

from manager.security import ROLES


class Command(BaseCommand):
    help = "Create roles and securely bootstrap the first administrator"

    def handle(self, *args, **options):
        groups = {name: Group.objects.get_or_create(name=name)[0] for name in ROLES}
        if User.objects.filter(groups=groups["admin"]).exists() or User.objects.filter(is_superuser=True).exists():
            return
        username = os.environ.get("PM_ADMIN_USERNAME", "").strip()
        password_file = os.environ.get("PM_ADMIN_PASSWORD_FILE", "").strip()
        password = os.environ.get("PM_ADMIN_PASSWORD", "").strip()
        if not username or (not password and not password_file):
            raise CommandError("First startup requires PM_ADMIN_USERNAME and either PM_ADMIN_PASSWORD or PM_ADMIN_PASSWORD_FILE")
        if password_file:
            path = Path(password_file)
            if not path.is_file():
                raise CommandError("PM_ADMIN_PASSWORD_FILE does not point to a readable file")
            password = path.read_text(encoding="utf-8").strip()
        if len(password) < 12:
            raise CommandError("The initial administrator password must contain at least 12 characters")
        user = User.objects.create_user(username=username, password=password, is_staff=True, is_superuser=False)
        user.groups.add(groups["admin"])
        self.stdout.write(self.style.SUCCESS(f"Created administrator {username}"))
