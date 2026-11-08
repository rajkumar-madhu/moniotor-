"""The four things the wizard asks the rest of the product to do for one target.

Each hook is a seam: the wizard shell (views + templates + state machine) is complete and walks
end to end without them, and each hook is filled in by the module that owns that work:

    verify_target     probes.checks (DNS, TCP, HTTP /metrics) + optional paramiko facts
    enable_collection the install command / SSH install, SNMP module, kube-state-metrics
    logs_config       promtail / vector configuration and the log-ingest verification
    activate          cmdb.ConfigurationItem, network.Device, observe.Exporter wiring

Contract for every hook:

* it takes the target (and, where noted, the options the operator ticked on that step) and returns
  a plain dict that the view writes into `target.facts`, plus whatever the docstring names;
* it is idempotent: calling it twice for the same target must not create a second record, a second
  install or a second exporter row;
* it never raises for an expected failure. An unreachable host, a refused credential or a missing
  exporter is a result: set `state` / `last_error` and return. Only a bug raises;
* it must not put a secret into its return value, into `last_error`, into a log line or into an
  exception message. Secrets are read with `core.secrets.use()` inside a `with` block;
* all outbound probing goes through `probes.checks`, so the SSRF policy applies.

Until a hook is implemented it raises NotImplementedError; the views catch that, record a
"pending" step log line and carry on, so the operator can still walk the whole wizard.
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

    Must set `target.state` to reachable / unreachable / exporter_found and `target.last_error`
    (never containing a secret), write every attempt with its outcome and duration to StepLog, and
    return the facts dict (keys such as hostname, os, kernel, cpus, memory_mb, exporter,
    exporter_version, dns, tcp, http_status).
    """
    raise StepPending("verify_target() is not implemented yet")


def enable_collection(target, options=None):
    """Make `target` expose metrics. Returns a dict of facts (and is idempotent).

    `options` carries the operator's explicit choices from the Enable step, e.g.
    {"install_over_ssh": True, "snmp_module": "if_mib", "kube_state_url": "..."}.
    Nothing is installed unless the operator ticked it on that step.

    Expected behaviour: with no exporter present, either return the exact install command for the
    operator to run (key "install_command") or, only with options["install_over_ssh"], install it
    over SSH using the stored credential with a fixed command allow-list - never free text from the
    form. A failed install leaves the host untouched and reports stderr in `last_error`. For an
    SNMP device, enable the snmp_exporter module; for a Kubernetes node, point at kube-state-metrics.
    Sets `target.state` to installing / installed / failed.
    """
    raise StepPending("enable_collection() is not implemented yet")


def logs_config(target):
    """Return the log shipper configuration for `target`.

    Expected keys: "shipper" (promtail | vector), "config" (the text to paste, pre-filled with the
    product's log-ingest URL and a token read from the vault), "verified" (bool: lines have
    arrived), "lines" (how many arrived so far). The token is rendered once for the operator to
    copy, exactly as the log-ingest integration already does; it is never stored in StepLog.
    """
    raise StepPending("logs_config() is not implemented yet")


def activate(target):
    """Wire `target` into the rest of the product, idempotently. Returns a dict of what it linked.

    Expected to create-or-link, without ever duplicating a record:
      * a `cmdb.ConfigurationItem` with `prometheus_instance` set to `target.instance`;
      * a `network.Device` for SNMP targets, or a compute server for hypervisors / Kubernetes nodes;
      * an `observe.Exporter` row for the metrics endpoint;
      * membership of the service-discovery endpoint (target.enabled = True);
      * a StepLog entry.

    Must set target.ci / target.device / target.exporter and move the state to scraping (or live
    once the first sample has landed, recording `first_sample_at`). Returns e.g.
    {"ci": <pk>, "device": <pk>, "exporter": <pk>, "created": [...], "linked": [...]}.
    """
    raise StepPending("activate() is not implemented yet")
