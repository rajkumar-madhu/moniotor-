"""The Kubernetes section's tab strip, defined in one place.

Every page of the module gets the same strip. A tab whose page has not been built yet keeps
``"enabled": False`` and is simply not rendered — so the strip never links to a URL name that does not
resolve, and the page that adds ``k8s:workloads`` only has to flip its own flag here.

Adding a page:

1. write the view and give it a URL name in ``k8s/urls.py``;
2. set ``"enabled": True`` on its row below (nothing else in the module changes).
"""

TABS = [
    {"key": "overview", "label": "Overview", "icon": "grid", "url": "k8s:overview", "enabled": True},
    {"key": "clients", "label": "Clients", "icon": "users", "url": "k8s:clients", "enabled": True},
    {"key": "namespaces", "label": "Namespaces", "icon": "layers", "url": "k8s:namespaces", "enabled": True},
    {"key": "nodes", "label": "Nodes", "icon": "server", "url": "k8s:nodes", "enabled": True},
    # Pages owned by the other sheets of the workbook. Flip "enabled" on when the page lands.
    {"key": "workloads", "label": "Workloads", "icon": "app", "url": "k8s:workloads", "enabled": True},
    {"key": "services", "label": "Services", "icon": "share", "url": "k8s:services", "enabled": True},
    {"key": "ingresses", "label": "Ingresses", "icon": "link", "url": "k8s:ingresses", "enabled": True},
    {"key": "volumes", "label": "Volumes", "icon": "hdd", "url": "k8s:volumes", "enabled": True},
    {"key": "certificates", "label": "Certificates", "icon": "shield", "url": "k8s:certificates",
     "enabled": True},
    {"key": "secrets", "label": "Secrets", "icon": "key", "url": "k8s:secrets", "enabled": True},
    {"key": "tools", "label": "Tools", "icon": "plug", "url": "k8s:tools", "enabled": True},
    {"key": "monitoring", "label": "Monitoring", "icon": "activity", "url": "k8s:monitoring", "enabled": True},
    {"key": "issues", "label": "Issues", "icon": "alert", "url": "k8s:issues", "enabled": True},
    {"key": "imports", "label": "Imports", "icon": "upload", "url": "k8s:imports", "enabled": True},
]


def enabled_tabs():
    """The tabs whose pages exist, in strip order."""
    return [tab for tab in TABS if tab.get("enabled")]


def pending_tabs():
    """The tabs whose pages are not built yet — shown as a note on the overview, never as links."""
    return [tab for tab in TABS if not tab.get("enabled")]


def is_enabled(key):
    return any(t["key"] == key and t.get("enabled") for t in TABS)
