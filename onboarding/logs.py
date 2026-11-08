"""The Logs step: a ready-to-paste log shipper configuration for one target, and the check that its
lines are arriving in the observe log store.

Shipper: Vector. LinkedEye's ingest endpoint (``POST /api/v1/logs/``, ``observe.views.log_ingest``) takes
JSON ``{ts, source, level, message, host, site, attributes}`` entries behind a bearer token, which is
exactly what Vector's ``http`` sink with ``encoding.codec = "json"`` produces. Promtail speaks only the
Loki push protocol (snappy-compressed protobuf), which that endpoint does not accept, so promtail cannot
be offered honestly until a Loki-compatible endpoint exists; ``SHIPPERS`` is the one place to extend.

The token is the product's ``LINKEDEYE_LOG_INGEST_TOKEN`` (the very value the ingest view compares
against). It is put into the generated text and into nothing else: not into an error, not into a log
line, not into the facts. The wizard shows the text once and stores it with the token replaced.

Verification reads ``observe.LogEntry`` for the tenant, in the run's site, received in the last
``WINDOW`` minutes, whose ``host`` is this target (hostname, address or short name).
"""
import ipaddress
import logging
import re
from datetime import timedelta

from django.conf import settings
from django.db.models import Count, Max, Q
from django.db.models.functions import Lower
from django.urls import NoReverseMatch, reverse
from django.utils import timezone

log = logging.getLogger("linkedeye.onboarding")

WINDOW = timedelta(minutes=5)
SAMPLE_LINES = 5
SAMPLE_TARGET_LIMIT = 12          # sample lines are read for at most this many targets per page
HOST_CHUNK = 200
SHIPPERS = ("vector",)
PLACEHOLDER_URL = "https://<linkedeye-host>/api/v1/logs/"

_RPM_HINTS = ("red hat", "rhel", "centos", "rocky", "alma", "fedora", "oracle linux", "amazon linux", "suse")
_CTRL = re.compile(r"[\x00-\x1f\x7f]")


# ----------------------------------------------------------------------------- quoting
def _q(value):
    """A double-quoted string that is valid in TOML and in VRL. Control characters are dropped, the
    quote and the backslash are escaped and a dollar sign is doubled, because Vector interpolates
    ``${VAR}`` inside the whole config file before it parses it."""
    text = _CTRL.sub("", str(value or ""))
    text = text.replace("\\", "\\\\").replace('"', '\\"').replace("$", "$$")
    return f'"{text}"'


def _short(host):
    host = (host or "").strip().lower()
    if not host:
        return ""
    try:
        ipaddress.ip_address(host)
        return host
    except ValueError:
        return host.split(".")[0]


def host_names(target):
    """The host labels a shipper on this target may report, lower-cased."""
    names = {n for n in (target.hostname, target.address, _short(target.hostname)) if n}
    return {n.strip().lower() for n in names if n and n.strip()}


# ----------------------------------------------------------------------------- the product side
def ingest_token():
    """The token the ingest endpoint enforces. Never log or return it anywhere but the generated text."""
    from administration import integration_config as ic

    return ic.log_ingest_token()


def ingest_enabled():
    return bool(ingest_token())


def ingest_url(request=None):
    try:
        path = reverse("observe:log_ingest")
    except NoReverseMatch:
        path = "/api/v1/logs/"
    if request is not None:
        return request.build_absolute_uri(path)
    base = (getattr(settings, "LINKEDEYE_PORTAL_URL", "") or "").rstrip("/")
    parts = base.split("/")
    if base and len(parts) >= 3:          # LINKEDEYE_PORTAL_URL may point at the login page: keep scheme://host
        return "/".join(parts[:3]) + path
    return PLACEHOLDER_URL


def rotate_url():
    try:
        return reverse("administration:integrations") + "#logs"
    except NoReverseMatch:
        return ""


# ----------------------------------------------------------------------------- per-OS sources
def _linux_files(os_name):
    low = (os_name or "").lower()
    if any(h in low for h in _RPM_HINTS):
        return ["/var/log/messages", "/var/log/secure", "/var/log/cron"]
    return ["/var/log/syslog", "/var/log/auth.log", "/var/log/kern.log"]


_LEVEL_FROM_TEXT = r"""  if match(msg, r'(?i)\b(crit|critical|fatal|panic|emerg|emergency)\b') {
    level = "CRITICAL"
  } else if match(msg, r'(?i)\b(err|error|fail|failed|failure)\b') {
    level = "ERROR"
  } else if match(msg, r'(?i)\b(warn|warning)\b') {
    level = "WARN"
  }"""


def _vrl_linux(host, site, target_type):
    return f"""msg = string(.message) ?? "log line"
level = "INFO"
prio = -1
if exists(.PRIORITY) {{
  prio = to_int(.PRIORITY) ?? -1
}}
if prio >= 0 && prio <= 2 {{
  level = "CRITICAL"
}} else if prio == 3 {{
  level = "ERROR"
}} else if prio == 4 {{
  level = "WARN"
}} else if prio == 7 {{
  level = "DEBUG"
}} else if prio < 0 {{
{_LEVEL_FROM_TEXT}
}}
attrs = {{"shipper": "vector", "target_type": {_q(target_type)}}}
if exists(.file) {{
  attrs.file = .file
}}
if exists(._SYSTEMD_UNIT) {{
  attrs.unit = ._SYSTEMD_UNIT
}}
src = string(.SYSLOG_IDENTIFIER) ?? string(._SYSTEMD_UNIT) ?? string(.file) ?? "system"
. = {{"ts": .timestamp, "host": {_q(host)}, "site": {_q(site)}, "source": src, "level": level, "message": msg, "attributes": attrs}}"""


def _vrl_windows(host, site):
    return f"""msg = string(.message) ?? "log line"
level = "INFO"
{_LEVEL_FROM_TEXT}
attrs = {{"shipper": "vector", "target_type": "windows"}}
if exists(.file) {{
  attrs.file = .file
}}
src = string(.file) ?? "windows"
. = {{"ts": .timestamp, "host": {_q(host)}, "site": {_q(site)}, "source": src, "level": level, "message": msg, "attributes": attrs}}"""


def _vrl_syslog(host, site):
    return f"""msg = string(.message) ?? "log line"
sev = downcase(string(.severity) ?? "info")
level = "INFO"
if sev == "emerg" || sev == "alert" || sev == "crit" {{
  level = "CRITICAL"
}} else if sev == "err" {{
  level = "ERROR"
}} else if sev == "warning" {{
  level = "WARN"
}} else if sev == "debug" {{
  level = "DEBUG"
}}
attrs = {{"shipper": "vector", "target_type": "device"}}
if exists(.source_ip) {{
  attrs.source_ip = .source_ip
}}
if exists(.facility) {{
  attrs.facility = .facility
}}
src = string(.appname) ?? "syslog"
. = {{"ts": .timestamp, "host": string(.hostname) ?? {_q(host)}, "site": {_q(site)}, "source": src, "level": level, "message": msg, "attributes": attrs}}"""


def _vrl_kubernetes(host, site):
    return f"""msg = string(.message) ?? "log line"
level = "INFO"
{_LEVEL_FROM_TEXT}
ns = string(.kubernetes.pod_namespace) ?? "default"
pod = string(.kubernetes.pod_name) ?? "pod"
attrs = {{"shipper": "vector", "target_type": "k8s_node", "namespace": ns, "pod": pod}}
. = {{"ts": .timestamp, "host": string(.kubernetes.pod_node_name) ?? {_q(host)}, "site": {_q(site)}, "source": ns + "/" + pod, "level": level, "message": msg, "attributes": attrs}}"""


def _toml_list(items):
    return "[" + ", ".join(_q(i) for i in items) + "]"


def _sink(inputs, url, token):
    return f"""
# Where the lines go: LinkedEye's log-ingest endpoint (JSON array, bearer token).
[sinks.linkedeye]
type = "http"
inputs = {_toml_list(inputs)}
uri = {_q(url)}
method = "post"
encoding.codec = "json"
auth.strategy = "bearer"
auth.token = {_q(token)}
batch.max_events = 200
batch.timeout_secs = 5
healthcheck.enabled = false      # the endpoint answers POST only"""


def vector_config(target, token, url, os_name=""):
    """The Vector configuration (TOML) for `target`: its sources for its kind of host, a remap that
    shapes every event into LinkedEye's log entry, and the http sink with the bearer token."""
    host = target.hostname
    site = target.run.site or ""
    kind = target.target_type
    head = [f"# LinkedEye log shipper for {_q(host)[1:-1]} (site {_q(site)[1:-1] or 'not set'}).",
            "# Save as /etc/vector/vector.toml (Windows: C:/Program Files/Vector/config/vector.toml),",
            "# run `vector validate` and then start or restart the service. Keep this file readable by root only.",
            ""]
    if kind == "windows":
        sources = ['[sources.le_files]', 'type = "file"',
                   'include = ["C:/inetpub/logs/LogFiles/**/*.log", "C:/ProgramData/**/*.log"]',
                   'read_from = "end"', 'ignore_older_secs = 86400', "",
                   "# Windows Event Log: use a Vector build that lists the windows_event_log source",
                   "# (`vector list`), add it here and add its name to the inputs below.", ""]
        inputs, vrl = ["le_files"], _vrl_windows(host, site)
    elif kind == "device":
        sources = ["# Network devices push syslog to this collector (point the device's syslog target at",
                   "# this machine). Port 514 needs root or CAP_NET_BIND_SERVICE; use 1514 if you prefer.",
                   "[sources.le_syslog]", 'type = "syslog"', 'address = "0.0.0.0:514"', 'mode = "udp"', ""]
        inputs, vrl = ["le_syslog"], _vrl_syslog(host, site)
    elif kind == "k8s_node":
        sources = ["# Run Vector as a DaemonSet with /var/log and /var/lib/docker/containers mounted read-only.",
                   "[sources.le_k8s]", 'type = "kubernetes_logs"', ""]
        inputs, vrl = ["le_k8s"], _vrl_kubernetes(host, site)
    else:
        files = _linux_files(os_name)
        sources = ["[sources.le_journald]", 'type = "journald"', "",
                   "[sources.le_files]", 'type = "file"', f"include = {_toml_list(files)}",
                   'read_from = "end"', 'ignore_older_secs = 86400', ""]
        inputs, vrl = ["le_journald", "le_files"], _vrl_linux(host, site, kind)
    shape = ["# Shape every event into LinkedEye's entry: ts, host, site, source, level, message, attributes.",
             "[transforms.le_shape]", 'type = "remap"', f"inputs = {_toml_list(inputs)}",
             "source = '''", vrl, "'''", ""]
    return "\n".join(head + sources + shape) + _sink(["le_shape"], url, token) + "\n"


def _not_applicable(target):
    if target.target_type == "url":
        return "A URL / service check has no host to ship logs from. Onboard the server that runs it to collect its logs."
    if target.target_type == "prometheus":
        return "A Prometheus connection has no host to ship logs from. Onboard the server it runs on to collect its logs."
    return ""


# ----------------------------------------------------------------------------- verification
def _redact(text, token):
    text = str(text or "")
    if token:
        text = text.replace(token, "[redacted]")
    return text[:300]


def _candidates(run, window_start):
    from observe.models import LogEntry
    qs = LogEntry.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, received_at__gte=window_start,
                                 ts__gte=window_start - timedelta(days=1))
    site = (run.site or "").strip()
    if site and site != "All Sites":
        qs = qs.filter(Q(site=site) | Q(site=""))
    return qs


def target_statuses(run, targets, now=None):
    """{target.pk: status} for the targets of one run, from the observe log store.

    status = {"applicable", "arriving", "lines", "last_seen", "samples": [...], "nearby": [hosts]}.
    A fixed number of queries whatever the number of targets (plus one per target for sample lines, for the
    first SAMPLE_TARGET_LIMIT targets that have lines). Read-only."""
    now = now or timezone.now()
    start = now - WINDOW
    token = ingest_token()
    base = _candidates(run, start)
    out, wanted = {}, {}
    for t in targets:
        na = _not_applicable(t)
        out[t.pk] = {"applicable": not na, "note": na, "arriving": False, "lines": 0, "last_seen": None,
                     "samples": [], "nearby": []}
        if not na:
            wanted[t.pk] = host_names(t)
    counts = {}
    names = sorted({n for s in wanted.values() for n in s})
    for i in range(0, len(names), HOST_CHUNK * 3):
        chunk = names[i:i + HOST_CHUNK * 3]
        rows = (base.annotate(h=Lower("host")).filter(h__in=chunk).values("h").order_by()
                .annotate(n=Count("id"), last=Max("received_at")))
        for r in rows:
            counts[r["h"]] = (r["n"], r["last"])
    sampled = 0
    for pk, hosts in wanted.items():
        n = sum(counts[h][0] for h in hosts if h in counts)
        if not n:
            continue
        st = out[pk]
        st["arriving"], st["lines"] = True, n
        st["last_seen"] = max(counts[h][1] for h in hosts if h in counts)
        if sampled < SAMPLE_TARGET_LIMIT:
            sampled += 1
            rows = (base.annotate(h=Lower("host")).filter(h__in=list(hosts))
                    .order_by("-ts", "-id")[:SAMPLE_LINES])
            st["samples"] = [{"ts": e.ts, "level": e.level, "tone": e.tone, "source": e.source,
                              "message": _redact(e.message, token)} for e in rows]
    if any(w and not out[pk]["arriving"] for pk, w in wanted.items()):
        known = set(names)
        seen = (base.annotate(h=Lower("host")).exclude(host="").values("h")
                .annotate(n=Count("id")).order_by("-n")[:12])
        nearby = [r["h"] for r in seen if r["h"] not in known][:6]
        for pk in wanted:
            if not out[pk]["arriving"]:
                out[pk]["nearby"] = nearby
    return out


def status_for(target):
    return target_statuses(target.run, [target]).get(target.pk) or {}


# ----------------------------------------------------------------------------- the hook
def logs_config(target):
    """The hook behind ``steps.logs_config``. See that docstring for the contract.

    Returns shipper, config (contains the token: the wizard shows it once and stores it redacted),
    token (so the wizard can redact it), verified, lines, last_seen, applicable and, when something
    stops the operator from shipping, ``error`` (never the token)."""
    st = status_for(target)
    base = {"shipper": SHIPPERS[0], "config": "", "applicable": st.get("applicable", True),
            "verified": bool(st.get("arriving")), "lines": st.get("lines", 0),
            "last_seen": st["last_seen"].isoformat() if st.get("last_seen") else "",
            "window_minutes": int(WINDOW.total_seconds() // 60)}
    if not st.get("applicable", True):
        base["note"] = st.get("note", "")
        return base
    token = ingest_token()
    if not token:
        base["error"] = ("Log ingest is switched off: set LINKEDEYE_LOG_INGEST_TOKEN on the server "
                         "(or save a token on the Integrations page), restart, then generate again.")
        return base
    url = ingest_url()
    os_name = ""
    facts = target.facts if isinstance(target.facts, dict) else {}
    if isinstance(facts.get("os"), str):
        os_name = facts["os"]
    base["config"] = vector_config(target, token, url, os_name)
    base["token"] = token
    base["ingest_url"] = url
    if url == PLACEHOLDER_URL:
        base["note"] = ("LINKEDEYE_PORTAL_URL is not set, so the endpoint address in the file is a placeholder: "
                        "replace <linkedeye-host> with the address of this console.")
    return base
