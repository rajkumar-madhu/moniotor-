"""Map network devices to legacy Global LinkedEye assets (iframeGraphs + template/DIRECT).

Panels load from ``iframeGraphs/<name>.j2`` at the repo root (uploaded from Global LinkedEye),
with fallback to ``backend/iframeGraphs/``. Neo4j onboard templates live under
``template/DIRECT`` or ``backend/template/DIRECT``.
"""
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings


def _first_dir(*candidates):
    """Prefer repo-root legacy assets, then backend/ copies."""
    for path in candidates:
        if path.is_dir():
            return path
    return candidates[0]


def _iframe_dir():
    base = Path(settings.BASE_DIR)
    return _first_dir(base / "iframeGraphs", base / "backend" / "iframeGraphs")


def _template_direct_dir():
    base = Path(settings.BASE_DIR)
    return _first_dir(base / "template" / "DIRECT", base / "backend" / "template" / "DIRECT")


def _norm(text):
    s = (text or "").upper()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


@lru_cache(maxsize=1)
def iframe_panel_names():
    root = _iframe_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.glob("*.j2") if not p.name.endswith("_stack.j2")))


@lru_cache(maxsize=1)
def iframe_stack_names():
    root = _iframe_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.glob("*_stack.j2")))


@lru_cache(maxsize=1)
def direct_template_names():
    root = _template_direct_dir()
    if not root.is_dir():
        return ()
    return tuple(sorted(p.name for p in root.glob("*.yaml")))


def _stem_tokens(stem):
    parts = re.split(r"[_\s\-/]+", stem or "")
    return {t for p in parts if p for t in [_norm(p)] if t and len(t) >= 2}


def _hay_tokens(device):
    model = (device.model or device.name or "").strip()
    vendor = (device.vendor or "").strip()
    chunks = [model] if model else []
    if vendor and _norm(vendor) not in _norm(model):
        chunks.append(vendor)
    return _stem_tokens(" ".join(chunks))


def panel_token(device):
    """Placeholder token for legacy ``__IP__`` markers (safe in HTML ids)."""
    ip = (getattr(device, "mgmt_ip", None) or getattr(device, "hostname", None) or "").strip()
    if ip:
        return re.sub(r"[^0-9A-Za-z_]", "_", ip.replace(".", "_"))
    return f"d{device.pk}"


def load_panel_markup(device, filename):
    """Read a matched iframeGraphs ``.j2`` file and substitute ``__IP__`` for display."""
    if not filename or ".." in filename or "/" in filename or "\\" in filename:
        return None
    path = _iframe_dir() / filename
    if not path.is_file() or path.suffix != ".j2":
        return None
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    token = panel_token(device)
    return text.replace("__IP__", token)


def _best_match(candidates, hay_t):
    if not hay_t:
        return ""
    digit_tokens = [t for t in hay_t if any(c.isdigit() for c in t)]
    best, best_score = "", (-1, -1)
    for name in candidates:
        stem = Path(name).stem.replace("_stack", "")
        key = _norm(stem)
        if digit_tokens and not all(t in key for t in digit_tokens):
            continue
        overlap = len(_stem_tokens(stem) & hay_t)
        score = (overlap, len(key))
        if score > best_score:
            best, best_score = name, score
    overlap = best_score[0]
    return best if overlap >= 2 else (best if overlap >= 1 and digit_tokens else "")


def resolve_legacy_panel(device, stacked=False):
    """Return legacy asset filenames for a device, or None when nothing matches."""
    if not (device.model or device.vendor or device.name):
        return None
    hay_t = _hay_tokens(device)
    if not hay_t:
        return None
    pool = iframe_stack_names() if stacked else iframe_panel_names()
    j2 = _best_match(pool, hay_t)
    if not j2 and not stacked:
        j2 = _best_match(iframe_stack_names(), hay_t)
    yaml = _best_match(direct_template_names(), hay_t)
    if not j2 and not yaml:
        return None
    return {
        "iframe_j2": j2,
        "iframe_stack_j2": _best_match(iframe_stack_names(), hay_t) if j2 else "",
        "onboard_yaml": yaml,
        "neo4j_icon": j2 or (Path(yaml).stem + ".j2" if yaml else ""),
    }
