"""
Django settings for LinkedEyeWebProject project.

Based on 'django-admin startproject' using Django 2.1.2.

For more information on this file, see
https://docs.djangoproject.com/en/2.1/topics/settings/

For the full list of settings and their values, sees
https://docs.djangoproject.com/en/2.1/ref/settings/
"""

import os
import posixpath
import ast

from LinkedEyeWebProject.env_config import load_env_file, env, env_int, service_url

# Build paths inside the project like this: os.path.join(BASE_DIR, ...)
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.abspath(os.path.join(BASE_DIR, '..', 'frontend'))
# Docker images are built from the repository root and place the frontend under
# /usr/src/frontend; local development reads the sibling repository folder.
if not os.path.isdir(FRONTEND_DIR):
    FRONTEND_DIR = os.path.join(BASE_DIR, 'frontend')

# Load optional `.env` from project root (directory containing manage.py)
load_env_file(BASE_DIR)

from lib.LinkedEyeVault.AppSecrets import get_app_secret

def _truthy_env(key, default='false'):
    return env(key, default).lower() in ('1', 'true', 'yes', 'on')


# Quick-start development settings - 
# See https://docs.djangoproject.com/en/2.1/howto/deployment/checklist/

# SECURITY WARNING: keep the secret key used in production secret!
SECRET_KEY = get_app_secret('SECRET_KEY', env_var='SECRET_KEY', default='dev-insecure-change-me')
# Shared bearer token for the read-only summary consumed by the new console.
DASHBOARD_API_TOKEN = env('DASHBOARD_API_TOKEN', '').strip()

# SECURITY WARNING: don't run with debug turned on in production!
DEBUG = _truthy_env('DJANGO_DEBUG', 'false')

# SECURITY: the placeholder below is published in this repository. Outside DEBUG a
# missing (or placeholder) SECRET_KEY must stop the process instead of silently signing
# sessions, CSRF tokens and password-reset links with a public value.
_INSECURE_SECRET_KEYS = ('', 'dev-insecure-change-me', 'changeme', 'change-me')
if not DEBUG and (SECRET_KEY or '').strip() in _INSECURE_SECRET_KEYS:
    from django.core.exceptions import ImproperlyConfigured
    raise ImproperlyConfigured(
        "SECRET_KEY is not set (or is the published placeholder). Provide it through the Vault "
        "app-secrets bundle or the SECRET_KEY environment variable, or set DJANGO_DEBUG=true for local development."
    )

# No client hostnames in source. DEBUG allows localhost; production REQUIRES
# LE_ALLOWED_HOSTS to be set per deployment. An unset value leaves the list empty
# so Django rejects all hosts (fails loud) — a misconfigured deployment is obvious
# instead of silently shipping another client's hostnames.
_allowed_hosts = ast.literal_eval(env('LE_ALLOWED_HOSTS', '[]'))
ALLOWED_HOSTS = _allowed_hosts if _allowed_hosts else (['127.0.0.1', 'localhost'] if DEBUG else [])
 
_cors_allowed = ast.literal_eval(env('LE_CORS_ALLOWED_ORIGINS', '[]'))
CORS_ORIGIN_ALLOW_ALL = _truthy_env('LE_CORS_ALLOW_ALL', 'true' if DEBUG else 'false')
if _cors_allowed:
    CORS_ALLOWED_ORIGINS = _cors_allowed
 
# Same as ALLOWED_HOSTS — set LE_CSRF_TRUSTED_ORIGINS per deployment (with the
# https:// scheme prefix). No client origins in source; empty when unset.
_csrf_trusted = ast.literal_eval(env('LE_CSRF_TRUSTED_ORIGINS', '[]'))
CSRF_TRUSTED_ORIGINS = _csrf_trusted if _csrf_trusted else []
 
X_FRAME_OPTIONS = env('LE_X_FRAME_OPTIONS', 'SAMEORIGIN')
 
# Application references
# https://docs.djangoproject.com/en/2.1/ref/settings/#std:setting-INSTALLED_APPS

INSTALLED_APPS = [
    'app',
    'notification',
    'useronboard',
    'auditlogs',
    'applications',
    'comparision',
    'lesites',
    'analytics',
    'onboarding',
    'autodiscover',
    'vault',
    'login',
    'userprofile',
    'bodeodstatus',
    'hsonboarding',
    'addservice',
    'allonboard',
    'entity',
    'dashboard',
    'sitehealth',
    'incidents',
    # Add your apps here to enable them
    'django.contrib.admin',
    'django.contrib.auth',
    'django.contrib.contenttypes',
    'django.contrib.sessions',
    'django.contrib.messages',
    'django.contrib.staticfiles',
    'corsheaders',
    #added new
    'django.contrib.sites',
    'allauth',
    'allauth.account',
    'allauth.socialaccount',
    'allauth.socialaccount.providers.google',
    'crispy_forms',
    #AZURE AD
    'ms_identity_web',
]


AUTHENTICATION_BACKENDS=(
    'django.contrib.auth.backends.ModelBackend',
    #used for social authentication

    'allauth.account.auth_backends.AuthenticationBackend',
    )

SITE_ID = int(env('GOOGLE_SITE_ID', '0'))
ACCOUNT_EMAIL_VERIFICATION='none'#previously none
#ACCOUNT_EMAIL_REQUIRED='false'#previously not added
#ACCOUNT_EMAIL_CONFIRMATION_EXPIRE_DAYS=30#previously not added
CRISPY_TEMPLATE_PACK='bootstrap4'
LOGIN_REDIRECT_URL='/login/google_verify'
ACCOUNT_LOGOUT_REDIRECT_URL= '/'
#SOCIALACCOUNT_QUERY_EMAIL='true'
#SOCIALACCOUNT_LOGIN_ON_GET=True
ACCOUNT_LOGIN_ERROR = ("Sorry, only users with email addresses from {allowed_domains} are allowed to sign in.")
ACCOUNT_SIGNUP_ERROR = ("Sorry, only users with email addresses from {allowed_domains} are allowed to sign up.")
ACCOUNT_DEFAULT_HTTP_PROTOCOL = 'http'  # Force allauth to use HTTP locally
SOCIALACCOUNT_PROVIDERS = {
    'google': {
        'SCOPE': ['profile', 'email'],
        'AUTH_PARAMS': {'access_type': 'online'},
        'APP': {
            'client_id': env('GOOGLE_CLIENT_ID', 'NONE'),
            'secret': get_app_secret('GOOGLE_SECRET', env_var='GOOGLE_SECRET', default=''),
            'key': ''
        },
        'DOMAINS': ast.literal_eval(env('GOOGLE_ALLOW_DOMAINS', "[]")),
    }
}
# Middleware framework
SOCIALACCOUNT_ADAPTER = 'app.adapter.LESocialLoginAdapter'


def _env_flag(key, default='false'):
    return _truthy_env(key, default)


# ── Keycloak SSO (optional — parallel to existing username/Google/Azure login) ──
KEYCLOAK_ENABLED = _env_flag('KEYCLOAK_ENABLED')
KEYCLOAK_SERVER_URL = env('KEYCLOAK_SERVER_URL', '').rstrip('/')
KEYCLOAK_REALM = env('KEYCLOAK_REALM', 'linkedeye')
KEYCLOAK_CLIENT_ID = env('KEYCLOAK_CLIENT_ID', 'linkedeye-web')
KEYCLOAK_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_CLIENT_SECRET', env_var='KEYCLOAK_CLIENT_SECRET', default=''
)

if KEYCLOAK_ENABLED and KEYCLOAK_SERVER_URL and KEYCLOAK_CLIENT_ID and KEYCLOAK_CLIENT_SECRET:
    AUTHENTICATION_BACKENDS = (
        'login.keycloak_backend.LinkedEyeKeycloakBackend',
    ) + AUTHENTICATION_BACKENDS

    _kc_oidc_base = (
        f'{KEYCLOAK_SERVER_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect'
    )
    OIDC_OP_AUTHORIZATION_ENDPOINT = f'{_kc_oidc_base}/auth'
    OIDC_OP_TOKEN_ENDPOINT = f'{_kc_oidc_base}/token'
    OIDC_OP_USER_ENDPOINT = f'{_kc_oidc_base}/userinfo'
    OIDC_OP_JWKS_ENDPOINT = f'{_kc_oidc_base}/certs'
    OIDC_OP_LOGOUT_ENDPOINT = f'{_kc_oidc_base}/logout'

    OIDC_RP_CLIENT_ID = KEYCLOAK_CLIENT_ID
    OIDC_RP_CLIENT_SECRET = KEYCLOAK_CLIENT_SECRET
    OIDC_RP_SIGN_ALGO = 'RS256'
    OIDC_RP_SCOPES = 'openid email profile roles'
    OIDC_VERIFY_SSL = _env_flag('KEYCLOAK_VERIFY_SSL', 'true')
    OIDC_CREATE_USER = True
    OIDC_STORE_ACCESS_TOKEN = True
    OIDC_STORE_ID_TOKEN = True

    _portal_url = env('PORTAL_URL', '').rstrip('/')
    if _portal_url:
        OIDC_RP_REDIRECT_URI = f'{_portal_url}/auth/oidc/callback/'
        OIDC_RP_POST_LOGOUT_REDIRECT_URI = f'{_portal_url}/'
else:
    KEYCLOAK_ENABLED = False

# ── Keycloak Admin sync (mirror app-onboarded users into Keycloak) ──
# The portal is the source of truth for user creation. When an admin onboards a
# user, the same user (same password, mapped realm role, site/subsite attributes)
# is pushed into Keycloak via the Admin API so SSO and MySQL fallback share one
# password. Uses a dedicated confidential service-account client with the
# realm-management manage-users/view-users/query-users roles.
KEYCLOAK_ADMIN_CLIENT_ID = env('KEYCLOAK_ADMIN_CLIENT_ID', 'linkedeye-admin')
KEYCLOAK_ADMIN_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_ADMIN_CLIENT_SECRET', env_var='KEYCLOAK_ADMIN_CLIENT_SECRET', default=''
)
# On by default once the admin client secret is present; force off with =false.
KEYCLOAK_ADMIN_SYNC_ENABLED = _env_flag(
    'KEYCLOAK_ADMIN_SYNC_ENABLED', 'true' if KEYCLOAK_ADMIN_CLIENT_SECRET else 'false'
)
# 'delete' mirrors an app delete into Keycloak; 'disable' keeps the user disabled.
KEYCLOAK_DELETE_MODE = env('KEYCLOAK_DELETE_MODE', 'delete')
# Realm role names that mirror the LinkedEye Django groups (must exist in the realm).
KEYCLOAK_KNOWN_ROLES = [
    'Admin', 'ViewOnly', 'Management', 'Onboard', 'UserView',
    'TechInfra', 'Risk', 'TradeSupport', 'Google', 'O365', 'DjangoAdmin',
]

# Middleware framework
# https://docs.djangoproject.com/en/2.1/topics/http/middleware/


#THE BELOW 2 LINES ARE NECESSARY FOR CACHE REMOVAL IN SERVERS, NOT NEEDED FOR DEV. THEY SHOULD BE INSIDE MIDDLEWARE.

#'lesites.middleware.NoCacheMiddleware',
#'lesites.middleware.NoCacheStaticFilesMiddleware',
MIDDLEWARE = [
 'corsheaders.middleware.CorsMiddleware',
    'django.middleware.security.SecurityMiddleware',
    'whitenoise.middleware.WhiteNoiseMiddleware',
    'django.contrib.sessions.middleware.SessionMiddleware',
    'django.middleware.common.CommonMiddleware',
    'django.middleware.csrf.CsrfViewMiddleware',
    'django.contrib.auth.middleware.AuthenticationMiddleware',
    'LinkedEyeWebProject.middleware.KeycloakOTPGateMiddleware',
    'django.contrib.messages.middleware.MessageMiddleware',
    'django.middleware.clickjacking.XFrameOptionsMiddleware',
 'allauth.account.middleware.AccountMiddleware',
    

]
#'django.middleware.clickjacking.XFrameOptionsMiddleware',

ROOT_URLCONF = 'LinkedEyeWebProject.urls'

# Template configuration
# https://docs.djangoproject.com/en/2.1/topics/templates/
TEMPLATES = [
    {
        'BACKEND': 'django.template.backends.django.DjangoTemplates',
        'DIRS': [os.path.join(FRONTEND_DIR, 'templates')],
        'APP_DIRS': True,
        'OPTIONS': {
            'context_processors': [
                'django.template.context_processors.debug',
                'django.template.context_processors.request',
                'django.contrib.auth.context_processors.auth',
                'django.contrib.messages.context_processors.messages',
            ],
        },
    },
]

WSGI_APPLICATION = 'LinkedEyeWebProject.wsgi.application'
# Database
# https://docs.djangoproject.com/en/2.1/ref/settings/#databases
DATABASES = {
    'default': {
        'ENGINE': 'django.db.backends.mysql',
        'NAME': env('MYSQL_DB_NAME', 'linkedeye'),
        'USER': env('MYSQL_DB_USER', 'root'),
        'PASSWORD': get_app_secret('MYSQL_DB_PASS', env_var='MYSQL_DB_PASS', default=''),
        'HOST': env('MYSQL_DB_HOST', 'mysql.fs-linkedeye'),
        'PORT': env('MYSQL_DB_PORT', '3306'),
    }
}

ELASTIC_HOST = env('ELASTIC_HOST', 'elasticsearch.fs-linkedeye')
ELASTIC_PORT = env('ELASTIC_PORT', '9200')
ELASTIC_URL = f'{ELASTIC_HOST}:{ELASTIC_PORT}'
#print('ELASTIC_URL--->'+ELASTIC_URL)
ELASTICSEARCH_DSL = {
    'default': {
        'hosts': [ELASTIC_URL],
    }
}


# Password validation
# https://docs.djangoproject.com/en/2.1/ref/settings/#auth-password-validators
AUTH_PASSWORD_VALIDATORS = [
    {
        'NAME': 'django.contrib.auth.password_validation.UserAttributeSimilarityValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.MinimumLengthValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.CommonPasswordValidator',
    },
    {
        'NAME': 'django.contrib.auth.password_validation.NumericPasswordValidator',
    },
]

# Internationalization
# https://docs.djangoproject.com/en/2.1/topics/i18n/
LANGUAGE_CODE = 'en-us'
TIME_ZONE = 'UTC'
USE_I18N = True
USE_TZ = True

# Static files (CSS, JavaScript, Images)
# https://docs.djangoproject.com/en/2.1/howto/static-files/

MEDIA_URL = '/media/'
#STATIC_URL = f'/static/{VERSION}/'

STATIC_URL = '/static/'
STATICFILES_DIRS = [os.path.join(FRONTEND_DIR, 'static')]
# Use a dedicated collectstatic target directory for WhiteNoise.
STATIC_ROOT = os.path.join(BASE_DIR, 'staticfiles')

# WhiteNoise serves static assets directly from Django/Gunicorn without Nginx.
STORAGES = {
    "default": {
        "BACKEND": "django.core.files.storage.FileSystemStorage",
    },
    "staticfiles": {
        "BACKEND": "whitenoise.storage.CompressedStaticFilesStorage",
    },
}
# STATICFILES_STORAGE removed -- STORAGES["staticfiles"]["BACKEND"] above (Django
# 4.2+ setting) already covers this and takes precedence; keeping both was legacy
# redundancy from when this project targeted Django 3.2.

# --- DEV ITERATION MODE (DEBUG only: JS/CSS quick-edit workflow) ---
# AUTOREFRESH=True -> WhiteNoise stat's every file per request -> edits visible instantly
# MAX_AGE=0        -> Cache-Control: no-store -> browsers don't cache static files
# In production (DEBUG=False) WhiteNoise defaults apply (cached stat, 60s max-age).
if DEBUG:
    WHITENOISE_AUTOREFRESH = True
    WHITENOISE_MAX_AGE = 0
# --- END DEV ITERATION MODE ---

#print('BASE_DIR--->'+BASE_DIR)
#print('BASE_DIR.split(os.path.sep)--->{}'.format(BASE_DIR.split(os.path.sep)))
#print('STATIC_URL--->'+STATIC_URL)
#print('STATIC_ROOT--->'+STATIC_ROOT)



MEDIA_ROOT = os.path.join(BASE_DIR, 'media')
#login required function

LOGIN_URL='/'

# Security headers/cookies (production-safe defaults, overridable by env)
SESSION_COOKIE_SECURE = _env_flag('LE_SESSION_COOKIE_SECURE', 'true' if not DEBUG else 'false')
CSRF_COOKIE_SECURE = _env_flag('LE_CSRF_COOKIE_SECURE', 'true' if not DEBUG else 'false')
SECURE_SSL_REDIRECT = _env_flag('LE_SECURE_SSL_REDIRECT', 'true' if not DEBUG else 'false')
SECURE_HSTS_SECONDS = env_int('LE_SECURE_HSTS_SECONDS', 31536000 if not DEBUG else 0)
SECURE_HSTS_INCLUDE_SUBDOMAINS = _env_flag('LE_SECURE_HSTS_INCLUDE_SUBDOMAINS', 'true' if not DEBUG else 'false')
SECURE_HSTS_PRELOAD = _env_flag('LE_SECURE_HSTS_PRELOAD', 'true' if not DEBUG else 'false')
SECURE_PROXY_SSL_HEADER = ('HTTP_X_FORWARDED_PROTO', 'https') if _env_flag('LE_TRUST_X_FORWARDED_PROTO', 'true') else None

# Service endpoints — set via environment or `.env` (see `.env.example`)
POSTGRES_USER = env('POSTGRES_USER', 'linkedeye')
POSTGRES_PASS = get_app_secret('POSTGRES_PASS', env_var='POSTGRES_PASS', default='')
POSTGRES_HOST = env('POSTGRES_HOST', 'postgres.fs-linkedeye')
POSTGRES_PORT = env('POSTGRES_PORT', '5432')
POSTGRES_DB_NAME = env('POSTGRES_DB_NAME', 'linkedeye')
POSTGRES_SUPERSET_DB = env('POSTGRES_SUPERSET_DB', 'superset')

ANALYTICS_DASHBOARD_USER = env('ANALYTICS_DASHBOARD_USER', 'linkedeyedashboard')
APPRISE_HOST = f"{env('APPRISE_HOST', 'apprise.fs-linkedeye')}:{env('APPRISE_PORT', '8000')}"

NEO4J_HOST = env('NEO4J_HOST', 'neo4j.fs-linkedeye')
NEO4J_PORT = env('NEO4J_PORT', '7687')
NEO4J_REST_PORT = env('NEO4J_REST_PORT', '7474')
NEO4J_USER = env('NEO4J_USER', 'neo4j')
NEO4J_PASS = get_app_secret('NEO4J_PASS', env_var='NEO4J_PASS', default='')

VAULT_SCHEME = env('VAULT_SCHEME', 'http')
VAULT_HOST = env('VAULT_HOST', 'vault.fs-linkedeye')
VAULT_PORT = env('VAULT_PORT', '8200')
VAULT_URL = service_url(VAULT_SCHEME, VAULT_HOST, VAULT_PORT)

WEBSOCKET_URL = env('WEBSOCKET_PREFIX_URL', '') + 'ws'
WEBSOCKET_STATS_IP = env('WEBSOCKET_STATS_IP', '')

ANALYTICS_DASHBOARD_PREFIXURL = env('ANALYTICS_DASHBOARD_PREFIX_URL', '')
PORTAL_URL = env('PORTAL_URL', '')
LINKEDEYE_EMAIL = env('LINKEDEYE_EMAIL', '')
LINKEDEYE_EMAIL_APPKEY = get_app_secret('LINKEDEYE_EMAIL_APPKEY', env_var='LINKEDEYE_EMAIL_APPKEY', default='')
ELASTIC_USER = env('ELASTIC_USER', 'elastic')
ELASTIC_PASS = get_app_secret('ELASTIC_PASS', env_var='ELASTIC_PASS', default='')
PROMETHEUS_USERNAME = env('PROMETHEUS_USERNAME', 'prometheus')
PROMETHEUS_PASSWORD = get_app_secret('PROMETHEUS_PASSWORD', env_var='PROMETHEUS_PASSWORD', default='')
# The prometheus_proxy rewrites a public Prometheus URL to the in-cluster service
# (the public URL isn't routable from inside the pod). Purely env-driven — no
# hostnames in source; set both per environment (see .env.example) to enable the
# rewrite, otherwise it is a no-op.
PROMETHEUS_EXTERNAL_URL = env('PROMETHEUS_EXTERNAL_URL', '')
PROMETHEUS_INTERNAL_URL = env('PROMETHEUS_INTERNAL_URL', '')
GRAFANA_USERNAME = env('GRAFANA_USERNAME', 'grafana')
GRAFANA_PASSWORD = get_app_secret('GRAFANA_PASSWORD', env_var='GRAFANA_PASSWORD', default='')

# ── Monitoring auth: how Django authenticates to Grafana/Prometheus ──
# 'bearer' → Keycloak client-credentials token sent as Authorization: Bearer to
#            the oauth2-proxy in front of Grafana/Prometheus (the current infra).
# 'basic'  → legacy HTTP Basic Auth straight to Grafana/Prometheus.
# Grafana and Prometheus each have their own confidential Keycloak client
# (Service Accounts enabled), separate from the interactive-SSO client above.
KEYCLOAK_GRAFANA_CLIENT_ID = env('KEYCLOAK_GRAFANA_CLIENT_ID', 'grafana')
KEYCLOAK_GRAFANA_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_GRAFANA_CLIENT_SECRET', env_var='KEYCLOAK_GRAFANA_CLIENT_SECRET', default=''
)
# Per-site Grafana Keycloak clients for multi-site aggregators (e.g. Global
# LE); same shape/behavior as KEYCLOAK_NEO4J_SITE_MAP below.
KEYCLOAK_GRAFANA_SITE_MAP = env('KEYCLOAK_GRAFANA_SITE_MAP', '{}')
KEYCLOAK_PROMETHEUS_CLIENT_ID = env('KEYCLOAK_PROMETHEUS_CLIENT_ID', 'prometheus')
KEYCLOAK_PROMETHEUS_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_PROMETHEUS_CLIENT_SECRET', env_var='KEYCLOAK_PROMETHEUS_CLIENT_SECRET', default=''
)
# Per-site Prometheus Keycloak clients for multi-site aggregators (e.g. Global
# LE); same shape/behavior as KEYCLOAK_NEO4J_SITE_MAP below.
KEYCLOAK_PROMETHEUS_SITE_MAP = env('KEYCLOAK_PROMETHEUS_SITE_MAP', '{}')
# Neo4j REST hosts fronted by oauth2-proxy (public ingress, e.g. a
# *-neo4j.finspot.in hostname) need the same bearer token; internal
# cluster-only Neo4j connections never hit oauth2-proxy and ignore this.
KEYCLOAK_NEO4J_CLIENT_ID = env('KEYCLOAK_NEO4J_CLIENT_ID', 'neo4j')
KEYCLOAK_NEO4J_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_NEO4J_CLIENT_SECRET', env_var='KEYCLOAK_NEO4J_CLIENT_SECRET', default=''
)
# Per-site Neo4j Keycloak clients for multi-site aggregators (e.g. Global LE):
# JSON {host: SITE_CODE}; token.py then resolves
# KEYCLOAK_NEO4J_<SITE_CODE>_CLIENT_ID/CLIENT_SECRET/TOKEN_URL via get_app_secret.
# Config-only (env/Vault) -- adding a site needs no code change. Empty/unmatched
# hosts fall back to the single KEYCLOAK_NEO4J_CLIENT_* client above.
KEYCLOAK_NEO4J_SITE_MAP = env('KEYCLOAK_NEO4J_SITE_MAP', '{}')
# Same, for Elasticsearch hosts fronted by oauth2-proxy (e.g. a public
# *-elastic.finspot.in ingress). The default singleton client (internal-only
# host) never uses this.
KEYCLOAK_ELASTIC_CLIENT_ID = env('KEYCLOAK_ELASTIC_CLIENT_ID', 'elastic')
KEYCLOAK_ELASTIC_CLIENT_SECRET = get_app_secret(
    'KEYCLOAK_ELASTIC_CLIENT_SECRET', env_var='KEYCLOAK_ELASTIC_CLIENT_SECRET', default=''
)
# Per-site Elasticsearch Keycloak clients for multi-site aggregators (e.g.
# Global LE); same shape/behavior as KEYCLOAK_NEO4J_SITE_MAP above --
# analytics/views.py's search_elasticsearch/export_to_excel/export_to_pdf
# resolve this via token.elastic_credentials(entity_host).
KEYCLOAK_ELASTIC_SITE_MAP = env('KEYCLOAK_ELASTIC_SITE_MAP', '{}')
# Shared token endpoint — defaults to the same Keycloak server/realm as the SSO
# client, overridable if the monitoring clients live in a different realm.
KEYCLOAK_MONITORING_TOKEN_URL = env(
    'KEYCLOAK_MONITORING_TOKEN_URL',
    f'{KEYCLOAK_SERVER_URL}/realms/{KEYCLOAK_REALM}/protocol/openid-connect/token'
    if KEYCLOAK_SERVER_URL else ''
)
# Optional scope requested in the client-credentials call (some setups need one
# to get the right audience for oauth2-proxy).
KEYCLOAK_MONITORING_SCOPE = env('KEYCLOAK_MONITORING_SCOPE', '')
# Default to bearer whenever a monitoring client secret is present, else basic.
MONITORING_AUTH_MODE = env(
    'MONITORING_AUTH_MODE',
    'bearer' if (KEYCLOAK_GRAFANA_CLIENT_SECRET or KEYCLOAK_PROMETHEUS_CLIENT_SECRET) else 'basic'
)
# TLS verification for outbound calls to Keycloak / oauth2-proxy / monitoring.
# Verification is ON unless MONITORING_VERIFY_SSL=false is set explicitly. For private CAs
# set MONITORING_CA_BUNDLE to a PEM bundle path instead of switching verification off.
MONITORING_VERIFY_SSL = _env_flag('MONITORING_VERIFY_SSL', 'true')
MONITORING_CA_BUNDLE = env('MONITORING_CA_BUNDLE', '').strip()
TOTP_MASTER_KEY = get_app_secret('TOTP_MASTER_KEY', env_var='TOTP_MASTER_KEY', default='')
ADMIN_DEFAULT_PASSWORD = get_app_secret('ADMIN_DEFAULT_PASSWORD', env_var='ADMIN_DEFAULT_PASSWORD', default='')

#AZURE ACTIVE DIRECTORY AUTHENTICATION CONFIGURATION

from login.aad_loader import load_aad_config
from ms_identity_web import IdentityWebPython

AAD_CONFIG = load_aad_config(BASE_DIR, get_app_secret)
MS_IDENTITY_WEB = IdentityWebPython(AAD_CONFIG)
ERROR_TEMPLATE = 'auth/{}.html' # for rendering 401 or other errors from msal_middleware
MIDDLEWARE.append('ms_identity_web.django.middleware.MsalMiddleware')

REDIS_HOST = env('REDIS_HOST', 'redis.fs-linkedeye')
REDIS_PORT = env('REDIS_PORT', '6379')
REDIS_USER = get_app_secret('REDIS_USER', env_var='REDIS_USER', default='') or ''
REDIS_PASSWORD = get_app_secret('REDIS_PASSWORD', env_var='REDIS_PASSWORD', default='')

# Redis 6+ ACL: include username in the URL when set (`redis://user:pass@host`).
# Empty user preserves the legacy `redis://:pass@host` form.
if REDIS_PASSWORD:
    _redis_auth = f"{REDIS_USER}:{REDIS_PASSWORD}@" if REDIS_USER else f":{REDIS_PASSWORD}@"
else:
    _redis_auth = ""
_default_redis_cache_url = f"redis://{_redis_auth}{REDIS_HOST}:{REDIS_PORT}/1"

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": env('REDIS_CACHE_URL', _default_redis_cache_url),
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "PASSWORD": REDIS_PASSWORD if REDIS_PASSWORD else None,
            "USERNAME": REDIS_USER if REDIS_USER else None,
            "COMPRESSOR": "django_redis.compressors.zlib.ZlibCompressor",
            "SOCKET_CONNECT_TIMEOUT": 5,
            "SOCKET_TIMEOUT": 5,
            "CONNECTION_POOL_KWARGS": {
                "max_connections": 50,
                "retry_on_timeout": True,
            },
        },
        "KEY_PREFIX": "le",
        "VERSION": 1,
    }
}

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"
SESSION_COOKIE_AGE = 28800  # 8 hours (one workday) from last activity
SESSION_SAVE_EVERY_REQUEST = True  # Extend session on each request (8 hours from last activity)
SESSION_EXPIRE_AT_BROWSER_CLOSE = True  # Logout when browser is closed
 
