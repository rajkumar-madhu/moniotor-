"""Administration views for the example-data lifecycle: remove it all (POST), hide the banner for this session (POST)."""
from django.contrib import messages
from django.shortcuts import redirect
from django.urls import reverse
from django.utils.http import url_has_allowed_host_and_scheme
from django.views.decorators.http import require_POST

from core import examples
from core.audit import audit

from .views import admin_required


@admin_required
@require_POST
def demo_remove(request):
    """Remove every example record. Needs the confirm checkbox; audited with the per-area counts."""
    back = reverse("administration:health") + "#demo-data"
    if request.POST.get("confirm") != "remove":
        messages.error(request, "Tick the confirmation box to remove the example data.")
        return redirect(back)
    results = examples.remove()
    removed = sum(r["removed"] for r in results)
    failed = [r for r in results if r["error"]]
    audit(request, "delete", object_type="core.exampledata", result="failed" if failed else "ok", removed=removed,
          areas={r["key"]: r["removed"] for r in results if r["before"]}, failed=[r["key"] for r in failed])
    examples.remember(request)
    if failed:
        messages.error(request, f"Removed {removed} example record(s), but {len(failed)} area(s) failed and kept their rows: "
                                + ", ".join(r["label"] for r in failed) + ".")
    elif removed:
        messages.success(request, f"Removed {removed} example record(s). Your own records were not touched.")
    else:
        messages.info(request, "There was no example data to remove.")
    return redirect(back)


@admin_required
@require_POST
def demo_dismiss(request):
    """Hide the 'Showing example data' banner for the rest of this session."""
    request.session[examples.DISMISS_KEY] = True
    nxt = request.POST.get("next", "")
    if not nxt or not url_has_allowed_host_and_scheme(nxt, allowed_hosts={request.get_host()}, require_https=request.is_secure()):
        nxt = reverse("dashboard")
    return redirect(nxt)
