"""Ensure Django Groups exist for Keycloak SSO role mapping."""

from django.core.management.base import BaseCommand
from django.contrib.auth.models import Group


DEFAULT_GROUPS = {
    'Admin': 21,
    'ViewOnly': 20,
    'Management': 20,
    'Onboard': 20,
    'UserView': 20,
    'TechInfra': 20,
    'Risk': 20,
    'TradeSupport': 20,
    'Google': 20,
    'O365': 20,
    'DjangoAdmin': 20,
}


class Command(BaseCommand):
    help = 'Create LinkedEye Django Groups used by Keycloak SSO role mapping'

    def handle(self, *args, **options):
        created = 0
        for name, weightage in DEFAULT_GROUPS.items():
            group, was_created = Group.objects.get_or_create(name=name, defaults={'weightage': weightage})
            if not was_created and getattr(group, 'weightage', None) != weightage:
                group.weightage = weightage
                group.save(update_fields=['weightage'])
            if was_created:
                created += 1
                self.stdout.write(self.style.SUCCESS(f'Created group {name} (weightage={weightage})'))
        self.stdout.write(self.style.SUCCESS(f'Groups ready ({created} new).'))
