"""Sidebar navigation: the single source of truth for the menu.

Shape (unchanged from the flat menu, plus two optional keys):

* a top-level entry is ``{"label", "icon", "url"}`` (one link) or ``{"label", "icon", "children": [...]}`` (a group);
* ``"section"`` (a key of SECTIONS) files the entry under a labelled, collapsible section of the sidebar. Entries
  without one fall into the last section;
* ``"badge"`` (a key of core.overview.nav_badges) puts a count on the entry in the sidebar.

``url`` is a URL name. An entry whose URL name does not resolve yet opens the "not built yet" page, so a module can
be added here before its view exists. Labels must be unique among the links of one section (the menu filter shows
"Group › Label"), so rename a link rather than reuse a label. See docs/UI.md for how to add an entry.
"""

SECTIONS = [
    ("operate", "Operate"),
    ("observe", "Observe"),
    ("infrastructure", "Infrastructure"),
    ("assets", "Assets"),
    ("insights", "Insights"),
    ("platform", "Platform"),
]

NAV = [
    # ---------------------------------------------------------------- OPERATE: what needs me, and the response to it
    {"section": "operate", "label": "Dashboard", "icon": "home", "url": "dashboard"},
    {"section": "operate", "label": "Ops Agent", "icon": "bot", "url": "agent:chat"},
    {"section": "operate", "label": "Service Desk", "icon": "headset", "badge": "incidents", "children": [
        {"label": "Tickets", "url": "servicedesk:tickets"},
        {"label": "Incidents", "url": "servicedesk:incidents"},
        {"label": "Problems", "url": "servicedesk:problems"},
        {"label": "Changes", "url": "servicedesk:changes"},
        {"label": "Service Catalog", "url": "servicedesk:catalog"},
        {"label": "SLA Policies", "url": "servicedesk:sla"},
    ]},
    {"section": "operate", "label": "BOD / EOD", "icon": "calendar", "children": [
        {"label": "Status Dashboard", "url": "bodeod:status"}, {"label": "Key Validation", "url": "bodeod:keys"},
        {"label": "Execution History", "url": "bodeod:history"},
        {"label": "Checklist", "url": "bodeod:checklist"}, {"label": "Holidays", "url": "bodeod:holidays"},
    ]},
    {"section": "operate", "label": "Automation", "icon": "zap", "children": [
        {"label": "Runbooks", "url": "automation:runbooks"}, {"label": "StackStorm", "url": "automation:stackstorm"},
        {"label": "Workflows", "url": "automation:workflows"},
    ]},

    # ---------------------------------------------------------------- OBSERVE: is it healthy, and what is it doing
    {"section": "observe", "label": "Overview", "icon": "activity", "url": "telemetry:overview"},
    {"section": "observe", "label": "Fleet", "icon": "radar", "url": "telemetry:fleet"},
    {"section": "observe", "label": "Heatmap", "icon": "grid", "url": "cmdb:heatmap"},
    {"section": "observe", "label": "Infrastructure", "icon": "layers", "url": "telemetry:infrastructure"},
    {"section": "observe", "label": "Metrics Explorer", "icon": "bar", "url": "telemetry:explorer"},
    {"section": "observe", "label": "Logs & Events", "icon": "list", "url": "observe:logs"},
    {"section": "observe", "label": "All Exporters", "icon": "exporters", "url": "observe:exporters"},
    {"section": "observe", "label": "Alert Manager", "icon": "alarm", "url": "telemetry:alertmanager", "badge": "alerts"},
    {"section": "observe", "label": "Checks", "icon": "check-circle", "badge": "checks", "children": [
        {"label": "Uptime Checks", "url": "tcpcheck:uptime"},
        {"label": "URL Checker", "url": "urlchecker:index"},
        {"label": "TCP / Telnet Checks", "url": "tcpcheck:index"},
    ]},
    {"section": "observe", "label": "Metric Sources", "icon": "sliders", "children": [
        {"label": "Prometheus Metrics", "url": "telemetry:prometheus"},
        {"label": "Nagios Status", "url": "telemetry:nagios"},
        {"label": "Target Discovery", "url": "telemetry:discovery"},
    ]},
    # Application performance: the adapter status matrix, process status and execution logs pushed by the customer's cron toolkit
    {"section": "observe", "label": "APM", "icon": "heartbeat", "children": [
        {"label": "APM Dashboard", "url": "apm:index"},
        {"label": "Sources", "url": "apm:sources"},
    ]},
    {"section": "observe", "label": "Domains", "icon": "browser", "children": [
        {"label": "Domains Overview", "url": "domains:overview"},
        {"label": "Domain List", "url": "domains:list"},
        {"label": "DNS Records", "url": "domains:dns"},
        {"label": "SSL Certificates", "url": "domains:ssl"},
        {"label": "Uptime Monitoring", "url": "domains:uptime"},
        {"label": "Performance", "url": "domains:performance"},
        {"label": "Security", "url": "domains:security"},
        {"label": "Export Reports", "url": "domains:export_reports"},
    ]},

    # ---------------------------------------------------------------- INFRASTRUCTURE: the estate itself
    {"section": "infrastructure", "label": "Network Devices", "icon": "network", "children": [
        {"label": "Switches", "url": "network:switches"}, {"label": "Routers", "url": "network:routers"},
        {"label": "Firewalls", "url": "network:firewalls"}, {"label": "Load Balancers", "url": "network:load_balancers"},
        {"label": "Wireless", "url": "network:wireless"}, {"label": "Server Devices", "url": "network:physical_servers"},
        {"label": "VM Devices", "url": "network:virtual_machines"},
        {"label": "All Devices", "url": "network:all"},
        {"label": "Fabric map", "url": "network:fabric"},
        {"label": "Network Discovery", "url": "network:discovery"},
        {"label": "Architecture", "url": "architecture:overview"},
    ]},
    {"section": "infrastructure", "label": "Servers & Compute", "icon": "cpu", "children": [
        {"label": "Compute Overview", "url": "compute:overview"},
        {"label": "Servers", "url": "compute:servers"},
        {"label": "Virtual Machines", "url": "compute:vms"},
        {"label": "Clusters", "url": "compute:clusters"},
        {"label": "Kubernetes (live)", "url": "compute:kubernetes"},
    ]},
    # vCenter, read-only: VM detail data, datastores, the unmatched-VM decisions and the control requests (records only)
    {"section": "infrastructure", "label": "Hypervisor", "icon": "cloud", "children": [
        {"label": "Datastores", "url": "hypervisor:datastores"},
        {"label": "Unmatched VMs", "url": "hypervisor:unmatched"},
        {"label": "VM Requests", "url": "hypervisor:requests"},
        {"label": "vCenter Sources", "url": "hypervisor:sources"},
    ]},
    # Kubernetes client inventory (the k8s module): the imported estate, not live cluster health.
    {"section": "infrastructure", "label": "Kubernetes", "icon": "hexagon", "children": [
        {"label": "Inventory Overview", "url": "k8s:overview"},
        {"label": "Clients", "url": "k8s:clients"},
        {"label": "Namespaces", "url": "k8s:namespaces"},
        {"label": "Nodes", "url": "k8s:nodes"},
        {"label": "Workloads", "url": "k8s:workloads"},
        {"label": "Services", "url": "k8s:services"},
        {"label": "Ingresses", "url": "k8s:ingresses"},
        {"label": "Volumes", "url": "k8s:volumes"},
        {"label": "Certificates", "url": "k8s:certificates"},
        {"label": "Secret References", "url": "k8s:secrets"},
        {"label": "Tools", "url": "k8s:tools"},
        {"label": "Monitoring", "url": "k8s:monitoring"},
        {"label": "Issues", "url": "k8s:issues"},
        {"label": "Imports", "url": "k8s:imports"},
    ]},
    # Server hardware over Redfish (Dell iDRAC, HPE iLO), read-only: the per-server tab lives on the physical server's device page
    {"section": "infrastructure", "label": "Server Hardware", "icon": "hdd", "children": [
        {"label": "BMC Endpoints", "url": "hardware:endpoints"},
        {"label": "Control Requests", "url": "hardware:requests"},
    ]},
    {"section": "infrastructure", "label": "IPAM", "icon": "globe", "children": [
        {"label": "IPAM Overview", "url": "ipam:overview"},
        {"label": "Subnets", "url": "ipam:subnets"},
        {"label": "IP Addresses", "url": "ipam:addresses"},
        {"label": "VLANs", "url": "ipam:vlans"},
        {"label": "DNS Zones", "url": "ipam:dns"},
    ]},
    {"section": "infrastructure", "label": "Data Centre", "icon": "server", "children": [
        {"label": "Data Centre Overview", "url": "dcim:overview"},
        {"label": "Rooms & Racks", "url": "dcim:racks"},
        {"label": "Power", "url": "dcim:power"},
        {"label": "Cooling", "url": "dcim:cooling"},
    ]},

    # ---------------------------------------------------------------- ASSETS: what we own
    {"section": "assets", "label": "IT Assets", "icon": "box", "url": "cmdb:assets"},
    {"section": "assets", "label": "Software", "icon": "app", "url": "cmdb:software"},
    {"section": "assets", "label": "Licenses", "icon": "key", "url": "cmdb:licenses"},
    {"section": "assets", "label": "Lifecycle", "icon": "clock", "url": "cmdb:lifecycle"},
    {"section": "assets", "label": "CMDB", "icon": "database", "children": [
        {"label": "Topology", "url": "cmdb:topology"}, {"label": "Configuration Items", "url": "cmdb:items"},
        {"label": "Relationships", "url": "cmdb:relationships"},
    ]},

    # ---------------------------------------------------------------- INSIGHTS
    {"section": "insights", "label": "Analytics", "icon": "analytics", "url": "reports:analytics"},
    {"section": "insights", "label": "Reports", "icon": "clipboard", "url": "reports:index"},

    # ---------------------------------------------------------------- PLATFORM: setting the product up
    {"section": "platform", "label": "Templates", "icon": "template", "url": "automation:templates"},
    {"section": "platform", "label": "Onboarding", "icon": "plug", "url": "onboarding:index"},
    {"section": "platform", "label": "Administration", "icon": "settings", "children": [
        {"label": "Settings", "url": "administration:index"},
        {"label": "Service Discovery", "url": "onboarding:sd_page"},
    ]},
]
