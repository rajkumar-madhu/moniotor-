"""Go-live check: example (demo) data still present. Picked up automatically by administration.golive."""
from administration.golive import ok, register, warn


@register("example_data", "Example data")
def example_data(ctx):
    from core import examples
    report = [r for r in examples.summary() if r["total"]]
    if not report:
        return ok("example_data", "Example data", "none: only your own records")
    n = sum(r["total"] for r in report)
    where = ", ".join(f"{r['key']} {r['total']}" for r in report[:6]) + (" ..." if len(report) > 6 else "")
    return warn("example_data", "Example data", f"{n} example records ({where})",
                "python manage.py remove_examples --yes (or Administration > System health > Demo data)")


@register("sample_pages", "Sample pages")
def sample_pages(ctx):
    from django.conf import settings
    if getattr(settings, "SHOW_SAMPLE_PAGES", False) and not settings.DEBUG:
        return warn("sample_pages", "Sample pages", "the /legacy/ design mock-ups are switched on: they show invented numbers to real users",
                    "unset LINKEDEYE_SHOW_SAMPLE_PAGES")
    return ok("sample_pages", "Sample pages", "off: /legacy/ answers 404")
