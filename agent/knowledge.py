"""Lexical (and optional vector) retrieval over KnowledgeChunk rows."""
import math
import re

from django.conf import settings
from django.db.models import Q
from django.utils import timezone

from core.access import scope_by_site
from core.overview import _safe, _url, model as overview_model, problems as overview_problems

from .models import KnowledgeChunk

TOKEN = re.compile(r"[a-z0-9][a-z0-9._-]{1,}")
STOP = frozenset("the a an of to for and or in on at is are was be from with by it this that".split())
CHUNK_CAP = 800
EVENT_CAP = 400
BODY_CAP = 800


def tokenize(text):
    return [t for t in TOKEN.findall((text or "").lower()) if t not in STOP]


def cosine(a, b):
    if not a or not b or len(a) != len(b):
        return 0.0
    dot = na = nb = 0.0
    for x, y in zip(a, b):
        try:
            xf, yf = float(x), float(y)
        except (TypeError, ValueError):
            return 0.0
        dot += xf * yf
        na += xf * xf
        nb += yf * yf
    if na <= 0 or nb <= 0:
        return 0.0
    return dot / (math.sqrt(na) * math.sqrt(nb))


def lexical_score(query, title, body):
    q = set(tokenize(query))
    if not q:
        return 0.0
    blob = set(tokenize(f"{title} {body}"))
    overlap = len(q & blob) / len(q)
    title_hit = 0.35 if any(t in (title or "").lower() for t in q) else 0.0
    return overlap + title_hit


def retrieve(user, site, query, limit=8):
    """Return scored snippets the user is allowed to see. Never unbounded."""
    from .llm import configured, embed

    q = (query or "").strip()
    if not q:
        return []
    qs = KnowledgeChunk.objects.filter(tenant_key=settings.LINKEDEYE_TENANT)
    qs = scope_by_site(qs, user)
    if site:
        qs = qs.filter(Q(site=site) | Q(site="") | Q(site="All Sites"))
    chunks = _safe(lambda: list(qs.order_by("-updated_at")[:CHUNK_CAP]), [])
    query_vec = []
    if configured() and any(c.embedding for c in chunks):
        try:
            query_vec = (embed([q]) or [None])[0] or []
        except Exception:
            query_vec = []
    scored = []
    for c in chunks:
        lex = lexical_score(q, c.title, c.body)
        vec = cosine(query_vec, c.embedding) if query_vec and c.embedding else 0.0
        score = (0.55 * vec + 0.45 * lex) if query_vec and c.embedding else lex
        if score <= 0:
            continue
        scored.append((score, {
            "source": c.source, "title": c.title, "body": (c.body or "")[:BODY_CAP],
            "site": c.site, "href": c.href, "score": round(score, 3),
        }))
    scored.sort(key=lambda x: x[0], reverse=True)
    return [row for _s, row in scored[:limit]]


def reindex(embed_vectors=True):
    """Rebuild knowledge chunks from live records. Bounded; never raises to the scheduler."""
    from .llm import configured, embed
    from .models import KnowledgeChunk as Chunk

    tenant = settings.LINKEDEYE_TENANT
    now = timezone.now()
    rows = _collect(tenant)[:CHUNK_CAP]
    existing = {(c.source, c.source_id): c for c in Chunk.objects.filter(tenant_key=tenant)}
    keep = set()
    saved = []
    need_embed = []
    for r in rows:
        key = (r["source"], r["source_id"])
        keep.add(key)
        chunk = existing.get(key) or Chunk(tenant_key=tenant, source=r["source"], source_id=r["source_id"])
        title, body, href, site = r["title"][:200], r["body"][:4000], r["href"][:400], r["site"]
        changed = (not chunk.pk) or chunk.title != title or chunk.body != body or chunk.site != site
        chunk.site, chunk.title, chunk.body, chunk.href, chunk.updated_at = site, title, body, href, now
        chunk.save()
        saved.append(chunk)
        if changed or not chunk.embedding:
            need_embed.append(chunk)
    stale = [c.pk for key, c in existing.items() if key not in keep]
    if stale:
        Chunk.objects.filter(pk__in=stale).delete()
    embedded = 0
    if embed_vectors and configured() and need_embed:
        for i in range(0, len(need_embed), 32):
            batch = need_embed[i:i + 32]
            try:
                vectors = embed([f"{c.title}\n{c.body}" for c in batch])
            except Exception:
                break
            for chunk, vec in zip(batch, vectors or []):
                if vec:
                    chunk.embedding = vec
                    chunk.save(update_fields=["embedding"])
                    embedded += 1
    return f"indexed {len(saved)} chunk(s), {embedded} with embeddings"


def _collect(tenant):
    rows = []
    Event = overview_model("core", "Event")
    if Event:
        qs = Event.objects.filter(tenant_key=tenant).exclude(severity="info").order_by("-occurred_at")[:EVENT_CAP]
        for e in _safe(lambda: list(qs), []):
            rows.append({
                "source": "event", "source_id": str(e.pk), "site": e.site or "",
                "title": e.title, "body": f"{e.severity} {e.source}: {e.message}",
                "href": _url("observe:events"),
            })
    for p in _safe(lambda: overview_problems(None, limit=80), []):
        rows.append({
            "source": "problem", "source_id": f"{p.get('kind')}:{p.get('name')}:{p.get('site')}"[:80],
            "site": p.get("site") or "", "title": f"{p.get('kind')} {p.get('name')}",
            "body": f"{p.get('status')} {p.get('detail') or ''}", "href": p.get("href") or "",
        })
    Ticket = overview_model("servicedesk", "Ticket")
    if Ticket:
        qs = Ticket.objects.filter(tenant_key=tenant).open().order_by("-opened_at")[:120]
        for t in _safe(lambda: list(qs), []):
            rows.append({
                "source": "ticket", "source_id": str(t.pk), "site": t.site or "",
                "title": f"{t.number} {t.title}",
                "body": f"{t.kind} {t.priority} {t.status} {t.ci_name} {t.description}",
                "href": t.get_absolute_url(),
            })
    Runbook = overview_model("automation", "Runbook")
    if Runbook:
        qs = Runbook.objects.filter(tenant_key=tenant).order_by("title")[:120]
        for r in _safe(lambda: list(qs), []):
            rows.append({
                "source": "runbook", "source_id": str(r.pk), "site": r.site or "",
                "title": r.title, "body": f"{r.service} {r.ci_name} {r.symptoms} {r.steps}",
                "href": r.get_absolute_url(),
            })
    return rows
