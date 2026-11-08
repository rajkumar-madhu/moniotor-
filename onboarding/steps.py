"""The four things the wizard asks the rest of the product to do for one target.

Each hook is a seam: the wizard shell (views + templates + state machine) is complete and walks
end to end without them, and each hook is filled in by the module that owns that work:

    verify_target     probes.checks (DNS, TCP, HTTP /metrics) + optional paramiko facts
    enable_collection the install command / SSH install, SNMP module, kube-state-metrics
    logs_config       promtail / vector configuration and the log-ingest verification
    activate          cmdb.ConfigurationItem, network.Device, observe.Exporter wiring

Contract for every hook:

* it takes the target (and, where noted, the options the operator ticked on that step) and returns
  a plain dict. The wizard persists it: ``state`` (a ``TargetState`` value) and ``error`` are the
  verdict, every other key is a fact merged into ``target.facts``. A hook does not save the target's
  state/error/facts itself (it may save the ``ci`` / ``device`` / ``exporter`` links and the records
  it creates). Without a ``state`` the wizard keeps the current state, or uses the default named in
  each docstring below. Keys whose names look like a secret (password, token, secret, community,
  private_key, ...) are dropped before anything is stored, whatever the hook returns;
* it is idempotent: calling it twice for the same target must not create a second record, a second
  install or a second exporter row;
* it never raises for an expected failure. An unreachable host, a refused credential or a missing
  exporter is a result: return ``{"state": "unreachable", "error": "..."}``. Only a bug raises, and
  the wizard then records the exception type only (never its message);
* it must not put a secret into its return value, into ``error``, into a log line or into an
  exception message. Secrets are read with ``core.secrets.use()`` inside a ``with`` block;
* all outbound probing goes through ``probes.checks``, so the SSRF policy applies.

Until a hook is implemented it raises StepPending (a NotImplementedError); the views catch that,
record a "pending" step-log line and carry on, so the operator can still walk the whole wizard.
"""


class StepPending(NotImplementedError):
    """Raised by a hook that is not wired up yet. The wizard treats it as 'not done, carry on'."""


def verify_target(target):
    """Check `target` now and record what was found. Returns a dict of facts.

    Expected to do, through `probes.checks` only:
      * DNS resolution of `target.hostname` (or use `target.address` when given);
      * a TCP connect to `target.port`;
      * an HTTP GET of `target.metrics_url` and a parse of the first lines to identify the exporter
        and its version;
      * optionally, with `target.credential` and paramiko present, SSH facts: hostname, OS, kernel,
        CPU count, memory, whether an exporter is already installed and which version. The host key
        is pinned on first connect; a change afterwards is a hard failure.

    Returns the facts dict (keys such as hostname, os, kernel, cpus, memory_mb, exporter,
    exporter_version, dns, tcp, http_status, duration_ms) plus, optionally, ``state`` (reachable /
    unreachable / exporter_found) and ``error``. Without a ``state``: ``error`` or ``tcp``/``dns``
    False means unreachable, an ``exporter`` fact means exporter_found, else reachable. Every probe
    attempt can be written to StepLog with ``onboarding.services.log_step`` (outcome and duration).
    """
    if target.target_type in ("linux", "windows"):
        from . import servers                 # Linux and Windows servers: onboarding/servers.py
        return servers.verify(target)
    if target.target_type == "device":
        from . import devices                 # SNMP devices, through the snmp_exporter: onboarding/devices.py
        return devices.verify_device(target)
    if target.target_type == "prometheus":
        from . import prometheus_step         # a Prometheus server's own API: onboarding/prometheus_step.py
        return prometheus_step.verify_prometheus(target)
    from . import others                      # Kubernetes nodes and URL services: onboarding/others.py
    return others.verify(target)


def enable_collection(target, options=None):
    """Make `target` expose metrics. Returns a dict of facts (and is idempotent).

    `options` carries the operator's explicit choices from the Enable step, e.g.
    {"install_over_ssh": True, "snmp_module": "if_mib", "kube_state_url": "..."}.
    Nothing is installed unless the operator ticked it on that step.

    Expected behaviour: with no exporter present, either return the exact install command for the
    operator to run (key "install_command") or, only with options["install_over_ssh"], install it
    over SSH using the stored credential with a fixed command allow-list - never free text from the
    form. A failed install leaves the host untouched and reports stderr in ``error``. For an
    SNMP device, enable the snmp_exporter module; for a Kubernetes node, point at kube-state-metrics.
    Return ``state`` installed (or exporter_found when one already answers) / failed; a result that
    only carries an ``install_command`` leaves the state as it is.
    """
    if target.target_type in ("linux", "windows"):
        from . import servers                 # Linux and Windows servers: onboarding/servers.py
        return servers.enable(target, options)
    from . import others                      # devices, Kubernetes nodes, URL services, Prometheus: onboarding/others.py
    return others.enable(target, options)


def logs_config(target):
    """Return the log shipper configuration for `target`.

    Expected keys: "shipper" (promtail | vector), "config" (the text to paste, pre-filled with the
    product's log-ingest URL and token), "token" (the token value, so the wizard can redact it),
    "verified" (bool: lines have arrived), "lines" (how many arrived so far). The wizard renders the
    full config in the one response that generated it, stores it with the token replaced by
    ``<LOG_INGEST_TOKEN>`` and never keeps the token in StepLog or ``target.facts``.
    """
    from . import logs                        # Vector configuration + arrival check: onboarding/logs.py
    return logs.logs_config(target)


def activate(target):
    """Wire `target` into the rest of the product, idempotently. Returns a dict of what it linked.

    Expected to create-or-link, without ever duplicating a record:
      * a `cmdb.ConfigurationItem` with `prometheus_instance` set to `target.instance`;
      * a `network.Device` for SNMP targets, or a compute server for hypervisors / Kubernetes nodes;
      * an `observe.Exporter` row for the metrics endpoint;
      * membership of the service-discovery endpoint (target.enabled = True);
      * a StepLog entry.

    Must save target.ci / target.device / target.exporter. The state defaults to scraping (the
    wizard then checks Prometheus for ``up == 1`` and promotes the target to live, recording
    ``first_sample_at``). Returns e.g.
    {"ci": <pk>, "device": <pk>, "exporter": <pk>, "created": [...], "linked": [...]}.
    """
    from . import activate as activation      # onboarding/activate.py
    return activation.activate(target)
