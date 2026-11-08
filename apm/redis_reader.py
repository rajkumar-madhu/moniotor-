"""Path B: LinkedEye reads the toolkit's two Redis keys itself and feeds the SAME ingest service as the push API.

* the ``redis`` package is an OPTIONAL dependency (``requirements-apm.txt``) imported lazily: without it nothing else breaks
  and the Source form says what to install;
* the host goes through the probe SSRF policy (``probes.checks.resolve_allowed``): loopback / private / link-local /
  metadata addresses are refused unless the operator listed the range in ``LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS``. A plain
  connection is made to the address that passed the check (no second DNS lookup); with TLS the host name is used so the
  certificate is verified;
* the password lives in the credential vault; it is decrypted only inside ``_password`` for the duration of one poll,
  never logged, never put in an error message (``_safe`` scrubs it) and never written to the Source;
* short socket timeouts, a size cap on the value, and every failure is recorded on the Source (``redis_last_error``).
"""
import logging
import re
import socket
import threading

from django.conf import settings
from django.utils import timezone
from django.views.decorators.debug import sensitive_variables

from core import secrets as vault

from . import payloads, services
from .models import Source

log = logging.getLogger("linkedeye.apm.redis")

CONNECT_TIMEOUT = 3
SOCKET_TIMEOUT = 3
DEADLINE = 25                       # seconds for a whole poll: socket timeouts apply to each read, so a hostile server could drip-feed
KEY_RE = re.compile(r"^[A-Za-z0-9_.:\-/@+=]{1,200}$")
INSTALL_HINT = "The 'redis' Python package is not installed. Install requirements-apm.txt and restart LinkedEye."


class ReaderError(Exception):
    """A poll problem the operator can read; the message never contains the password."""


def redis_module():
    """The ``redis`` package, imported lazily. Raises ReaderError with the install hint when it is missing."""
    try:
        import redis
    except ImportError:
        raise ReaderError(INSTALL_HINT) from None
    return redis


def available():
    try:
        redis_module()
    except ReaderError:
        return False
    return True


def key_names(source):
    """[(kind, key)] the source is configured to read."""
    out = []
    for kind, suffix in (("matrix", source.redis_adapter_suffix), ("table", source.redis_process_suffix)):
        if suffix:
            out.append((kind, f"{source.redis_prefix}:{suffix}" if source.redis_prefix else suffix))
    return out


def configured(source):
    return bool(source.redis_host and key_names(source))


def _safe(exc, secret=""):
    text = f"{type(exc).__name__}: {exc}"
    if secret:
        text = text.replace(secret, "***")
    return payloads.clean_text(text, 200)


@sensitive_variables()
def _password(source):
    """The Redis password from the credential vault ("" when none is configured)."""
    if not source.redis_credential_id:
        return ""
    from django.apps import apps
    Credential = apps.get_model("onboarding", "Credential")
    row = Credential.objects.filter(pk=source.redis_credential_id, tenant_key=source.tenant_key).first()
    if row is None:
        raise ReaderError("The stored Redis password is missing; enter it again on the source.")
    try:
        return vault.decrypt(row.secret)
    except (vault.VaultUnavailable, vault.VaultError):
        raise ReaderError("The credential vault cannot decrypt the Redis password (check LINKEDEYE_CREDENTIAL_KEY).") from None


def resolve(source):
    """The address the connection is made to, after the SSRF policy. Raises ReaderError."""
    from probes import checks
    try:
        return checks.resolve_allowed(source.redis_host, int(source.redis_port))
    except checks.ProbeBlocked as exc:
        raise ReaderError(f"Host refused by the network policy: {payloads.clean_text(exc, 160)}. A private range must be "
                          "listed in LINKEDEYE_PROBE_ALLOWED_PRIVATE_CIDRS.") from None
    except (socket.gaierror, OSError):
        raise ReaderError(f"The host name {payloads.clean_text(source.redis_host, 80)!r} does not resolve.") from None


@sensitive_variables()
def connect(source, ip, password):
    """A connected redis client (monkeypatched in tests). Plain connections use the checked address, TLS the host name."""
    redis = redis_module()
    kwargs = dict(port=int(source.redis_port), db=int(source.redis_db), username=source.redis_username or None,
                  password=password or None, socket_connect_timeout=CONNECT_TIMEOUT, socket_timeout=SOCKET_TIMEOUT,
                  decode_responses=False)
    if source.redis_tls:
        return redis.Redis(host=source.redis_host, ssl=True, ssl_cert_reqs="required", **kwargs)
    return redis.Redis(host=ip, **kwargs)


def _watchdog(client):
    """Drop the connection if one poll takes longer than DEADLINE (returns the timer to cancel, or None for a client without
    a connection pool)."""
    pool = getattr(client, "connection_pool", None)
    if pool is None:
        return None
    timer = threading.Timer(DEADLINE, pool.disconnect)
    timer.daemon = True
    timer.start()
    return timer


def _read(client, key):
    """The value of ``key`` as text, None when absent. Refuses values above the size cap before downloading them."""
    if not KEY_RE.match(key):
        raise ReaderError(f"The key name {payloads.clean_text(key, 60)!r} is not valid.")
    strlen = getattr(client, "strlen", None)
    if strlen is not None:
        size = strlen(key)
        if not size:
            return None
        if size > payloads.MAX_LITERAL:
            raise ReaderError(f"The value of {key} is larger than {payloads.MAX_LITERAL // 1024} KB; not read.")
    value = client.get(key)
    if value is None:
        return None
    return value.decode("utf-8", "replace") if isinstance(value, (bytes, bytearray)) else str(value)


@sensitive_variables()
def poll_source(source, now=None, emit_events=True):
    """Read the source's keys once and ingest them. Returns {'ok', 'read', 'errors', 'results'}; records the outcome on the
    Source. Never raises."""
    now = now or timezone.now()
    errors, results = [], {}
    password = ""
    try:
        if not configured(source):
            raise ReaderError("Redis host and at least one key suffix are required.")
        password = _password(source)
        ip = resolve(source)
        client = connect(source, ip, password)
        timer = _watchdog(client)
        try:
            for kind, key in key_names(source):
                try:
                    text = _read(client, key)
                    if text is None:
                        errors.append(f"key {key} not found")
                        continue
                    payload = payloads.sanitize_tree(payloads.parse_text(text))
                    fn = services.ingest_matrix if kind == "matrix" else services.ingest_table
                    results[kind] = fn(source, payload, host=f"redis:{source.redis_host}", script="redis", now=now,
                                       emit_events=emit_events)
                except payloads.PayloadError as exc:
                    errors.append(f"{key}: {payloads.clean_text(exc, 160)}")
                except ReaderError as exc:
                    errors.append(str(exc))
        finally:
            if timer is not None:
                timer.cancel()
            close = getattr(client, "close", None)
            if close:
                try:
                    close()
                except Exception:                   # noqa: BLE001
                    pass
    except ReaderError as exc:
        errors.append(str(exc))
    except Exception as exc:                        # noqa: BLE001 - redis.exceptions.*, timeouts, resets
        errors.append(_safe(exc, password))
    error_text = "; ".join(errors)[:300]
    Source.objects.filter(pk=source.pk).update(redis_last_polled_at=now, redis_last_error=error_text)
    source.redis_last_polled_at, source.redis_last_error = now, error_text
    return {"ok": not errors, "errors": errors, "results": results}


@sensitive_variables()
def test_source(source):
    """The 'Test connection' button: PING and check that the keys exist. Returns (ok, message); reads nothing else."""
    password = ""
    try:
        if not configured(source):
            raise ReaderError("Redis host and at least one key suffix are required.")
        password = _password(source)
        ip = resolve(source)
        client = connect(source, ip, password)
        timer = _watchdog(client)
        try:
            client.ping()
            found, missing = [], []
            for _kind, key in key_names(source):
                if not KEY_RE.match(key):
                    raise ReaderError(f"The key name {payloads.clean_text(key, 60)!r} is not valid.")
                (found if client.exists(key) else missing).append(key)
        finally:
            if timer is not None:
                timer.cancel()
            close = getattr(client, "close", None)
            if close:
                try:
                    close()
                except Exception:                   # noqa: BLE001
                    pass
        if missing:
            return False, f"Connected, but the key(s) {', '.join(missing)} do not exist yet."
        return True, f"Connected. Found {', '.join(found)}."
    except ReaderError as exc:
        return False, str(exc)
    except Exception as exc:                        # noqa: BLE001
        return False, _safe(exc, password)


def poll_all(now=None):
    """The ``apm.redis_poll`` job body."""
    sources = list(Source.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, enabled=True, redis_poll=True))
    ok = bad = 0
    for src in sources:
        try:
            res = poll_source(src, now=now)
        except Exception:                           # noqa: BLE001
            log.exception("apm redis poll crashed")
            bad += 1
            continue
        ok += 1 if res["ok"] else 0
        bad += 0 if res["ok"] else 1
    return f"polled {len(sources)} source(s): {ok} ok, {bad} with errors" if sources else "no sources to poll"
