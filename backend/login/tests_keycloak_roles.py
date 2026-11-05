"""Regression tests for Keycloak -> Django role synchronisation.

Covers the bug where changing a user's role in Keycloak left the previous role
in place: sync_keycloak_user() treated an empty role mapping as "leave groups
alone", so a role removed in Keycloak never propagated and role_required() went
on enforcing the stale group.
"""

from django.contrib.auth.models import Group, User
from django.db import connection
from django.http import HttpResponse
from django.test import RequestFactory, TestCase

from login.decorators import role_required
from login.keycloak_utils import extract_keycloak_roles, map_roles_to_django_groups, sync_keycloak_user


def claims_with_roles(*roles, email='tester@finspot.in'):
    """Minimal Keycloak ID-token shape carrying realm roles."""
    return {'email': email, 'realm_access': {'roles': list(roles)}}


def claims_with_tenant_roles(*tenant_roles, email='tester@finspot.in', stale_realm_roles=()):
    """Staff broker token: tenant_roles is authoritative; realm_access may be stale."""
    claims = {'email': email, 'tenant_roles': list(tenant_roles)}
    if stale_realm_roles:
        claims['realm_access'] = {'roles': list(stale_realm_roles)}
    return claims


class WeightageSchemaMixin:
    """Create auth_group.weightage, which the test runner otherwise lacks.

    useronboard/models.py does Group.add_to_class('weightage', ...) on Django's
    built-in auth.Group, and no app in this project commits migrations --
    entrypoint.py runs `makemigrations` at container startup, so deployed
    databases end up with the column. The test runner only applies committed
    migrations, so without this the table has no weightage column and every
    Group query fails. This reproduces the deployed schema.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        with connection.cursor() as cursor:
            try:
                cursor.execute(
                    'ALTER TABLE auth_group ADD COLUMN weightage integer NOT NULL DEFAULT 16'
                )
            except Exception:
                pass  # already present (e.g. running against a real deployed DB)


class RoleSyncTests(WeightageSchemaMixin, TestCase):
    def setUp(self):
        for name, weight in (('Admin', 21), ('ViewOnly', 20), ('Management', 20)):
            Group.objects.get_or_create(name=name, defaults={'weightage': weight})
        self.user = User.objects.create_user(
            username='tester@finspot.in', email='tester@finspot.in', password='x'
        )

    def group_names(self):
        return sorted(self.user.groups.values_list('name', flat=True))

    def test_initial_role_is_applied(self):
        sync_keycloak_user(self.user, claims_with_roles('Admin'))
        self.assertEqual(self.group_names(), ['Admin'])

    def test_role_change_replaces_previous_role(self):
        sync_keycloak_user(self.user, claims_with_roles('Admin'))
        sync_keycloak_user(self.user, claims_with_roles('Management'))
        self.assertEqual(self.group_names(), ['Management'])

    def test_role_removal_falls_back_to_default_not_stale_role(self):
        """The actual bug: no recognised role must not leave the old role behind."""
        sync_keycloak_user(self.user, claims_with_roles('Admin'))
        sync_keycloak_user(self.user, claims_with_roles())
        self.assertEqual(self.group_names(), ['ViewOnly'])
        self.assertNotIn('Admin', self.group_names())

    def test_unrecognised_role_does_not_preserve_old_role(self):
        """A role that exists in Keycloak but has no Django Group must not grant the old one."""
        sync_keycloak_user(self.user, claims_with_roles('Admin'))
        sync_keycloak_user(self.user, claims_with_roles('SomeRoleOnlyInKeycloak'))
        self.assertEqual(self.group_names(), ['ViewOnly'])

    def test_admin_created_group_is_mappable(self):
        """Groups created via useronboard addRoles must work over SSO."""
        Group.objects.create(name='CustomOps', weightage=20)
        sync_keycloak_user(self.user, claims_with_roles('CustomOps'))
        self.assertEqual(self.group_names(), ['CustomOps'])

    def test_all_groups_cleared_when_default_group_missing(self):
        sync_keycloak_user(self.user, claims_with_roles('Admin'))
        Group.objects.filter(name='ViewOnly').delete()
        sync_keycloak_user(self.user, claims_with_roles())
        self.assertEqual(self.group_names(), [])

    def test_map_roles_ignores_unknown_and_keeps_known(self):
        groups = map_roles_to_django_groups(['Admin', 'NotARealRole'])
        self.assertEqual([g.name for g in groups], ['Admin'])

    def test_tenant_roles_override_stale_realm_access(self):
        """Staff SSO must honour finspot-management tenant_roles, not shadow realm roles."""
        claims = claims_with_tenant_roles(
            'mobikwik-ViewOnly',
            stale_realm_roles=['Admin', 'DjangoAdmin', 'ViewOnly'],
        )
        self.assertEqual(extract_keycloak_roles(claims), ['ViewOnly'])
        sync_keycloak_user(self.user, claims)
        self.assertEqual(self.group_names(), ['ViewOnly'])


class RoleRequiredTests(WeightageSchemaMixin, TestCase):
    def setUp(self):
        self.factory = RequestFactory()
        # ViewOnly is created FIRST so it has the lower pk. groups.first() has no
        # ORDER BY and so returns it, while -weightage ordering returns Admin --
        # the two disagree, which is what makes the test below meaningful.
        for name, weight in (('ViewOnly', 20), ('Admin', 21)):
            Group.objects.get_or_create(name=name, defaults={'weightage': weight})
        self.user = User.objects.create_user(
            username='dual@finspot.in', email='dual@finspot.in', password='x'
        )

    def call_protected_view(self, allowed):
        @role_required(allowed_roles=allowed)
        def view(request):
            return HttpResponse('ok')

        request = self.factory.get('/')
        request.user = self.user
        return view(request)

    def test_highest_weightage_group_wins(self):
        """Enforcement must agree with the admin grid, which orders by -weightage."""
        self.user.groups.set(Group.objects.filter(name__in=['Admin', 'ViewOnly']))
        self.assertEqual(self.call_protected_view(['Admin']).status_code, 200)

    def test_user_without_required_role_is_denied(self):
        self.user.groups.set(Group.objects.filter(name='ViewOnly'))
        self.assertEqual(self.call_protected_view(['Admin']).status_code, 403)

    def test_user_with_no_groups_is_denied(self):
        self.user.groups.clear()
        self.assertEqual(self.call_protected_view(['Admin']).status_code, 403)
