"""One agent turn: persist the user message, call tools / the model, persist the reply."""
import json
import logging
from types import SimpleNamespace

from django.conf import settings
from django.utils import timezone

from . import knowledge, llm, tools
from .models import Message, Thread

log = logging.getLogger("linkedeye.agent")

INSTRUCTIONS = """You are LinkedEye Ops Agent, a read-only assistant for a multi-tenant infrastructure monitoring console.
Answer only from tool results. Never invent hosts, metrics, statuses, ticket numbers or sites.
If a tool returns nothing, say so and suggest a narrower search.
Cite record names and sites. Tools cannot change production systems.
The operator can only see the sites they are a member of; do not speculate about other tenants.
Keep answers concise and operational."""

HISTORY_LIMIT = 16
TOOL_ROUNDS = 4
CONTENT_CAP = 4000
TOOL_RESULT_CAP = 1500
THREAD_MESSAGE_CAP = 80


def run_turn(user, thread, text):
    """Append a user message and an assistant reply. Returns the assistant Message."""
    text = (text or "").strip()[:CONTENT_CAP]
    if not text:
        raise ValueError("Message is empty.")
    if thread.messages.count() >= THREAD_MESSAGE_CAP:
        raise ValueError("This thread is full. Start a new conversation.")
    Message.objects.create(thread=thread, role=Message.USER, content=text)
    if not thread.title:
        thread.title = text.splitlines()[0][:80]
    thread.updated_at = timezone.now()
    thread.save(update_fields=["title", "updated_at"])

    ctx = SimpleNamespace(user=user, site=thread.site or "", tenant=settings.LINKEDEYE_TENANT)
    if llm.configured():
        try:
            return _model_turn(thread, ctx, text)
        except llm.LLMError as e:
            log.warning("agent model failed: %s", e)
            return _retrieval_turn(thread, ctx, text, note=str(e))
        except Exception:
            log.exception("agent model turn failed")
            return _retrieval_turn(thread, ctx, text, note="The language model failed. Showing retrieved records.")
    return _retrieval_turn(thread, ctx, text)


def _retrieval_turn(thread, ctx, text, note=""):
    hits = knowledge.retrieve(ctx.user, ctx.site, text, limit=8)
    problems = tools.list_problems(ctx)
    summary = tools.site_summary(ctx)
    inventory = tools.search_inventory(ctx, query=text) if text else {"items": []}
    tickets = tools.search_tickets(ctx, query=text)
    lines = []
    if note:
        lines.append(note)
        lines.append("")
    lines.append(_format_summary(summary))
    lines.append("")
    items = problems.get("items") or []
    if items:
        lines.append("Needs attention now:")
        for p in items[:8]:
            lines.append(f"- {p.get('kind')} {p.get('name')} ({p.get('status')})"
                         f"{' · ' + p['site'] if p.get('site') else ''}"
                         f"{' — ' + p['detail'] if p.get('detail') else ''}")
    else:
        lines.append("Nothing is currently down, degraded or firing on your sites.")
    extra = (inventory.get("items") or []) + (tickets.get("items") or [])
    if extra and text:
        lines.append("")
        lines.append("Matching records:")
        for row in extra[:8]:
            label = row.get("number") or row.get("name") or row.get("title")
            kind = row.get("kind") or row.get("status") or ""
            lines.append(f"- {kind} {label}".strip())
    if hits:
        lines.append("")
        lines.append("From the knowledge index:")
        for h in hits[:5]:
            lines.append(f"- {h['title']}: {h['body'][:180]}")
    citations = []
    for row in items + extra + hits:
        href = row.get("href")
        title = row.get("name") or row.get("title") or row.get("number")
        if href and title:
            citations.append({"title": str(title)[:80], "href": href, "site": row.get("site") or ""})
    return Message.objects.create(
        thread=thread, role=Message.ASSISTANT, content="\n".join(lines).strip(),
        citations=_unique_citations(citations),
    )


def _model_turn(thread, ctx, text):
    history = _openai_history(thread)
    # Seed retrieval so the first reply has context even if the model skips tools.
    hits = knowledge.retrieve(ctx.user, ctx.site, text, limit=6)
    seed = ""
    if hits:
        seed = "Retrieved context:\n" + "\n".join(
            f"- [{h['source']}] {h['title']}: {h['body'][:200]}" for h in hits
        )
    messages = [{"role": "system", "content": INSTRUCTIONS}]
    if seed:
        messages.append({"role": "system", "content": seed})
    messages.extend(history)
    citations = [{"title": h["title"], "href": h["href"], "site": h.get("site") or ""} for h in hits if h.get("href")]
    schemas = tools.openai_tools()
    for _round in range(TOOL_ROUNDS):
        reply = llm.complete(messages, tools=schemas)
        tool_calls = reply.get("tool_calls") or []
        if not tool_calls:
            content = (reply.get("content") or "").strip() or "I had no additional detail to add."
            return Message.objects.create(
                thread=thread, role=Message.ASSISTANT, content=content[:CONTENT_CAP],
                citations=_unique_citations(citations),
            )
        messages.append({"role": "assistant", "content": reply.get("content") or "", "tool_calls": tool_calls})
        for call in tool_calls:
            fn = (call.get("function") or {}) if isinstance(call, dict) else {}
            name = fn.get("name") or ""
            raw_args = fn.get("arguments") or "{}"
            try:
                args = json.loads(raw_args) if isinstance(raw_args, str) else (raw_args or {})
            except json.JSONDecodeError:
                args = {}
            result = tools.run_tool(name, args, ctx)
            call_id = call.get("id") or name
            blob = json.dumps(result, default=str)[:TOOL_RESULT_CAP]
            Message.objects.create(
                thread=thread, role=Message.TOOL, content=blob, tool_name=name, tool_call_id=str(call_id)[:80],
            )
            messages.append({"role": "tool", "tool_call_id": call_id, "content": blob})
            citations.extend(_citations_from_result(result))
    return Message.objects.create(
        thread=thread, role=Message.ASSISTANT,
        content="I reached the tool-call limit. Please ask a more specific question.",
        citations=_unique_citations(citations),
    )


def _openai_history(thread):
    rows = list(thread.messages.exclude(role=Message.TOOL).order_by("created_at", "pk")[:HISTORY_LIMIT])
    out = []
    for m in rows:
        if m.role == Message.ASSISTANT:
            out.append({"role": "assistant", "content": m.content or ""})
        else:
            out.append({"role": "user", "content": m.content or ""})
    return out


def _format_summary(summary):
    site = summary.get("site") or "your sites"
    n = summary.get("needs_attention")
    lines = [f"Snapshot for {site}: {n} item(s) need attention."]
    tiles = summary.get("tiles") or []
    if tiles:
        lines.append(" · ".join(f"{t['label']} {t['value']}" for t in tiles))
    return "\n".join(lines)


def _citations_from_result(result):
    out = []
    for row in (result or {}).get("items") or []:
        href = row.get("href")
        title = row.get("name") or row.get("title") or row.get("number")
        if href and title:
            out.append({"title": str(title)[:80], "href": href, "site": row.get("site") or ""})
    return out


def _unique_citations(items):
    seen, out = set(), []
    for c in items:
        href = (c or {}).get("href") or ""
        if not href.startswith("/") or href.startswith("//") or href in seen:
            continue
        seen.add(href)
        out.append({"title": c.get("title") or href, "href": href, "site": c.get("site") or ""})
        if len(out) >= 8:
            break
    return out


def visible_threads(user):
    return Thread.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, owner=user)
