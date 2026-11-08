"""Create the first administrator(s) of the legacy console.

This replaces the old behaviour where the login page created ``admin`` and
``djangoadmin`` (and logged the caller in) the first time anyone posted that
username. An operator now runs this once, with the password supplied through
``ADMIN_DEFAULT_PASSWORD`` (Vault bundle or environment):

    ADMIN_DEFAULT_PASSWORD='...' python manage.py bootstrap_admin
    ADMIN_DEFAULT_PASSWORD='...' python manage.py bootstrap_admin --djangoadmin

Existing users are never modified. Roles (groups) are created when missing.
"""
import os

from django.conf import settings
from django.contrib.auth.models import Group, User
from django.core.management.base import BaseCommand, CommandError

# name -> weightage (21 = 010101 ['VSA','VA','ESA','EA','DSA','DA'])
ROLES = {'Admin': 21, 'ViewOnly': 20, 'Google': 20, 'O365': 20, 'DjangoAdmin': 20}


class Command(BaseCommand):
    help = "Create the 'admin' user (and optionally the 'djangoadmin' superuser) with ADMIN_DEFAULT_PASSWORD."

    def add_arguments(self, parser):
        parser.add_argument('--djangoadmin', action='store_true',
                            help="Also create the 'djangoadmin' Django superuser (for /admin).")

    def handle(self, *args, **options):
        password = getattr(settings, 'ADMIN_DEFAULT_PASSWORD', '') or os.getenv('ADMIN_DEFAULT_PASSWORD', '')
        if not password:
            raise CommandError("ADMIN_DEFAULT_PASSWORD is not configured; refusing to create accounts.")

        for name, weightage in ROLES.items():
            Group.objects.get_or_create(name=name, defaults={'weightage': weightage})

        self._create('admin', password, 'Admin', superuser=False)
        if options['djangoadmin']:
            self._create('djangoadmin', password, 'DjangoAdmin', superuser=True)

    def _create(self, username, password, group_name, superuser):
        if User.objects.filter(username=username).exists():
            self.stdout.write("User '%s' already exists; left unchanged." % username)
            return
        extra = dict(email=username, first_name=username if not superuser else 'django',
                     last_name=username if not superuser else 'admin', is_active=True)
        if superuser:
            user = User.objects.create_superuser(username=username, password=password, **extra)
        else:
            user = User.objects.create_user(username=username, password=password, **extra)
        user.groups.add(Group.objects.get(name=group_name))
        self.stdout.write(self.style.SUCCESS("Created user '%s'." % username))
