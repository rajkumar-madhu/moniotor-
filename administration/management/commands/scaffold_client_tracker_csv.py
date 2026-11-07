"""Emit a FinSpot-style client-wise tracker CSV for a legacy site slug (e.g. fs-dr-le)."""
from django.core.management.base import BaseCommand

from administration.finspot_naming import DOMAIN, lesites_url, portal_url, tool_url
from administration.lesites_defaults import TOOL_ROWS


class Command(BaseCommand):
    help = (
        "Print a client-wise tracker CSV template with FinSpot hostnames "
        "(portal fs-le-<env>.finspot.in, lesites ?site=fs-<env>-le). Password columns left empty."
    )

    def add_arguments(self, parser):
        parser.add_argument("site_slug", help="Legacy sitename, e.g. fs-dr-le")
        parser.add_argument("--client-name", default="", help="Display name (default derived from slug)")
        parser.add_argument("--environment", default="DR", help="ENV row label (DEV, DR, PROD, …)")
        parser.add_argument("--server-ip", default="", help="Optional server IP in meta row")
        parser.add_argument("--lead", default="", help="Optional lead name")
        parser.add_argument(
            "--extra-site-slug",
            action="append",
            default=[],
            help="Additional site slugs (repeat flag); first slug is primary portal row",
        )

    def handle(self, *args, **options):
        slug = options["site_slug"].strip().lower()
        extra = [s.strip().lower() for s in options["extra_site_slug"] if s.strip()]
        slugs = [slug] + [s for s in extra if s != slug]
        primary = slugs[0]
        portal = portal_url(primary)
        client_name = options["client_name"] or self._title(slug)
        client_slug = primary
        env = options["environment"].upper()
        ip = options["server_ip"]
        lead = options["lead"]

        lines = []
        lines.append(f"LinkedEye — {client_name}  [{env}],,,,,,,,,,")
        meta = f"Slug: {client_slug}   |   Base URL: {portal}"
        if ip:
            meta += f"   |   Server IP: {ip}"
        if lead:
            meta += f"   |   Lead: {lead}"
        meta += f"   |   Lesites: {lesites_url(primary)}"
        lines.append(meta + ",,,,,,,,,,")
        ip_part = f"IP: {ip}" if ip else "IP:"
        lines.append(f"ENV: {env},,STATUS: Active,,{ip_part},,REVIEWED: Pending,,,,")
        lines.append(",,,,,,,,,,")
        lines.append('"  TOOL & SERVICE ACCESS — URLs, Credentials, Endpoints",,,,,,,,,,')
        lines.append("#,Tool / Service,URL (HTTPS),Username,Password / Token,Auth Method,Port,Status,Notes,TLS,GIT")

        for i, (label, svc, port, auth) in enumerate(TOOL_ROWS, start=1):
            url = tool_url(primary, svc) if svc != "linkedeye" else portal
            notes = f"Host pattern *.{DOMAIN}; fill credentials in vault"
            if label == "LinkedEye UI":
                notes = lesites_url(primary)
            lines.append(f'{i},{label},{url},,,{auth},{port},Active,"{notes}",Yes,')

        lines.append(",,,,,,,,,,")
        lines.append("  SITE / ORGANIZATION SEEDS — Server Mapping,,,,,,,,,,")
        lines.append("#,Site Name,Slug,Server IP,Environment,FQDN,Status,BOD/EOD,Lead,Notes,")
        for i, s in enumerate(slugs, start=1):
            name = options["client_name"] if len(slugs) == 1 and options["client_name"] else self._title(s)
            fqdn = portal_host_only(s)
            lines.append(
                f'{i},{name},{s},{ip},{env},{fqdn},Active,Yes,{lead},Legacy lesites: {lesites_url(s)},'
            )

        self.stdout.write("\n".join(lines))

    def _title(self, slug):
        parts = slug.replace("-", " ").replace("_", " ").split()
        return " ".join(p.upper() if p in ("dr", "dev", "uat", "dx", "isv", "le") else p.title() for p in parts)


def portal_host_only(site_slug):
    from administration.finspot_naming import portal_host

    return portal_host(site_slug)
