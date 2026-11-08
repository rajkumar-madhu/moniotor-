"""What gets installed on a server, and the exact text an operator can paste instead.

Everything here is built in code from constants. Nothing in this module (or anything that runs a
command on a host) ever takes text from a form: the only inputs are the target type and a validated
port number. The same script is shown for copy-and-paste and run over SSH, so what the operator
reviews is what LinkedEye would run.

Both scripts follow the same rules:

* idempotent: an exporter that is already running is left alone, and an existing install that is not
  running is never overwritten;
* verify first: the download is checked against a SHA-256 pinned in this file before anything on the
  host changes, so a failed download or a mismatch leaves the host untouched;
* roll back: if the service does not come up after the files were written, the files, the unit and
  any user the script created are removed again.

Versions and checksums are pinned on purpose (the checksums were taken from the projects' signed
release pages); bump them together.
"""
import base64
import re

from .models import TargetType

NODE_EXPORTER_VERSION = "1.9.1"
NODE_EXPORTER_SHA256 = {
    "amd64": "becb950ee80daa8ae7331d77966d94a611af79ad0d3307380907e0ec08f5b4e8",
    "arm64": "848f139986f63232ced83babe3cad1679efdbb26c694737edc1f4fbd27b96203",
    "armv7": "f04c95c9c474516b06ab10e34a663992ccad4c9185596e404f02790ec23d83d9",
}
#: `uname -m` value -> release architecture name
NODE_EXPORTER_ARCH = {"x86_64": "amd64", "amd64": "amd64", "aarch64": "arm64", "arm64": "arm64",
                      "armv7l": "armv7"}
NODE_EXPORTER_BASE = "https://github.com/prometheus/node_exporter/releases/download"

WINDOWS_EXPORTER_VERSION = "0.30.5"
WINDOWS_EXPORTER_SHA256 = {
    "amd64": "59483778da38cb5d74da4b8b75cf4e3f8b3ec1f833819ab37b290d7b8a6925f7",
    "arm64": "f37737993049901684be9daf763d54f74e7dd37f2e2c6ef57a738317aa77e30a",
}
WINDOWS_EXPORTER_BASE = "https://github.com/prometheus-community/windows_exporter/releases/download"

#: services that mean "a node exporter is already managed by systemd on this host"
LINUX_SERVICES = ("node_exporter", "prometheus-node-exporter")

_ARCH_RE = re.compile(r"^[a-z0-9_]{1,12}$")


def supports(target_type):
    return target_type in (TargetType.LINUX, TargetType.WINDOWS)


def valid_port(port):
    try:
        port = int(port)
    except (TypeError, ValueError):
        raise ValueError("not a valid port") from None
    if not 1 <= port <= 65535:
        raise ValueError("not a valid port")
    return port


# ----------------------------------------------------------------------------- Linux
def node_exporter_url(arch="amd64", version=NODE_EXPORTER_VERSION):
    return (f"{NODE_EXPORTER_BASE}/v{version}/node_exporter-{version}.linux-{arch}.tar.gz")


def systemd_unit_lines(port):
    """The exact systemd unit LinkedEye writes (also shown to the operator)."""
    port = valid_port(port)
    return [
        "[Unit]",
        "Description=Prometheus Node Exporter",
        "After=network-online.target",
        "Wants=network-online.target",
        "",
        "[Service]",
        "User=node_exporter",
        "Group=node_exporter",
        "Type=simple",
        f"ExecStart=/usr/local/bin/node_exporter --web.listen-address=:{port}",
        "Restart=on-failure",
        "RestartSec=5",
        "NoNewPrivileges=true",
        "ProtectHome=true",
        "ProtectSystem=strict",
        "",
        "[Install]",
        "WantedBy=multi-user.target",
    ]


def systemd_unit(port):
    return "\n".join(systemd_unit_lines(port)) + "\n"


def _sh_quote(line):
    # unit lines never contain a single quote; refuse rather than escape if one ever does
    if "'" in line:
        raise ValueError("unsafe line")
    return f"'{line}'"


def linux_install_script(port=9100):
    """A POSIX sh script that installs node_exporter as a systemd service. Run as root."""
    port = valid_port(port)
    arch_cases = []
    for uname, arch in sorted(NODE_EXPORTER_ARCH.items()):
        assert _ARCH_RE.match(uname) and _ARCH_RE.match(arch)
        arch_cases.append((uname, arch))
    case_lines = []
    done = set()
    for uname, arch in arch_cases:
        if arch in done:
            continue
        done.add(arch)
        names = "|".join(u for u, a in arch_cases if a == arch)
        case_lines.append(f"  {names}) ARCH={arch}; SHA256={NODE_EXPORTER_SHA256[arch]} ;;")
    unit = " ".join(_sh_quote(line) for line in systemd_unit_lines(port))
    return f"""set -eu
VERSION='{NODE_EXPORTER_VERSION}'
BIN=/usr/local/bin/node_exporter
UNIT=/etc/systemd/system/node_exporter.service
if [ "$(id -u)" != 0 ]; then echo "this must run as root" >&2; exit 1; fi
command -v systemctl >/dev/null 2>&1 || {{ echo "systemd (systemctl) is required" >&2; exit 1; }}
for svc in {" ".join(LINUX_SERVICES)}; do
  if systemctl is-active --quiet "$svc" 2>/dev/null; then echo "$svc is already running; nothing to do"; exit 0; fi
done
if [ -e "$BIN" ] || [ -e "$UNIT" ]; then
  echo "node_exporter is already installed but not running; leaving it as it is (try: systemctl enable --now node_exporter)" >&2
  exit 1
fi
case "$(uname -m)" in
{chr(10).join(case_lines)}
  *) echo "unsupported CPU architecture: $(uname -m)" >&2; exit 1 ;;
esac
FILE="node_exporter-${{VERSION}}.linux-${{ARCH}}.tar.gz"
URL="{NODE_EXPORTER_BASE}/v${{VERSION}}/${{FILE}}"
TMP="$(mktemp -d)"
COMMITTED=0
NEWUSER=0
cleanup() {{
  rc=$?
  rm -rf "$TMP"
  if [ "$rc" != 0 ] && [ "$COMMITTED" = 1 ]; then
    systemctl disable --now node_exporter >/dev/null 2>&1 || true
    rm -f "$UNIT" "$BIN"
    systemctl daemon-reload >/dev/null 2>&1 || true
    if [ "$NEWUSER" = 1 ]; then userdel node_exporter >/dev/null 2>&1 || true; fi
    echo "install failed; the changes made so far were rolled back" >&2
  fi
  exit "$rc"
}}
trap cleanup EXIT
if command -v curl >/dev/null 2>&1; then curl -fsSL --proto '=https' --tlsv1.2 -o "$TMP/$FILE" "$URL"
elif command -v wget >/dev/null 2>&1; then wget -q -O "$TMP/$FILE" "$URL"
else echo "curl or wget is required to download node_exporter" >&2; exit 1; fi
command -v sha256sum >/dev/null 2>&1 || {{ echo "sha256sum is required to verify the download" >&2; exit 1; }}
echo "$SHA256  $TMP/$FILE" | sha256sum -c - >/dev/null 2>&1 || {{ echo "checksum mismatch for $FILE; nothing was installed" >&2; exit 1; }}
tar -xzf "$TMP/$FILE" -C "$TMP" --strip-components=1 "node_exporter-${{VERSION}}.linux-${{ARCH}}/node_exporter"
COMMITTED=1
if ! id node_exporter >/dev/null 2>&1; then
  useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
  NEWUSER=1
fi
install -m 0755 -o root -g root "$TMP/node_exporter" "$BIN"
printf '%s\\n' {unit} > "$UNIT"
chmod 0644 "$UNIT"
systemctl daemon-reload
systemctl enable --now node_exporter
sleep 2
systemctl is-active --quiet node_exporter || {{ echo "node_exporter did not start: $(systemctl status node_exporter --no-pager -l 2>&1 | tail -n 5)" >&2; exit 1; }}
echo "node_exporter ${{VERSION}} installed and listening on :{port}"
"""


def linux_command(port=9100):
    """The block an operator pastes into a root shell on the host (the same script LinkedEye would run)."""
    # the script is passed as an argument, not on stdin, so nothing it runs can swallow the rest of it
    return "sh -c \"$(cat <<'LINKEDEYE_INSTALL'\n" + linux_install_script(port) + "LINKEDEYE_INSTALL\n)\"\n"


def linux_firewall_hints(port):
    port = valid_port(port)
    return [
        f"ufw allow from <prometheus-ip> to any port {port} proto tcp",
        f"firewall-cmd --permanent --add-rich-rule='rule family=ipv4 source address=<prometheus-ip> port port={port} protocol=tcp accept' && firewall-cmd --reload",
    ]


# ----------------------------------------------------------------------------- Windows
def windows_exporter_url(arch="amd64", version=WINDOWS_EXPORTER_VERSION):
    return f"{WINDOWS_EXPORTER_BASE}/v{version}/windows_exporter-{version}-{arch}.msi"


def windows_install_script(port=9182):
    """A PowerShell script that installs windows_exporter from the pinned MSI. Run elevated."""
    port = valid_port(port)
    sha = WINDOWS_EXPORTER_SHA256
    return f"""$ErrorActionPreference = 'Stop'
$ProgressPreference = 'SilentlyContinue'
$version = '{WINDOWS_EXPORTER_VERSION}'
$port = {port}
if (-not ([Security.Principal.WindowsPrincipal][Security.Principal.WindowsIdentity]::GetCurrent()).IsInRole([Security.Principal.WindowsBuiltInRole]::Administrator)) {{ throw 'this must run as an administrator' }}
$svc = Get-Service -Name windows_exporter -ErrorAction SilentlyContinue
if ($svc) {{ if ($svc.Status -eq 'Running') {{ Write-Output 'windows_exporter is already running; nothing to do'; exit 0 }}; throw 'windows_exporter is already installed but not running; leaving it as it is' }}
if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') {{ $arch = 'arm64'; $sha256 = '{sha["arm64"]}' }} else {{ $arch = 'amd64'; $sha256 = '{sha["amd64"]}' }}
$file = Join-Path $env:TEMP "windows_exporter-$version-$arch.msi"
[Net.ServicePointManager]::SecurityProtocol = [Net.SecurityProtocolType]::Tls12
Invoke-WebRequest -UseBasicParsing -Uri "{WINDOWS_EXPORTER_BASE}/v$version/windows_exporter-$version-$arch.msi" -OutFile $file
if ((Get-FileHash -Algorithm SHA256 -Path $file).Hash.ToLower() -ne $sha256) {{ Remove-Item $file -Force; throw 'checksum mismatch; nothing was installed' }}
$p = Start-Process -FilePath msiexec.exe -ArgumentList '/i', ('"' + $file + '"'), '/qn', "LISTEN_PORT=$port" -Wait -PassThru
if ($p.ExitCode -ne 0 -and $p.ExitCode -ne 3010) {{ Remove-Item $file -Force; throw ('msiexec failed with exit code ' + $p.ExitCode) }}
Start-Sleep -Seconds 3
if ((Get-Service -Name windows_exporter).Status -ne 'Running') {{ Start-Process -FilePath msiexec.exe -ArgumentList '/x', ('"' + $file + '"'), '/qn' -Wait; Remove-Item $file -Force; throw 'windows_exporter did not start; it was uninstalled again' }}
Remove-Item $file -Force
Write-Output "windows_exporter $version installed and listening on :$port"
"""


def windows_command(port=9182):
    """The block an operator pastes into an elevated PowerShell on the host."""
    return windows_install_script(port)


def windows_firewall_hints(port):
    port = valid_port(port)
    return [f'New-NetFirewallRule -DisplayName "Prometheus windows_exporter" -Direction Inbound -Protocol TCP -LocalPort {port} -RemoteAddress <prometheus-ip> -Action Allow']


def powershell_encoded(script):
    """The -EncodedCommand argument for a script (UTF-16LE, base64): no quoting can go wrong."""
    return base64.b64encode(script.encode("utf-16-le")).decode("ascii")


# ----------------------------------------------------------------------------- per target
def details(target_type, port):
    """Everything the operator needs to install the exporter by hand, or None for other target types.

    {exporter, version, command, shell, download_url, unit, port, firewall, notes}"""
    port = valid_port(port)
    if target_type == TargetType.LINUX:
        return {
            "exporter": "node_exporter", "version": NODE_EXPORTER_VERSION, "shell": "root shell (sh)",
            "command": linux_command(port), "download_url": node_exporter_url("amd64"),
            "download_urls": {a: node_exporter_url(a) for a in sorted(NODE_EXPORTER_SHA256)},
            "unit": systemd_unit(port), "unit_path": "/etc/systemd/system/node_exporter.service",
            "port": port, "firewall": linux_firewall_hints(port),
            "notes": "Run it as root. It verifies the download against a pinned SHA-256, writes the unit, "
                     "starts the service and undoes everything if the service does not come up.",
        }
    if target_type == TargetType.WINDOWS:
        return {
            "exporter": "windows_exporter", "version": WINDOWS_EXPORTER_VERSION, "shell": "elevated PowerShell",
            "command": windows_command(port), "download_url": windows_exporter_url("amd64"),
            "download_urls": {a: windows_exporter_url(a) for a in sorted(WINDOWS_EXPORTER_SHA256)},
            "unit": "", "unit_path": "",
            "port": port, "firewall": windows_firewall_hints(port),
            "notes": "Run it in an elevated PowerShell. It verifies the MSI against a pinned SHA-256, installs "
                     "the Windows service and uninstalls it again if the service does not start.",
        }
    return None


def command_for(target_type, port):
    info = details(target_type, port)
    return info["command"] if info else ""
