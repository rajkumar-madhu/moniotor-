"""Read-only go-live readiness report: PASS / WARN / FAIL for the running configuration.

    python manage.py go_live_check                      # exit status 1 when anything FAILs
    python manage.py go_live_check --strict             # ... or WARNs
    python manage.py go_live_check --json
    python manage.py go_live_check --url https://linkedeye.example.com      # also probe the running site
    python manage.py go_live_check --wait-worker 90     # wait up to 90 s for the first scheduler heartbeat
    python manage.py go_live_check --send-test-mail you@example.com         # the only action that is not read-only

The checks themselves are in administration/golive.py, where other apps can add their own (module golive_checks.py).
"""
import json
import time

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from administration import golive


class Command(BaseCommand):
    help = "Inspect the running configuration and print PASS/WARN/FAIL for everything a live deployment depends on (read-only)."

    def add_arguments(self, parser):
        parser.add_argument("--strict", action="store_true", help="Exit with status 1 on WARN as well as FAIL.")
        parser.add_argument("--json", action="store_true", help="Print the report as JSON.")
        parser.add_argument("--only", default="", help="Comma-separated check keys to run (see --list).")
        parser.add_argument("--skip", default="", help="Comma-separated check keys to skip.")
        parser.add_argument("--list", action="store_true", help="List the check keys and exit.")
        parser.add_argument("--url", default="", help="Also probe a running console, e.g. https://linkedeye.example.com.")
        parser.add_argument("--insecure", action="store_true", help="With --url: do not verify the TLS certificate (self-signed).")
        parser.add_argument("--wait-worker", type=int, default=0, metavar="SECONDS",
                            help="Wait up to this long for the scheduler's first heartbeat before judging it.")
        parser.add_argument("--send-test-mail", default="", metavar="ADDRESS",
                            help="Send one test message through the configured SMTP server (not read-only).")

    def handle(self, *args, **opts):
        if opts["list"]:
            for key in golive.check_keys():
                self.stdout.write(key)
            return
        only = [k.strip() for k in opts["only"].split(",") if k.strip()]
        skip = [k.strip() for k in opts["skip"].split(",") if k.strip()]
        unknown = sorted(set(only + skip) - set(golive.check_keys()))
        if unknown:
            raise CommandError(f"unknown check(s): {', '.join(unknown)} (see --list)")
        if opts["wait_worker"] > 0:
            self._wait_for_worker(opts["wait_worker"])
        results = golive.run_checks(only=only, skip=skip)
        if opts["url"]:
            results += golive.check_url(opts["url"], verify=not opts["insecure"])
        if opts["send_test_mail"]:
            results.append(self._send_test_mail(opts["send_test_mail"]))
        counts, verdict = golive.counts(results), golive.overall(results)
        if opts["json"]:
            self.stdout.write(json.dumps({"overall": verdict, "counts": counts,
                                          "results": [r.as_dict() for r in results]}, indent=2))
        else:
            self._print(results, counts)
        self.stdout.flush()
        if verdict == golive.FAIL or (opts["strict"] and verdict == golive.WARN):
            raise CommandError(f"go-live check: {counts[golive.FAIL]} FAIL, {counts[golive.WARN]} WARN", returncode=1)

    def _print(self, results, counts):
        paint = {golive.PASS: self.style.SUCCESS, golive.WARN: self.style.WARNING, golive.FAIL: self.style.ERROR}
        width = max((len(r.label) for r in results), default=10)
        self.stdout.write(f"LinkedEye go-live check, {timezone.now():%Y-%m-%d %H:%M} UTC")
        for r in results:
            self.stdout.write(f"  {paint[r.status](r.status)}  {r.label.ljust(width)}  {r.detail}")
            if r.status != golive.PASS and r.fix:
                self.stdout.write(f"        {'':<{width}}  fix: {r.fix}")
        self.stdout.write(f"{counts[golive.PASS]} PASS, {counts[golive.WARN]} WARN, {counts[golive.FAIL]} FAIL")

    def _wait_for_worker(self, seconds):
        from administration.models import Heartbeat
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if Heartbeat.objects.filter(key="scheduler").exists():
                return
            time.sleep(2)

    def _send_test_mail(self, address):
        from django.conf import settings
        from django.core.mail import EmailMultiAlternatives, get_connection

        from administration import integration_config as ic
        try:
            conn = None
            if ic.has_override("email"):
                conn = get_connection(host=ic.email_host() or settings.EMAIL_HOST, port=ic.email_port(),
                                      username=ic.email_host_user() or None, password=ic.email_host_password() or None,
                                      use_tls=ic.email_use_tls())
            sender = ic.default_from_email() or settings.DEFAULT_FROM_EMAIL
            EmailMultiAlternatives("LinkedEye go-live test message", "If you can read this, LinkedEye can send sign-in codes.",
                                   sender, [address], connection=conn).send(fail_silently=False)
        except Exception as exc:    # the message names the class only: SMTP errors can echo the user name
            return golive.fail("smtp_test", "Test message", f"could not send to {address}: {exc.__class__.__name__}",
                               "check LINKEDEYE_EMAIL_HOST / PORT / USER / PASSWORD / USE_TLS")
        return golive.ok("smtp_test", "Test message", f"sent to {address}; check the inbox")
