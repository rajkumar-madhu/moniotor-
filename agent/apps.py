from django.apps import AppConfig


class AgentConfig(AppConfig):
    name = "agent"
    verbose_name = "Ops Agent"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        from core.jobs import register

        from . import jobs

        register("agent.reindex", 300, jobs.reindex_knowledge)
        register("agent.retention", 3600, jobs.prune_threads)
