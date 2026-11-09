"""The context of the Hardware tab on a physical server's device page (called lazily from ``network.views.detail``)."""
from django.urls import reverse

from core.access import can_on

from . import services, snapshot

REFRESH_SECONDS = 30


def context(request, device):
    """{"hw": {...}} for ``hardware/_tab.html``. Never raises for a missing endpoint (the tab then offers to add one)."""
    ep = services.endpoints_for(request.user).filter(device=device).first()
    admin = can_on(request.user, "admin", device.site)
    operate = can_on(request.user, "operate", device.site)
    key = snapshot.range_key(request.GET.get("hw_range"))
    pk = device.pk
    data = snapshot.build(ep, key, {"operate": operate, "admin": admin}) if ep is not None else None
    config = None
    if ep is not None:
        config = {"urls": {"live": reverse("hardware:live", args=[pk]), "refresh": reverse("hardware:refresh", args=[pk]),
                           "vendor": reverse("hardware:vendor", args=[pk]), "inventory": reverse("hardware:inventory", args=[pk]),
                           "events": reverse("hardware:events", args=[pk]), "export": reverse("hardware:export", args=[pk]),
                           "alerts": reverse("hardware:alerts", args=[pk]), "request": reverse("hardware:request_new", args=[pk]),
                           "requests": reverse("hardware:requests"), "endpoint": reverse("hardware:endpoint_edit", args=[ep.pk])},
                  "interval": REFRESH_SECONDS, "range": key, "ranges": [[k, v] for k, v in snapshot.RANGE_LABELS.items()],
                  "actions": [["power_on", "Power on"], ["graceful_shutdown", "Graceful shutdown"], ["force_off", "Force power off"],
                              ["update_firmware", "Update firmware"]],
                  "steps": dict(services.MANUAL_STEPS)}
    return {"hw": {"endpoint": ep, "data": data, "config": config, "can_admin": admin, "can_operate": operate, "range": key,
                   "new_url": f"{reverse('hardware:endpoint_new')}?device={pk}", "endpoints_url": reverse("hardware:endpoints")}}
