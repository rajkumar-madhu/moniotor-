from django.contrib import admin

from .models import KnowledgeChunk, Message, Thread


class MessageInline(admin.TabularInline):
    model = Message
    extra = 0
    readonly_fields = ("role", "content", "tool_name", "created_at")


@admin.register(Thread)
class ThreadAdmin(admin.ModelAdmin):
    list_display = ("id", "title", "owner", "site", "updated_at")
    list_filter = ("site",)
    search_fields = ("title", "owner__username")
    inlines = [MessageInline]


@admin.register(KnowledgeChunk)
class KnowledgeChunkAdmin(admin.ModelAdmin):
    list_display = ("source", "title", "site", "updated_at")
    list_filter = ("source", "site")
    search_fields = ("title", "body", "source_id")
