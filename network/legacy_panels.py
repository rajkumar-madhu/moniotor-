"""Map network devices to legacy Global LinkedEye assets (iframeGraphs + template/DIRECT).

Legacy entity UI loads SVG front panels from ``backend/iframeGraphs/<name>.j2`` via
``/getfilecontents`` and builds Neo4j nodes from ``template/DIRECT/*.yaml`` at onboard time.
"""
import re
from functools import lru_cache
from pathlib import Path

from django.conf import settings

_IFRAME_DIR = Path(settings.BASE_DIR) / "backend" / "iframeGraphs"
_TEMPLATE_DIRECT = Path(settings.BASE_DIR) / "backend" / "template" / "DIRECT"


def _norm(text):
    s = (text or "").upper()
    s = re.sub(r"[^A-Z0-9]+", "", s)
    return s


@lru_cache(maxsize=1)
def iframe_panel_names():
    if not _IFRAME_DIR.is_dir():
        return ()
    return tuple(sorted(p.name for p in _IFRAME_DIR.glob("*.j2") if not p.name.endswith("_stack.j2")))


@lru_cache(maxsize=1)
def iframe_stack_names():
    if not _IFRAME_DIR.is_dir():
        return ()
    return tuple(sorted(p.name for p in _IFRAME_DIR.glob("*_stack.j2")))


@lru_cache(maxsize=1)
def direct_template_names():
    if not _TEMPLATE_DIRECT.is_dir():
        return ()
    return tuple(sorted(p.name for p in _TEMPLATE_DIRECT.glob("*.yaml")))


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


def _best_match(candidates, hay_t):
    if not hay_t:
        return ""
    best, best_score = "", 0
    for name in candidates:
        stem = Path(name).stem.replace("_stack", "")
        key = _norm(stem)
        if key and all(t in key for t in hay_t if len(t) >= 4):
            if len(key) > best_score:
                best, best_score = name, len(key)
        overlap = len(_stem_tokens(stem) & hay_t)
        if overlap > best_score:
            best, best_score = name, overlap
    return best if best_score >= 2 else (best if best_score >= 1 and any(t.isdigit() or any(c.isdigit() for c in t) for t in hay_t) else "")


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
