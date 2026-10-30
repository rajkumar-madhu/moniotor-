"""Sidebar navigation. `url` is a URL name; items without one open the
"not built yet" page so the menu never has dead links."""

NAV = [
    {"label": "Dashboard", "icon": "home", "url": "dashboard"},
    {"label": "Service Desk", "icon": "headset", "children": [
        {"label": "Incidents", "url": "incidents"},
        {"label": "Problems"}, {"label": "Changes"}, {"label": "Service Catalog"},
    ]},
    {"label": "Assets", "icon": "box", "children": [{"label": "IT Assets"}, {"label": "Software"}, {"label": "Licenses"}]},
    {"label": "CMDB", "icon": "database", "children": [{"label": "Topology"}, {"label": "Configuration Items"}, {"label": "Relationships"}]},
    {"label": "Monitoring", "icon": "activity", "children": [
        {"label": "Heatmap", "url": "heatmap"},
        {"label": "Infrastructure"}, {"label": "Nagios Status"}, {"label": "Prometheus Metrics"},
        {"label": "Alert Manager"}, {"label": "Telnet Monitoring"},
        {"label": "URL Checker", "url": "urlchecker:index"},
        {"label": "All Exporters", "url": "exporters"},
        {"label": "Metrics Explorer"},
    ]},
    {"label": "Network Devices", "icon": "network", "children": [
        {"label": "Switches"}, {"label": "Routers"},
        {"label": "Firewalls", "url": "firewall"},
        {"label": "Load Balancers"}, {"label": "Wireless"}, {"label": "All Devices"},
    ]},
    {"label": "Domains", "icon": "globe", "children": [
        {"label": "Overview", "url": "domains:overview"},
        {"label": "Domain List", "url": "domains:list"},
        {"label": "SSL Certificates", "url": "domains:ssl"},
    ]},
    {"label": "BOD / EOD", "icon": "calendar", "children": [{"label": "Status Dashboard"}, {"label": "Key Validation"}, {"label": "Execution History"}]},
    {"label": "Automation", "icon": "zap", "children": [{"label": "Runbooks"}, {"label": "StackStorm"}, {"label": "Workflows"}]},
    {"label": "Reports", "icon": "clipboard"},
    {"label": "Administration", "icon": "settings", "url": "admin:index"},
]
