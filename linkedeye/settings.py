"""LinkedEye console settings. All secrets and environment specifics come from
environment variables (see .env.example); nothing sensitive is committed."""
import os
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def env(name, default=None):
    """LINKEDEYE_<name>; an empty value counts as unset, so `LINKEDEYE_PROBE_TIMEOUT=` in an env file keeps the default."""
    value = os.environ.get(f"LINKEDEYE_{name}")
    return default if value in (None, "") else value


def env_list(name, default=""):
    return [x.strip() for x in env(name, default).split(",") if x.strip()]


DEBUG = env("DEBUG", "false").lower() in ("1", "true", "yes")
SECRET_KEY = env("SECRET_KEY") or ("dev-only-insecure-key" if DEBUG else None)
if not SECRET_KEY:
    raise RuntimeError("Set LINKEDEYE_SECRET_KEY (or LINKEDEYE_DEBUG=true for local development).")
if not DEBUG and (len(SECRET_KEY) < 50 or len(set(SECRET_KEY)) < 20
                  or SECRET_KEY.lower().startswith(("change-me", "dev-only", "<replace"))):
    # a published placeholder (.env.example, the dev key) or a short / repetitive key signs sessions and CSRF tokens
    raise RuntimeError("LINKEDEYE_SECRET_KEY is too weak for production: use at least 50 random characters, e.g. "
                       "python3 -c 'import secrets; print(secrets.token_urlsafe(50))'")

ALLOWED_HOSTS = env_list("ALLOWED_HOSTS", "localhost,127.0.0.1" if DEBUG else "")
CSRF_TRUSTED_ORIGINS = env_list("CSRF_TRUSTED_ORIGINS")
# Reverse proxies (IPs or CIDRs) whose X-Forwarded-For is believed for audit source IPs; empty = use the peer.
TRUSTED_PROXIES = env_list("TRUSTED_PROXIES")

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "core",
    "probes",
    "urlchecker",
    "domainmonitor",
    "servicedesk",
    "cmdb",
    "network",
    "telemetry",
    "observe",
    "tcpcheck",
    "bodeod",
    "automation",
    "administration",
    "reports",
    "agent",
    "ipam",
    "dcim",
    "compute",
    "onboarding",
    "k8s",
    "apm",
    "architecture",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",  # serves collected static files (hashed, compressed)
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "core.middleware.LoginRequiredMiddleware",
    "core.middleware.ClientScopeMiddleware",
]

ROOT_URLCONF = "linkedeye.urls"
WSGI_APPLICATION = "linkedeye.wsgi.application"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "core.context_processors.shell",
            ],
        },
    },
]

DATABASES = {
    "default": {
        "ENGINE": env("DB_ENGINE", "django.db.backends.sqlite3"),
        "NAME": env("DB_NAME", str(BASE_DIR / "db.sqlite3")),
        "USER": env("DB_USER", ""),
        "PASSWORD": env("DB_PASSWORD", ""),
        "HOST": env("DB_HOST", ""),
        "PORT": env("DB_PORT", ""),
        "CONN_MAX_AGE": int(env("DB_CONN_MAX_AGE", "60")),
        "CONN_HEALTH_CHECKS": True,
    }
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 12}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = env("TIME_ZONE", "Asia/Kolkata")
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
STORAGES = {
    "default": {"BACKEND": "django.core.files.storage.FileSystemStorage"},
    # DEBUG (tests, runserver) needs no collectstatic; production serves hashed, compressed files
    "staticfiles": {"BACKEND": "django.contrib.staticfiles.storage.StaticFilesStorage" if DEBUG
                    else "whitenoise.storage.CompressedManifestStaticFilesStorage"},
}
WHITENOISE_MANIFEST_STRICT = False  # a template naming a missing static file gets a 404 asset, not a 500 page
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

# Django 6 makes https the default scheme for URL form fields; opt in now (no effect on 4.2)
FORMS_URLFIELD_ASSUME_HTTPS = True

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "dashboard"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_HTTPONLY = True
# Sessions end after this many seconds without a request (default 8 hours, as the legacy console); Django's own default
# would keep a stolen or forgotten session alive for 14 days. LINKEDEYE_SESSION_SAVE_EVERY_REQUEST=false makes it absolute.
SESSION_COOKIE_AGE = int(env("SESSION_COOKIE_AGE", "28800"))
SESSION_SAVE_EVERY_REQUEST = env("SESSION_SAVE_EVERY_REQUEST", "true").lower() in ("1", "true", "yes")
SESSION_COOKIE_SECURE = not DEBUG
CSRF_COOKIE_SECURE = not DEBUG
SECURE_CONTENT_TYPE_NOSNIFF = True
X_FRAME_OPTIONS = "DENY"

# ---- LinkedEye ----
LINKEDEYE_TENANT = env("TENANT", "default")
# Optional authenticated bridge to the legacy Neo4j dashboard backend. Both the URL and token are required.
LINKEDEYE_BACKEND_SUMMARY_URL = env("BACKEND_SUMMARY_URL", "").strip()
LINKEDEYE_BACKEND_SUMMARY_TOKEN = env("BACKEND_SUMMARY_TOKEN", "").strip()
LINKEDEYE_VERSION = "9.0"
# Credential vault (onboarding): Fernet key(s) for secrets at rest. Comma-separate to rotate (first
# key encrypts, every listed key may decrypt). Unset: storing a secret fails; there is no plaintext fallback.
LINKEDEYE_CREDENTIAL_KEY = env("CREDENTIAL_KEY", "")
# Onboarding sends an exporter's HTTP basic / bearer login only over https. true: also over plain http (a network you trust).
LINKEDEYE_ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS = env("ONBOARDING_ALLOW_CLEARTEXT_CREDENTIALS", "").strip().lower() in (
    "1", "true", "yes", "on")
# honour sensitive_post_parameters / sensitive_variables even with DEBUG=true (Django's own filter switches off)
DEFAULT_EXCEPTION_REPORTER_FILTER = "core.debug.AlwaysCleansingFilter"
PROBE_ALLOWED_PRIVATE_CIDRS = env_list("PROBE_ALLOWED_PRIVATE_CIDRS")
PROBE_TIMEOUT = float(env("PROBE_TIMEOUT", "10"))
PROBE_WORKERS = int(env("PROBE_WORKERS", "8"))
PROBE_RETENTION_DAYS = int(env("PROBE_RETENTION_DAYS", "30"))

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "handlers": {"console": {"class": "logging.StreamHandler"}},
    "loggers": {
        "linkedeye": {"handlers": ["console"], "level": env("LOG_LEVEL", "INFO")},
        # with DEBUG off, 500 tracebacks and DisallowedHost would otherwise only be mailed to (empty) ADMINS
        "django.request": {"handlers": ["console"], "level": "ERROR", "propagate": False},
        "django.security": {"handlers": ["console"], "level": "WARNING", "propagate": False},
    },
}

# ---- Notifications (critical/warning events) ----
NOTIFY_WEBHOOK_URLS = env_list("NOTIFY_WEBHOOK_URLS")        # JSON POST per event (Slack/Teams/Apprise/StackStorm…)
NOTIFY_EMAILS = env_list("NOTIFY_EMAILS")
NOTIFY_SEVERITIES = env_list("NOTIFY_SEVERITIES", "critical,warning")
EMAIL_BACKEND = env("EMAIL_BACKEND", "django.core.mail.backends.smtp.EmailBackend" if not DEBUG else "django.core.mail.backends.console.EmailBackend")
EMAIL_HOST = env("EMAIL_HOST", "localhost")
EMAIL_PORT = int(env("EMAIL_PORT", "25"))
EMAIL_HOST_USER = env("EMAIL_HOST_USER", "")
EMAIL_HOST_PASSWORD = env("EMAIL_HOST_PASSWORD", "")
EMAIL_USE_TLS = env("EMAIL_USE_TLS", "false").lower() in ("1", "true", "yes")
DEFAULT_FROM_EMAIL = env("DEFAULT_FROM_EMAIL", "linkedeye@localhost")
# seconds; an unreachable SMTP server must not hang the notifier (Django's default is "no timeout")
EMAIL_TIMEOUT = float(env("EMAIL_TIMEOUT", "10"))
LINKEDEYE_WELCOME_EMAIL = env("WELCOME_EMAIL", "true").lower() in ("1", "true", "yes")
LINKEDEYE_PORTAL_URL = env("PORTAL_URL", "").strip()
# E-mail OTP after password on console sign-in (off in DEBUG unless LOGIN_OTP=true).
LINKEDEYE_LOGIN_OTP = env("LOGIN_OTP", "false" if DEBUG else "true").lower() in ("1", "true", "yes")

# ---- Sign-in throttling (administration.LoginThrottle; shared by every web worker) ----
LOGIN_MAX_FAILURES = int(env("LOGIN_MAX_FAILURES", "5"))                 # per username, within the window
LOGIN_MAX_FAILURES_PER_IP = int(env("LOGIN_MAX_FAILURES_PER_IP", "20"))  # per client address (TRUSTED_PROXIES honoured)
LOGIN_WINDOW_MINUTES = int(env("LOGIN_WINDOW_MINUTES", "15"))            # counting window and lock-out time
# Deliver notifications on a small background pool (after the event's transaction commits) instead of on the
# emitting thread, so a slow webhook/SMTP server never stalls the scheduler or a request. false = synchronous.
NOTIFY_ASYNC = env("NOTIFY_ASYNC", "true").lower() in ("1", "true", "yes")
NOTIFY_WORKERS = int(env("NOTIFY_WORKERS", "2"))
NOTIFY_QUEUE_SIZE = int(env("NOTIFY_QUEUE_SIZE", "500"))      # pending deliveries; beyond this they are dropped (logged)
# more than this many notifications of one severity within NOTIFY_DIGEST_WINDOW seconds: the rest are collapsed
# into one summary notification at the end of the window (0 = never collapse)
NOTIFY_DIGEST_THRESHOLD = int(env("NOTIFY_DIGEST_THRESHOLD", "10"))
NOTIFY_DIGEST_WINDOW = int(env("NOTIFY_DIGEST_WINDOW", "60"))

# ---- Scheduler (manage.py run_probes) ----
# Only one run_probes per database runs passes: PostgreSQL uses a session advisory lock, other databases a lease
# row (core.SchedulerLease) renewed every SCHEDULER_HEARTBEAT_SECONDS. A second instance idles as a standby.
SCHEDULER_LEASE = env("SCHEDULER_LEASE", "true").lower() in ("1", "true", "yes")
SCHEDULER_LEASE_TTL = int(env("SCHEDULER_LEASE_TTL", "90"))
SCHEDULER_HEARTBEAT_SECONDS = int(env("SCHEDULER_HEARTBEAT_SECONDS", "30"))
# a pass that has run this long skips the remaining non-critical jobs to the next pass (most overdue run first)
SCHEDULER_PASS_BUDGET = float(env("SCHEDULER_PASS_BUDGET", "60"))
SCHEDULER_CRITICAL_JOBS = env_list("SCHEDULER_CRITICAL_JOBS", "tcpcheck.run_due,servicedesk.sla")

# ---- Integrations (all optional; pages show "not connected" when unset) ----
PROMETHEUS_URL = env("PROMETHEUS_URL", "")                    # e.g. http://prometheus.fs-linkedeye:9090
PROMETHEUS_TIMEOUT = float(env("PROMETHEUS_TIMEOUT", "8"))
ALERTMANAGER_WEBHOOK_TOKEN = env("ALERTMANAGER_WEBHOOK_TOKEN", "")   # required bearer token for /api/v1/alertmanager/
LOG_INGEST_TOKEN = env("LOG_INGEST_TOKEN", "")               # required bearer token for /api/v1/logs/
STACKSTORM_URL = env("STACKSTORM_URL", "")                    # e.g. https://st2.fs-linkedeye/api/v1/webhooks/
STACKSTORM_API_KEY = env("STACKSTORM_API_KEY", "")
INCIDENT_AUTO_CREATE = env("INCIDENT_AUTO_CREATE", "true").lower() in ("1", "true", "yes")
NAGIOS_URL = env("NAGIOS_URL", "")                            # Nagios Core base URL (statusjson.cgi is appended)
NAGIOS_USER = env("NAGIOS_USER", "")
NAGIOS_PASSWORD = env("NAGIOS_PASSWORD", "")
NAGIOS_TIMEOUT = float(env("NAGIOS_TIMEOUT", "10"))
# Retention (days) for high-volume module data
LOG_RETENTION_DAYS = int(env("LOG_RETENTION_DAYS", "14"))
TELEMETRY_ALERT_RETENTION_DAYS = int(env("TELEMETRY_ALERT_RETENTION_DAYS", "90"))
# Hours without a repeat notification after which a still "firing" alert is resolved as stale (0 = never). Keep it
# well above Alertmanager's repeat_interval.
TELEMETRY_ALERT_STALE_HOURS = int(env("TELEMETRY_ALERT_STALE_HOURS", "48"))
AUTOMATION_RETENTION_DAYS = int(env("AUTOMATION_RETENTION_DAYS", "90"))
# Ops Agent: OpenAI-compatible chat. Empty key = retrieval-only. The key never leaves the server.
AGENT_API_KEY = env("AGENT_API_KEY", "")
AGENT_BASE_URL = env("AGENT_BASE_URL", "https://api.openai.com/v1")
AGENT_MODEL = env("AGENT_MODEL", "gpt-4o-mini")
AGENT_EMBEDDING_MODEL = env("AGENT_EMBEDDING_MODEL", "text-embedding-3-small")
AGENT_TIMEOUT = float(env("AGENT_TIMEOUT", "30"))
AGENT_RETENTION_DAYS = int(env("AGENT_RETENTION_DAYS", "90"))
AGENT_TURNS_PER_MINUTE = int(env("AGENT_TURNS_PER_MINUTE", "20"))   # questions per user per minute (0 = no limit)
# APM (apm app): days of adapter runs and status-change history kept, and days of execution logs kept (the retention
# job also caps the rows kept per source).
APM_RETENTION_DAYS = int(env("APM_RETENTION_DAYS", "30"))
APM_LOG_RETENTION_DAYS = int(env("APM_LOG_RETENTION_DAYS", "7"))
