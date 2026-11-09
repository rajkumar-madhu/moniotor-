from django.apps import AppConfig


class CoreConfig(AppConfig):
    name = "core"
    verbose_name = "LinkedEye core"

    def ready(self):
        from . import notify  # noqa: F401  (registers the notification listener)
        from django.contrib.auth.signals import user_logged_in

        from . import examples

        # administrators learn at sign-in whether example data exists (one integer in the session, shown as a banner)
        user_logged_in.connect(lambda sender, request=None, user=None, **kw: examples.refresh_session_flag(request, user) if request is not None else None,
                               weak=False, dispatch_uid="core.examples.refresh_session_flag")
