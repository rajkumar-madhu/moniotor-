"""Regression tests for the legacy backend security review (area legacy-backend)."""
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from unittest import mock

from legacy_tests import harness

harness.setup()

from django.contrib.auth.models import Group, User  # noqa: E402
from django.core.management import call_command  # noqa: E402
from django.core.management.base import CommandError  # noqa: E402
from django.test import Client, TestCase, override_settings  # noqa: E402

BACKEND_DIR = harness.BACKEND_DIR
PYTHON = sys.executable


def _admin_group():
    group, _ = Group.objects.get_or_create(name='Admin', defaults={'weightage': 21})
    return group


def _post_login(client, view_url, username, password='x'):
    return client.post(view_url, {'alldata': json.dumps({'username': username, 'password': password})})


# --------------------------------------------------------------------------
# deploy-docs-1: no auto-created admin / superuser from the login view
# --------------------------------------------------------------------------
class LoginDoesNotBootstrapAdminTests(TestCase):
    URLS = ('/login/verify', '/login2/verify')

    def test_anonymous_cannot_create_admin_or_djangoadmin(self):
        for url in self.URLS:
            for name in ('admin', 'djangoadmin'):
                with override_settings(ADMIN_DEFAULT_PASSWORD='SetByOperator1!'):
                    client = Client()
                    resp = _post_login(client, url, name)
                self.assertEqual(json.loads(resp.content)['status'], 500, (url, name))
                self.assertFalse(User.objects.filter(username=name).exists(), (url, name))
                self.assertNotIn('_auth_user_id', client.session, (url, name))

    def test_empty_default_password_still_creates_nothing(self):
        client = Client()
        _post_login(client, '/login/verify', 'admin')
        self.assertEqual(User.objects.count(), 0)
        self.assertNotIn('_auth_user_id', client.session)

    def test_existing_admin_with_correct_password_still_reaches_otp_step(self):
        user = User.objects.create_user('admin', password='correct-horse')
        user.groups.add(_admin_group())
        resp = _post_login(Client(), '/login/verify', 'admin', 'correct-horse')
        body = json.loads(resp.content)
        self.assertEqual(body['status'], 202)
        self.assertEqual(body['redirectUrl'], '/dashboard')

    def test_empty_password_never_authenticates(self):
        # Older releases could create 'admin' with an empty password.
        user = User.objects.create_user('admin', password='')
        user.groups.add(_admin_group())
        for url in self.URLS:
            client = Client()
            resp = _post_login(client, url, 'admin', '')
            self.assertEqual(json.loads(resp.content)['status'], 500, url)
            self.assertNotIn('_auth_user_id', client.session, url)

    def test_existing_admin_with_wrong_password_rejected(self):
        user = User.objects.create_user('admin', password='correct-horse')
        user.groups.add(_admin_group())
        client = Client()
        resp = _post_login(client, '/login/verify', 'admin', 'nope')
        self.assertEqual(json.loads(resp.content)['status'], 500)
        self.assertNotIn('_auth_user_id', client.session)


class BootstrapAdminCommandTests(TestCase):
    def test_requires_password(self):
        with override_settings(ADMIN_DEFAULT_PASSWORD=''), mock.patch.dict(os.environ, {'ADMIN_DEFAULT_PASSWORD': ''}):
            with self.assertRaises(CommandError):
                call_command('bootstrap_admin')
        self.assertFalse(User.objects.filter(username='admin').exists())

    def test_creates_admin_and_groups_idempotently(self):
        with override_settings(ADMIN_DEFAULT_PASSWORD='SetByOperator1!'):
            call_command('bootstrap_admin')
            call_command('bootstrap_admin')
        user = User.objects.get(username='admin')
        self.assertTrue(user.check_password('SetByOperator1!'))
        self.assertTrue(user.groups.filter(name='Admin').exists())
        self.assertFalse(user.is_superuser)
        self.assertFalse(User.objects.filter(username='djangoadmin').exists())
        self.assertEqual(Group.objects.filter(name='Admin').count(), 1)

    def test_superuser_only_with_flag(self):
        with override_settings(ADMIN_DEFAULT_PASSWORD='SetByOperator1!'):
            call_command('bootstrap_admin', '--djangoadmin')
        su = User.objects.get(username='djangoadmin')
        self.assertTrue(su.is_superuser)
        self.assertTrue(su.check_password('SetByOperator1!'))

    def test_existing_user_password_is_not_overwritten(self):
        User.objects.create_user('admin', password='keep-me')
        with override_settings(ADMIN_DEFAULT_PASSWORD='SetByOperator1!'):
            call_command('bootstrap_admin')
        self.assertTrue(User.objects.get(username='admin').check_password('keep-me'))


# --------------------------------------------------------------------------
# deploy-docs-2: notification endpoints need a session and the right role
# --------------------------------------------------------------------------
NOTIFICATION_URLS = [
    ('get', 'notification/'),
    ('post', 'notification/serviceoperation'),
    ('post', 'notification/sendnotification'),
    ('get', 'notification/getallservices'),
    ('post', 'notification/savesettings'),
    ('post', 'notification/save_image/'),
    ('get', 'notification/profile_images/bob/'),
    ('delete', 'notification/delete-profile-image/'),
    ('post', 'notification/sendnotifications'),
    ('post', 'notification/escalapolicy'),
    ('get', 'notification/get_escalation_policies'),
    ('get', 'notification/edit_escalation_policy'),
    ('post', 'notification/delete_escalation_policy'),
    ('post', 'notification/toggle_email_notification'),
    ('post', 'notification/snooze_email_notification'),
    ('get', 'notification/get_devices_by_type'),
    ('get', 'notification/get_device_alert_categories'),
    ('post', 'notification/save_device_alert_policy'),
    ('get', 'notification/get_device_alert_policies'),
    ('get', 'notification/edit_device_alert_policy'),
    ('post', 'notification/delete_device_alert_policy'),
]
ADMIN_ONLY_URLS = [
    ('post', 'notification/serviceoperation'),
    ('post', 'notification/escalapolicy'),
    ('get', 'notification/get_escalation_policies'),
    ('get', 'notification/edit_escalation_policy'),
    ('post', 'notification/delete_escalation_policy'),
    ('post', 'notification/toggle_email_notification'),
    ('post', 'notification/snooze_email_notification'),
    ('post', 'notification/save_device_alert_policy'),
    ('get', 'notification/get_device_alert_policies'),
    ('get', 'notification/edit_device_alert_policy'),
    ('post', 'notification/delete_device_alert_policy'),
]


class NotificationAuthTests(TestCase):
    def test_url_list_covers_every_route(self):
        import notification.urls as nurls
        routed = {str(p.pattern) for p in nurls.urlpatterns}
        listed = {u.split('notification/', 1)[1].replace('bob', '<str:username>') for _, u in NOTIFICATION_URLS}
        self.assertEqual(routed, listed)

    def test_anonymous_is_redirected_and_touches_nothing(self):
        for method, url in NOTIFICATION_URLS:
            with mock.patch('notification.views.connection') as conn:
                resp = getattr(Client(), method)('/' + url, data=json.dumps({'title': 't', 'status': 0}),
                                                 content_type='application/json') if method != 'get' \
                    else Client().get('/' + url)
            self.assertEqual(resp.status_code, 302, (method, url, resp.status_code))
            conn.cursor.assert_not_called()

    def test_csrf_exempt_removed(self):
        from notification import views
        for name in ('toggle_email_notification', 'snooze_email_notification'):
            self.assertFalse(getattr(getattr(views, name), 'csrf_exempt', False), name)

    def test_csrf_enforced_for_logged_in_toggle(self):
        user = User.objects.create_user('root1', password='pw')
        user.groups.add(_admin_group())
        client = Client(enforce_csrf_checks=True)
        client.force_login(user)
        resp = client.post('/notification/toggle_email_notification', data='{"title":"x","status":0}',
                           content_type='application/json')
        self.assertEqual(resp.status_code, 403)

    def test_non_admin_forbidden_on_policy_and_alert_control(self):
        user = User.objects.create_user('viewer', password='pw')
        user.groups.add(Group.objects.create(name='ViewOnly', weightage=20))
        client = Client()
        client.force_login(user)
        for method, url in ADMIN_ONLY_URLS:
            with mock.patch('notification.views.connection') as conn:
                if method == 'get':
                    resp = client.get('/' + url)
                else:
                    resp = client.post('/' + url, data='{"title":"x","status":0}', content_type='application/json')
            self.assertEqual(resp.status_code, 403, (method, url, resp.status_code))
            conn.cursor.assert_not_called()

    def test_admin_can_toggle(self):
        user = User.objects.create_user('root1', password='pw')
        user.groups.add(_admin_group())
        client = Client()
        client.force_login(user)
        with mock.patch('notification.views.connection') as conn:
            cur = conn.cursor.return_value.__enter__.return_value
            cur.fetchone.return_value = (1,)
            resp = client.post('/notification/toggle_email_notification', data='{"title":"x","status":0}',
                               content_type='application/json')
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(json.loads(resp.content)['status'], 200)

    def test_non_admin_cannot_send_notification_to_someone_else(self):
        user = User.objects.create_user('viewer', password='pw')
        user.groups.add(Group.objects.create(name='ViewOnly', weightage=20))
        client = Client()
        client.force_login(user)
        payload = {'data': {'user': 'someone-else', 'title': 't', 'template_type': 'x', 'dataObj': {}}}
        with mock.patch('notification.views.Notification') as notif:
            resp = client.post('/notification/sendnotifications', {'alldata': json.dumps(payload)})
        self.assertEqual(resp.status_code, 403)
        notif.assert_not_called()


# --------------------------------------------------------------------------
# deploy-docs-8: vault seal/unseal must be POST + CSRF protected
# --------------------------------------------------------------------------
class VaultChangeStatusTests(TestCase):
    def _admin_client(self, **kw):
        user = User.objects.create_user('root1', password='pw')
        user.groups.add(_admin_group())
        client = Client(**kw)
        client.force_login(user)
        return client

    def test_get_does_not_change_state(self):
        client = self._admin_client()
        with mock.patch('vault.views.Vault') as vault:
            resp = client.get('/changeStatus?status=Unsealed')
        self.assertEqual(resp.status_code, 405)
        vault.assert_not_called()

    def test_post_with_csrf_token_works(self):
        client = self._admin_client(enforce_csrf_checks=True)
        with mock.patch('vault.views.Vault') as vault:
            resp = client.post('/changeStatus', {'status': 'Unsealed'})
        self.assertEqual(resp.status_code, 403)  # no CSRF token
        vault.assert_not_called()

    def test_post_seals(self):
        client = self._admin_client()
        with mock.patch('vault.views.Vault') as vault:
            vault.return_value.Seal.return_value = {'status': 200, 'data': 'Sealed'}
            resp = client.post('/changeStatus', {'status': 'Unsealed'})
        self.assertEqual(json.loads(resp.content)['status'], 'Sealed')
        vault.return_value.Seal.assert_called_once()

    def test_anonymous_post_redirected(self):
        with mock.patch('vault.views.Vault') as vault:
            resp = Client().post('/changeStatus', {'status': 'Unsealed'})
        self.assertEqual(resp.status_code, 302)
        vault.assert_not_called()


# --------------------------------------------------------------------------
# deploy-docs-5: SECRET_KEY fallback and TLS verification
# --------------------------------------------------------------------------
def _import_settings(env):
    code = (
        "from legacy_tests import harness\n"
        "harness._install_stubs()\n"
        "import LinkedEyeWebProject.settings as s\n"
        "print('SK=' + s.SECRET_KEY)\n"
        "print('VERIFY=' + repr(s.MONITORING_VERIFY_SSL))\n"
        "print('CA=' + repr(getattr(s, 'MONITORING_CA_BUNDLE', None)))\n"
    )
    base = {'PATH': os.environ.get('PATH', ''), 'HOME': tempfile.gettempdir(), 'PYTHONPATH': BACKEND_DIR}
    base.update(env)
    return subprocess.run([PYTHON, '-c', code], cwd=BACKEND_DIR, env=base, capture_output=True, text=True, timeout=120)


class SettingsSecretKeyTests(TestCase):
    def test_production_without_secret_key_refuses_to_start(self):
        res = _import_settings({'DJANGO_DEBUG': 'false'})
        self.assertNotEqual(res.returncode, 0, res.stdout)
        self.assertIn('SECRET_KEY', res.stderr)
        self.assertNotIn('dev-insecure-change-me', res.stdout)

    def test_production_with_placeholder_secret_key_refuses_to_start(self):
        res = _import_settings({'DJANGO_DEBUG': 'false', 'SECRET_KEY': 'dev-insecure-change-me'})
        self.assertNotEqual(res.returncode, 0)

    def test_production_with_real_secret_key_starts(self):
        res = _import_settings({'DJANGO_DEBUG': 'false', 'SECRET_KEY': 'k' * 50})
        self.assertEqual(res.returncode, 0, res.stderr)
        self.assertIn('SK=' + 'k' * 50, res.stdout)

    def test_debug_may_use_dev_fallback(self):
        res = _import_settings({'DJANGO_DEBUG': 'true'})
        self.assertEqual(res.returncode, 0, res.stderr)

    def test_tls_verification_defaults_on(self):
        res = _import_settings({'DJANGO_DEBUG': 'true'})
        self.assertIn('VERIFY=True', res.stdout)

    def test_tls_verification_can_be_disabled_explicitly(self):
        res = _import_settings({'DJANGO_DEBUG': 'true', 'MONITORING_VERIFY_SSL': 'false'})
        self.assertIn('VERIFY=False', res.stdout)

    def test_ca_bundle_setting(self):
        res = _import_settings({'DJANGO_DEBUG': 'true', 'MONITORING_CA_BUNDLE': '/etc/ssl/private-ca.pem'})
        self.assertIn("CA='/etc/ssl/private-ca.pem'", res.stdout)

    def test_env_example_does_not_disable_tls(self):
        text = open(os.path.join(BACKEND_DIR, '.env.example'), encoding='utf-8').read()
        self.assertNotRegex(text, r'(?m)^MONITORING_VERIFY_SSL\s*=\s*false')


class MonitoringTokenVerifyTests(TestCase):
    def test_verify_ssl_default_true(self):
        from lib.LinkedEyeMonitoring import token
        with override_settings():
            from django.conf import settings
            if hasattr(settings, 'MONITORING_VERIFY_SSL'):
                del settings.MONITORING_VERIFY_SSL
            self.assertIs(token.verify_ssl(), True)

    def test_verify_ssl_uses_ca_bundle(self):
        from lib.LinkedEyeMonitoring import token
        with override_settings(MONITORING_VERIFY_SSL=True, MONITORING_CA_BUNDLE='/etc/ca.pem'):
            self.assertEqual(token.verify_ssl(), '/etc/ca.pem')
        with override_settings(MONITORING_VERIFY_SSL=False, MONITORING_CA_BUNDLE='/etc/ca.pem'):
            self.assertIs(token.verify_ssl(), False)

    def test_token_fetch_verifies_certificate_by_default(self):
        from lib.LinkedEyeMonitoring import token
        from django.conf import settings
        with override_settings():
            if hasattr(settings, 'MONITORING_VERIFY_SSL'):
                del settings.MONITORING_VERIFY_SSL
            with mock.patch.object(token, 'requests') as req, mock.patch.object(token, 'cache') as cache:
                cache.get.return_value = None
                req.post.return_value.ok = True
                req.post.return_value.json.return_value = {'access_token': 'a', 'expires_in': 300}
                token._fetch_client_credentials_token('https://kc/token', 'id', 'sec', 'k')
        self.assertIs(req.post.call_args.kwargs['verify'], True)


# --------------------------------------------------------------------------
# deploy-docs-9: entrypoint must not persist the root token or drop tables
# --------------------------------------------------------------------------
class EntrypointTests(TestCase):
    def setUp(self):
        sys.modules.pop('entrypoint', None)
        self.tmp = tempfile.mkdtemp()
        self.cfg = os.path.join(self.tmp, 'linkedeye_vault.yaml')
        with open(self.cfg, 'w') as fh:
            fh.write("token: ''\n")

    def _load(self):
        sys.path.insert(0, BACKEND_DIR)
        import entrypoint
        entrypoint.ConfigFile = self.cfg
        return entrypoint

    def _vault(self):
        vault = mock.MagicMock()
        vault.GetRootToken.return_value = 'ROOT-TOKEN-VALUE'
        vault.CreateScopedToken.return_value = 'SCOPED-TOKEN-VALUE'
        return vault

    def test_root_token_not_written(self):
        ep = self._load()
        vault = self._vault()
        with mock.patch.object(ep, 'Vault', return_value=vault), \
                mock.patch.object(ep, 'createDatabase'), mock.patch.object(ep, 'makeMigrations'), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop('LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN', None)
            ep.main()
        content = open(self.cfg).read()
        self.assertNotIn('ROOT-TOKEN-VALUE', content)
        self.assertIn('SCOPED-TOKEN-VALUE', content)

    def test_root_token_only_with_explicit_opt_in(self):
        ep = self._load()
        vault = self._vault()
        with mock.patch.object(ep, 'Vault', return_value=vault), \
                mock.patch.object(ep, 'createDatabase'), mock.patch.object(ep, 'makeMigrations'), \
                mock.patch.dict(os.environ, {'LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN': 'true'}):
            ep.main()
        self.assertIn('ROOT-TOKEN-VALUE', open(self.cfg).read())

    def test_scoped_token_failure_fails_closed(self):
        ep = self._load()
        vault = self._vault()
        vault.CreateScopedToken.side_effect = Exception('boom')
        with mock.patch.object(ep, 'Vault', return_value=vault), \
                mock.patch.object(ep, 'createDatabase'), mock.patch.object(ep, 'makeMigrations'):
            os.environ.pop('LINKEDEYE_ENTRYPOINT_PERSIST_ROOT_TOKEN', None)
            with self.assertRaises(Exception):
                ep.main()
        self.assertNotIn('ROOT-TOKEN-VALUE', open(self.cfg).read())

    def _geo_conn(self, existing_rows):
        conn = mock.MagicMock()
        cursor = conn.cursor.return_value
        cursor.fetchone.return_value = (existing_rows,)
        cursor.lastrowid = 1
        return conn, cursor

    def test_geo_tables_not_dropped_when_populated(self):
        ep = self._load()
        conn, cursor = self._geo_conn(250)
        with mock.patch.object(ep.mysql.connector, 'connect', return_value=conn):
            ep.createCountryStateTable()
        sql = ' '.join(str(c.args[0]) for c in cursor.execute.call_args_list).upper()
        self.assertNotIn('DROP TABLE', sql)
        self.assertNotIn('INSERT', sql)

    def test_geo_tables_loaded_in_single_transaction_when_empty(self):
        ep = self._load()
        conn, cursor = self._geo_conn(0)
        frame = mock.MagicMock()
        frame.iterrows.return_value = iter([(0, mock.MagicMock(countryshortname='IN', countryname='India'))])
        with mock.patch.object(ep.mysql.connector, 'connect', return_value=conn), \
                mock.patch.object(ep.pd, 'read_csv', return_value=frame), \
                mock.patch.object(ep.pd, 'isna', return_value=False), \
                mock.patch.object(ep.os.path, 'isfile', return_value=False):
            ep.createCountryStateTable()
        sql = ' '.join(str(c.args[0]) for c in cursor.execute.call_args_list).upper()
        self.assertNotIn('DROP TABLE', sql)
        self.assertIn('CREATE TABLE IF NOT EXISTS COUNTRY', sql)
        self.assertIn('INSERT INTO', sql)
        self.assertEqual(conn.commit.call_count, 1)

    def test_geo_reload_failure_rolls_back(self):
        ep = self._load()
        conn, cursor = self._geo_conn(0)
        with mock.patch.object(ep.mysql.connector, 'connect', return_value=conn), \
                mock.patch.object(ep.pd, 'read_csv', side_effect=IOError('missing csv')):
            ep.createCountryStateTable()
        conn.rollback.assert_called()
        conn.commit.assert_not_called()

    def test_makemigrations_can_be_disabled(self):
        ep = self._load()
        with mock.patch.object(ep.subprocess, 'run') as run, \
                mock.patch.dict(os.environ, {'LINKEDEYE_ENTRYPOINT_MAKEMIGRATIONS': 'false'}):
            ep.makeMigrations()
        called = [c.args[0] for c in run.call_args_list]
        self.assertFalse(any('makemigrations' in cmd for cmd in called), called)
        self.assertTrue(any('migrate' in cmd for cmd in called), called)

    def test_importing_entrypoint_has_no_side_effects(self):
        # A bare import must not contact Vault or the database.
        sys.modules.pop('entrypoint', None)
        sys.path.insert(0, BACKEND_DIR)
        import entrypoint  # noqa: F401  (raises on the old module-level main flow)


class VaultScopedTokenTests(TestCase):
    def test_create_scoped_token(self):
        from lib.LinkedEyeVault.Vault import Vault
        v = Vault.__new__(Vault)
        v.isUnsealed = True
        v._postman = mock.MagicMock(return_value={
            'status': 200, 'msg': json.dumps({'auth': {'client_token': 's.scoped', 'policies': ['p']}})})
        self.assertEqual(v.CreateScopedToken('linkedeye_secret_acl'), 's.scoped')
        args, kwargs = v._postman.call_args
        self.assertEqual(args[0], '/v1/auth/token/create')
        self.assertEqual(kwargs['data']['policies'], ['linkedeye_secret_acl'])
        self.assertNotIn('root', kwargs['data']['policies'])

    def test_create_scoped_token_failure_raises(self):
        from lib.LinkedEyeVault.Vault import Vault
        v = Vault.__new__(Vault)
        v.isUnsealed = True
        v._postman = mock.MagicMock(return_value={'status': 403, 'msg': 'denied'})
        with self.assertRaises(Exception):
            v.CreateScopedToken('p')


# --------------------------------------------------------------------------
# deploy-docs-3 / 10 / 14: repository hygiene
# --------------------------------------------------------------------------
def _read(rel):
    with open(os.path.join(BACKEND_DIR, rel), encoding='utf-8', errors='replace') as fh:
        return fh.read()


class RepositoryHygieneTests(TestCase):
    @staticmethod
    def _hashes():
        path = os.path.join(os.path.dirname(__file__), 'leaked.sha256')
        return {l.strip() for l in open(path) if l.strip() and not l.startswith('#')}

    def test_no_known_leaked_value_in_docs(self):
        hashes = self._hashes()
        self.assertGreaterEqual(len(hashes), 3)
        for rel in ('SECRETS_AUDIT.md', 'lib/LinkedEyeValidation/Read.me'):
            for token in re.split(r"[\s`'\"|,;()<>]+", _read(rel)):
                self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), hashes, rel)

    def test_secrets_audit_marks_values_for_rotation(self):
        self.assertIn('<rotate-and-remove>', _read('SECRETS_AUDIT.md'))

    def test_other_read_me_files_redacted(self):
        hashes = self._hashes()
        for root, _dirs, files in os.walk(BACKEND_DIR):
            if '.git' in root or 'node_modules' in root:
                continue
            for fn in files:
                if fn.lower() in ('read.me', 'readme.md', 'readme.txt', 'readme.html'):
                    text = open(os.path.join(root, fn), encoding='utf-8', errors='replace').read()
                    for token in re.split(r"[\s`'\"|,;()<>]+", text):
                        self.assertNotIn(hashlib.sha256(token.encode()).hexdigest(), hashes, os.path.join(root, fn))

    def test_stale_requirement_files(self):
        # The obsolete lists may only forward to the maintained requirements.txt.
        for rel in ('requirement.txt', 'requirements_win.txt'):
            path = os.path.join(BACKEND_DIR, rel)
            if not os.path.exists(path):
                continue
            lines = [l.strip() for l in _read(rel).splitlines() if l.strip() and not l.startswith('#')]
            self.assertEqual(lines, ['-r requirements.txt'], rel)

    def test_keycloak_dev_compose_has_no_known_credentials(self):
        text = _read('docker-compose.keycloak.yml')
        self.assertNotIn('admin123456', text)
        self.assertIn('${KEYCLOAK_ADMIN_PASSWORD:?', text)
        self.assertRegex(text, r'"127\.0\.0\.1:8083:8080"')

    def test_keycloak_scripts_have_no_known_credentials(self):
        for rel in ('scripts/keycloak-setup.ps1', 'scripts/run-dev.ps1'):
            text = _read(rel)
            self.assertNotIn('admin123456', text, rel)
            self.assertNotIn('linkedeye-kc-dev-secret-2026', text, rel)

    def test_security_changes_documented(self):
        text = _read('CHANGES-SECURITY.md')
        for ident in ('deploy-docs-1', 'deploy-docs-2', 'deploy-docs-3', 'deploy-docs-5',
                      'deploy-docs-8', 'deploy-docs-9', 'deploy-docs-10', 'deploy-docs-14'):
            self.assertIn(ident, text)
