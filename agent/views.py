from django.conf import settings
from django.contrib import messages
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.urls import reverse
from django.views.decorators.http import require_POST

from core.access import require, visible_sites
from core.audit import audit
from core.overview import site_names

from . import llm, runner
from .models import Message, Thread

SUGGESTIONS = [
    "What's down right now?",
    "Which alerts are firing?",
    "Open incidents",
    "Find a runbook for this outage",
]


def _wants_json(request):
    return request.headers.get("Accept", "").startswith("application/json") or request.headers.get("X-Requested-With") == "XMLHttpRequest"


def _thread_for(request, pk):
    return get_object_or_404(
        Thread.objects.filter(tenant_key=settings.LINKEDEYE_TENANT, owner=request.user), pk=pk,
    )


def _site_param(request):
    site = (request.POST.get("site") or request.GET.get("site") or "").strip()
    allowed = visible_sites(request.user)
    names = site_names(request.user)
    if site and site not in names:
        return ""
    if allowed is not None and site and site not in allowed:
        return ""
    return site


def _page(request, thread=None, extra=None):
    request.le_site_names = site_names(request.user)
    threads = list(runner.visible_threads(request.user)[:40])
    visible = []
    if thread:
        visible = list(thread.messages.exclude(role=Message.TOOL).order_by("created_at", "pk"))
    ctx = {
        "thread": thread,
        "threads": threads,
        "chat_messages": visible,
        "llm_configured": llm.configured(),
        "suggestions": SUGGESTIONS,
        "agent_model": getattr(settings, "AGENT_MODEL", "gpt-4o-mini"),
    }
    if extra:
        ctx.update(extra)
    return render(request, "agent/chat.html", ctx)


@require("view")
def chat(request):
    thread = runner.visible_threads(request.user).first()
    return _page(request, thread)


@require("view")
def thread(request, pk):
    return _page(request, _thread_for(request, pk))


@require("view")
@require_POST
def new_thread(request):
    site = _site_param(request)
    t = Thread.objects.create(
        tenant_key=settings.LINKEDEYE_TENANT, owner=request.user, site=site, title="",
    )
    audit(request, "create", t, site=site)
    content = (request.POST.get("content") or "").strip()
    reply = None
    if content:
        try:
            reply = runner.run_turn(request.user, t, content)
            audit(request, "run", t, preview=content[:80])
        except ValueError as e:
            limited = isinstance(e, runner.RateLimited)
            if limited:
                t.delete()   # nothing was asked: do not leave an empty conversation behind
            messages.error(request, str(e))
            if _wants_json(request):
                body = {"ok": False, "error": str(e)}
                if not limited:
                    body.update(id=t.pk, url=t.get_absolute_url())
                return JsonResponse(body, status=429 if limited else 400)
            if limited:
                return redirect("agent:chat")
    if _wants_json(request):
        payload = {"ok": True, "id": t.pk, "url": t.get_absolute_url(), "title": t.display_title()}
        if reply:
            payload["user"] = {"role": "user", "content": content}
            payload["assistant"] = {"role": "assistant", "content": reply.content, "citations": reply.citations or []}
        return JsonResponse(payload)
    return redirect(t.get_absolute_url())


@require("view")
@require_POST
def send_message(request, pk):
    t = _thread_for(request, pk)
    content = (request.POST.get("content") or "").strip()
    try:
        reply = runner.run_turn(request.user, t, content)
    except ValueError as e:
        if _wants_json(request):
            return JsonResponse({"ok": False, "error": str(e)}, status=429 if isinstance(e, runner.RateLimited) else 400)
        messages.error(request, str(e))
        return redirect(t.get_absolute_url())
    audit(request, "run", t, preview=content[:80])
    if _wants_json(request):
        return JsonResponse({
            "ok": True,
            "id": t.pk,
            "url": t.get_absolute_url(),
            "title": t.display_title(),
            "user": {"role": "user", "content": content},
            "assistant": {
                "role": "assistant", "content": reply.content, "citations": reply.citations or [],
            },
            "llm_configured": llm.configured(),
        })
    return redirect(t.get_absolute_url())


@require("view")
@require_POST
def delete_thread(request, pk):
    t = _thread_for(request, pk)
    audit(request, "delete", t, title=t.title)
    t.delete()
    messages.success(request, "Conversation deleted.")
    if _wants_json(request):
        return JsonResponse({"ok": True, "url": reverse("agent:chat")})
    return redirect("agent:chat")
