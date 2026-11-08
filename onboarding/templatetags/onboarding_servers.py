"""Template helpers for the server verify / enable steps."""
from django import template

from onboarding import servers, ssh

register = template.Library()


@register.simple_tag
def install_info(target):
    """The by-hand install details for a Linux or Windows target (command, download URL, unit, port),
    or None for other target types. Built in code from constants; nothing from the target's facts."""
    try:
        return servers.install_details(target)
    except ValueError:
        return None


@register.simple_tag
def ssh_ready():
    """True when paramiko is importable (the SSH install can be offered)."""
    return ssh.available()


@register.filter
def fp_short(value):
    """A host-key fingerprint shortened for tables: SHA256:abcd…wxyz."""
    value = str(value or "")
    return value if len(value) <= 22 else f"{value[:14]}…{value[-6:]}"
