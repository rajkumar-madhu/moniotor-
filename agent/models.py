"""Durable agent threads, messages and retrieved knowledge chunks."""
from django.conf import settings
from django.db import models
from django.urls import reverse
from django.utils import timezone


def _tenant():
    return settings.LINKEDEYE_TENANT


class Thread(models.Model):
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    owner = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE, related_name="agent_threads")
    site = models.CharField(max_length=100, blank=True, db_index=True,
                            help_text="Limit tool results to this site; blank = every site the owner can see.")
    title = models.CharField(max_length=120, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["-updated_at"]
        indexes = [models.Index(fields=["tenant_key", "owner", "-updated_at"])]

    def __str__(self):
        return self.title or f"Thread {self.pk}"

    def get_absolute_url(self):
        return reverse("agent:thread", args=[self.pk])

    def display_title(self):
        return self.title or "New conversation"


class Message(models.Model):
    USER, ASSISTANT, TOOL = "user", "assistant", "tool"
    ROLES = [(USER, "User"), (ASSISTANT, "Assistant"), (TOOL, "Tool")]

    thread = models.ForeignKey(Thread, on_delete=models.CASCADE, related_name="messages")
    role = models.CharField(max_length=12, choices=ROLES)
    content = models.TextField(blank=True)
    tool_name = models.CharField(max_length=60, blank=True)
    tool_call_id = models.CharField(max_length=80, blank=True)
    citations = models.JSONField(default=list, blank=True)
    created_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["created_at", "pk"]
        indexes = [models.Index(fields=["thread", "created_at"])]

    def __str__(self):
        return f"{self.role}: {self.content[:40]}"


class KnowledgeChunk(models.Model):
    """Indexed snapshot of events, problems, tickets and runbooks for retrieval."""
    tenant_key = models.CharField(max_length=100, db_index=True, default=_tenant)
    site = models.CharField(max_length=100, blank=True, db_index=True)
    source = models.CharField(max_length=20, db_index=True)
    source_id = models.CharField(max_length=80)
    title = models.CharField(max_length=200)
    body = models.TextField()
    href = models.CharField(max_length=400, blank=True)
    embedding = models.JSONField(default=list, blank=True)
    updated_at = models.DateTimeField(default=timezone.now, db_index=True)

    class Meta:
        ordering = ["-updated_at"]
        constraints = [
            models.UniqueConstraint(fields=["tenant_key", "source", "source_id"], name="agent_chunk_unique"),
        ]
        indexes = [models.Index(fields=["tenant_key", "site"])]

    def __str__(self):
        return f"{self.source}:{self.title}"
