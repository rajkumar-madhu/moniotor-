"""Error-report filter that keeps working under DEBUG=True.

Django's stock filter switches itself off when DEBUG is on ("your site is not safe anyway"), so the
technical 500 page then prints every POST parameter and every local variable of every frame. LinkedEye
takes secrets in POST bodies (the onboarding credential vault) and holds decrypted values in locals, and
a developer or a mis-set DEBUG must not be a way to read them: views that take secrets are decorated
with ``sensitive_post_parameters`` / ``sensitive_variables``, and this filter honours those decorators
whether or not DEBUG is on. Everything that is not decorated is shown exactly as before.
"""
from django.views.debug import SafeExceptionReporterFilter


class AlwaysCleansingFilter(SafeExceptionReporterFilter):
    def is_active(self, request):
        return True
