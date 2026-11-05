"""Settings override so unit tests can run without MySQL/Redis containers.

Usage:  python manage.py test <label> --settings=test_settings_sqlite
"""

from LinkedEyeWebProject.settings import *  # noqa: F401,F403

DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.sqlite3',
        'NAME': ':memory:',
    }
}

# The real config points at Redis; tests must not need a running instance.
CACHES = {
    'default': {
        'BACKEND': 'django.core.cache.backends.locmem.LocMemCache',
    }
}

PASSWORD_HASHERS = ['django.contrib.auth.hashers.MD5PasswordHasher']

DEBUG = False
