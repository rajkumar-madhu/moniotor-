"""The targets step: one host, a pasted list or a CSV, with a dry-run preview.

* ``add_single`` adds one host (name, optional address, type, port).
* ``preview`` is the dry run for the pasted list / CSV: it parses exactly what ``services.parse_target_list``
  would, says for every row whether it would be added, updated or left alone, lists the lines that could
  not be read and the rows over the per-run limit, and writes **nothing**. Its confirm button posts the
  normalised rows to the ordinary *Add targets* action, so there is one code path that writes.
* An upload is bounded (``services.MAX_PASTE_BYTES``) and must be text: an oversized or binary file is
  refused with a reason rather than silently truncated.
"""
import csv
import io

from django import forms
from django.contrib import messages
from django.shortcuts import redirect
from django.views.decorators.http import require_POST

from core.access import require
from core.audit import audit

from . import services
from .models import DEFAULT_PORTS, TargetType

PREVIEW_ROWS = 200


class SingleTargetForm(forms.Form):
    hostname = forms.CharField(label="Host name or IP", max_length=255,
                               widget=forms.TextInput(attrs={"placeholder": "db-01.example.internal or 10.0.3.14"}))
    address = forms.CharField(label="Address (optional)", max_length=255, required=False,
                              help_text="Scrape this IP instead of resolving the name.")
    target_type = forms.ChoiceField(label="Type", choices=TargetType.choices, initial=TargetType.LINUX)
    port = forms.IntegerField(label="Port", required=False, min_value=1, max_value=65535,
                              help_text="Empty: the standard port for the type.")

    def clean_hostname(self):
        host, _ = services._clean_host(self.cleaned_data.get("hostname"))
        if not host:
            raise forms.ValidationError("Use letters, digits, dot, dash or underscore, or an IP address.")
        return host

    def clean_address(self):
        raw = (self.cleaned_data.get("address") or "").strip()
        if not raw:
            return ""
        host, _ = services._clean_host(raw)
        if not host:
            raise forms.ValidationError("That is not a valid address.")
        return host


def classify(run, rows):
    """For each parsed row: what adding it would do. Returns (annotated rows, counts)."""
    existing = {t.hostname: t for t in run.targets.all()}
    room = services.MAX_TARGETS_PER_RUN - len(existing)
    counts = {"add": 0, "update": 0, "same": 0, "over": 0}
    out = []
    for row in rows:
        target = existing.get(row["hostname"])
        if target is None:
            if room <= 0:
                action = "over"
            else:
                action, room = "add", room - 1
        else:
            same = (target.target_type == row["target_type"] and target.port == row["port"]
                    and (not row["address"] or target.address == row["address"]))
            action = "same" if same else "update"
        counts[action] += 1
        out.append({**row, "action": action,
                    "type_label": TargetType(row["target_type"]).label})
    return out, counts


def normalised_text(rows):
    """The rows as a header CSV the ordinary add action reads back to the same rows."""
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow(["hostname", "address", "type", "port"])
    for row in rows:
        writer.writerow([row["hostname"], row["address"], row["target_type"], row["port"]])
    return buf.getvalue()


def _read_upload(uploaded):
    """(text, error): decoded CSV text, refusing what is too large or not text instead of truncating."""
    if not uploaded:
        return "", ""
    if uploaded.size > services.MAX_PASTE_BYTES:
        return "", (f"That file is {uploaded.size // 1024} KB; the limit is {services.MAX_PASTE_BYTES // 1024} KB. "
                    "Split it into smaller files.")
    head = uploaded.read(4096)
    uploaded.seek(0)
    if b"\x00" in head:
        return "", "That does not look like a text or CSV file."
    try:
        text = services.csv_upload_text(uploaded)
    except ValueError as exc:
        return "", str(exc)
    if not text.strip():
        return "", "The file is empty or is not readable text."
    return text, ""


def _guard(request, pk):
    from . import views
    run = views._run(request, pk)
    views._need_admin(request, run.site)
    return views, run


@require("view")
@require_POST
def add_single(request, pk):
    views, run = _guard(request, pk)
    form = SingleTargetForm(request.POST)
    if not form.is_valid():
        return views._render_step(request, run, "targets", single_form=form)
    data = form.cleaned_data
    ttype = data["target_type"]
    port = data.get("port") or DEFAULT_PORTS.get(ttype, 9100)
    created, updated, skipped = services.add_targets(
        run, [{"hostname": data["hostname"], "address": data["address"], "target_type": ttype, "port": port}],
        request=request)
    if created:
        messages.success(request, f"{data['hostname']} added.")
    elif updated:
        messages.success(request, f"{data['hostname']} was already on this run: its settings were updated.")
    elif skipped and run.targets.count() >= services.MAX_TARGETS_PER_RUN:
        messages.error(request, f"A run holds at most {services.MAX_TARGETS_PER_RUN} targets.")
    else:
        messages.info(request, f"{data['hostname']} is already on this run as given.")
    return redirect("onboarding:step", pk=run.pk, name="targets")


@require("view")
@require_POST
def preview(request, pk):
    """Dry run of the paste / CSV: shows what would happen and writes nothing."""
    views, run = _guard(request, pk)
    from .forms import TargetsForm
    form = TargetsForm(request.POST, request.FILES)
    if not form.is_valid():
        return views._render_step(request, run, "targets", form=form)
    data = form.cleaned_data
    default_type = data.get("target_type") or services.default_target_type(run.kind)
    csv_text, upload_error = _read_upload(data.get("csv_file"))
    if upload_error:
        form.add_error("csv_file", upload_error)
        return views._render_step(request, run, "targets", form=form)
    rows, bad, lines = [], [], 0
    seen = set()
    for blob in (data.get("hosts") or "", csv_text):
        if not blob.strip():
            continue
        lines += len([ln for ln in blob.replace("\r", "\n").split("\n") if ln.strip()])
        parsed, failed = services.parse_target_list(blob, default_type, data.get("port"))
        for row in parsed:
            if row["hostname"] not in seen:
                seen.add(row["hostname"])
                rows.append(row)
        bad.extend(failed)
    annotated, counts = classify(run, rows)
    audit(request, "onboarding.targets.preview", run, site=run.site, rows=len(rows), unreadable=len(bad))
    result = {
        "rows": annotated[:PREVIEW_ROWS], "shown": min(len(annotated), PREVIEW_ROWS), "total": len(annotated),
        "counts": counts, "bad": bad[:20], "n_bad": len(bad), "lines": lines,
        "confirm_text": normalised_text([r for r in annotated if r["action"] != "over"]),
        "limit": services.MAX_TARGETS_PER_RUN, "has_work": bool(counts["add"] or counts["update"]),
    }
    if not annotated:
        messages.error(request, "No usable host was found in what you pasted.")
    return views._render_step(request, run, "targets", form=form, preview=result)
