"""Standalone harness for the legacy backend security tests.

The legacy project needs MySQL, Redis, Vault, Neo4j and many third-party
packages. These tests only need Django itself, so any third-party module that
is not installed is replaced by a MagicMock and the database is in-memory
SQLite without migrations. Run from ``backend/``::

    python -m unittest legacy_tests.test_security -v
"""
import importlib
import os
import sys
from unittest import mock

BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

class _StubFinder:
    """Last-resort import hook: any module that cannot be found becomes a MagicMock.

    It is appended to sys.meta_path, so real packages and project modules always win.
    """

    _PROJECT = ('app', 'notification', 'useronboard', 'auditlogs', 'applications',
                'comparision', 'lesites', 'analytics', 'onboarding', 'autodiscover',
                'vault', 'login', 'userprofile', 'bodeodstatus', 'hsonboarding',
                'addservice', 'allonboard', 'entity', 'dashboard', 'sitehealth',
                'incidents', 'lib', 'LinkedEyeWebProject', 'legacy_tests', 'django')

    def find_spec(self, name, path=None, target=None):
        top = name.split('.')[0]
        if top in self._PROJECT:
            return None
        import importlib.machinery
        return importlib.machinery.ModuleSpec(name, self, is_package=True)

    def create_module(self, spec):
        m = mock.MagicMock(name=spec.name)
        m.__path__ = []
        m.__name__ = spec.name
        m.__spec__ = spec
        m.__loader__ = self
        return m

    def exec_module(self, module):
        pass


def _install_stubs():
    if not any(isinstance(f, _StubFinder) for f in sys.meta_path):
        sys.meta_path.append(_StubFinder())


def setup():
    os.environ.setdefault('SECRET_KEY', 'legacy-test-key-0123456789abcdef0123456789')
    os.environ.setdefault('DJANGO_DEBUG', 'true')
    _install_stubs()
    import django
    from django.conf import settings
    if settings.configured:
        return
    apps = [
        'django.contrib.auth', 'django.contrib.contenttypes', 'django.contrib.sessions',
        'django.contrib.messages', 'django.contrib.sites',
        'app', 'notification', 'useronboard', 'auditlogs', 'applications', 'comparision',
        'lesites', 'analytics', 'onboarding', 'autodiscover', 'vault', 'login',
        'userprofile', 'bodeodstatus', 'hsonboarding', 'addservice', 'allonboard',
        'entity', 'dashboard', 'sitehealth', 'incidents',
    ]
    settings.configure(
        DEBUG=False,
        SECRET_KEY=os.environ['SECRET_KEY'],
        ALLOWED_HOSTS=['testserver'],
        INSTALLED_APPS=apps,
        DATABASES={'default': {'ENGINE': 'django.db.backends.sqlite3', 'NAME': ':memory:'}},
        # useronboard adds Group.weightage via add_to_class, so auth must be created from models too.
        MIGRATION_MODULES={a.split('.')[-1]: None for a in apps},
        MIDDLEWARE=[
            'django.contrib.sessions.middleware.SessionMiddleware',
            'django.middleware.csrf.CsrfViewMiddleware',
            'django.contrib.auth.middleware.AuthenticationMiddleware',
            'django.contrib.messages.middleware.MessageMiddleware',
        ],
        ROOT_URLCONF='legacy_tests.urls',
        TEMPLATES=[{'BACKEND': 'django.template.backends.django.DjangoTemplates',
                    'APP_DIRS': True, 'OPTIONS': {'context_processors': []}}],
        PASSWORD_HASHERS=['django.contrib.auth.hashers.MD5PasswordHasher'],
        DEFAULT_AUTO_FIELD='django.db.models.AutoField',
        SITE_ID=1,
        STATIC_URL='/static/', STATIC_ROOT='/tmp/legacy-static',
        LOGIN_URL='/',
        ADMIN_DEFAULT_PASSWORD='',
        KEYCLOAK_ENABLED=False,
        SESSION_COOKIE_AGE=3600,
    )
    django.setup()
