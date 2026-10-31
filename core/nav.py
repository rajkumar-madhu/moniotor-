"""Sidebar navigation (order follows the LinkedEye design). `url` is a URL name;
items without one open the "not built yet" page so the menu never has dead links."""

NAV = [
    {"label": "Dashboard", "icon": "home", "url": "dashboard"},
    {"label": "Service Desk", "icon": "headset", "children": [
        {"label": "Tickets"}, {"label": "Incidents", "url": "incidents"},
        {"label": "Problems"}, {"label": "Changes"}, {"label": "Service Catalog"},
    ]},
    {"label": "Assets", "icon": "box", "children": [{"label": "IT Assets"}, {"label": "Software"}, {"label": "Licenses"}, {"label": "Lifecycle"}]},
    {"label": "CMDB", "icon": "database", "children": [{"label": "Topology"}, {"label": "Configuration Items"}, {"label": "Relationships"}]},
    {"label": "Monitoring", "icon": "gauge", "children": [
        {"label": "Heatmap", "url": "heatmap"},
        {"label": "Infrastructure"}, {"label": "Nagios Status"}, {"label": "Prometheus Metrics"},
        {"label": "Alert Manager"}, {"label": "Telnet Monitoring"}, {"label": "Uptime Checks"},
        {"label": "Metrics Explorer"}, {"label": "Service Topology"},
    ]},
    {"label": "Network Devices", "icon": "network", "children": [
        {"label": "Switches"}, {"label": "Routers"},
        {"label": "Firewalls", "url": "firewall"},
        {"label": "Load Balancers"}, {"label": "Wireless"}, {"label": "Physical Servers"},
        {"label": "Virtual Machines"}, {"label": "All Devices"},
    ]},
    {"label": "Domains", "icon": "browser", "children": [
        {"label": "Overview", "url": "domains:overview"},
        {"label": "Domain List", "url": "domains:list"},
        {"label": "DNS Records", "url": "domains:dns"},
        {"label": "SSL Certificates", "url": "domains:ssl"},
        {"label": "Uptime Monitoring"}, {"label": "Performance"}, {"label": "Security"}, {"label": "Export Reports"},
    ]},
    {"label": "URL Checker", "icon": "link", "url": "urlchecker:index", "chev": True},
    {"label": "Templates", "icon": "template", "chev": True},
    {"label": "All Exporters", "icon": "exporters", "url": "exporters", "chev": True},
    {"label": "Logs & Events", "icon": "logs", "chev": True},
    {"label": "BOD / EOD", "icon": "calendar", "children": [{"label": "Status Dashboard"}, {"label": "Key Validation"}, {"label": "Execution History"}]},
    {"label": "Automation", "icon": "bot", "children": [{"label": "Runbooks"}, {"label": "StackStorm"}, {"label": "Workflows"}]},
    {"label": "Analytics", "icon": "analytics", "chev": True},
    {"label": "Reports", "icon": "clipboard", "chev": True},
    {"label": "Administration", "icon": "settings", "url": "admin:index", "chev": True},
]
