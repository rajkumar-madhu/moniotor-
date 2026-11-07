# Notification templates

LinkedEye delivers **critical** and **warning** events to webhooks and e-mail (`core.notify`). Configure channels under **Administration › Integrations** or with `LINKEDEYE_NOTIFY_*` environment variables (see [DEPLOYMENT.md](DEPLOYMENT.md)).

## Webhook JSON (Slack, Teams, Apprise, StackStorm…)

Each event is a single `POST` with `Content-Type: application/json`:

```json
{
  "title": "Portal health check down",
  "body": "HTTP 503 from https://portal.example/health",
  "severity": "critical",
  "source": "urlchecker",
  "site": "Mumbai (NSE)",
  "occurred_at": "2026-11-06T10:15:00+00:00",
  "kind": "state_change",
  "object": "urlchecker.UrlCheck:42",
  "html": "<!doctype html>…"
}
```

Digest summaries (when many events arrive in a short window) use `"kind": "digest"` and include `"count"` and `"titles"`.

**Slack (Incoming Webhook)** — map fields in a workflow or use Apprise with `slack://`. Minimal example using only `text`:

```json
{
  "text": "[CRITICAL] Portal health check down — HTTP 503 (Mumbai (NSE))"
}
```

Your receiver can read `title`, `body`, `severity`, and `site` from the LinkedEye payload and format blocks as needed.

## E-mail templates

HTML and plain-text bodies are rendered from Django templates:

| Template | Used for |
|---|---|
| `core/templates/notify/event_email.html` | Single event (HTML) |
| `core/templates/notify/event_email.txt` | Single event (plain text) |
| `core/templates/notify/digest_email.html` | Collapsed digest (HTML) |
| `core/templates/notify/digest_email.txt` | Collapsed digest (plain text) |

Rendering lives in `core/notify_templates.py`. To customize branding or layout, edit those files in your deployment (or override them in a fork). The subject line remains `[LinkedEye SEVERITY] title` from `core/notify.py`.

## Automation “Notification” templates

Under **Automation › Templates**, a template of kind **Notification** emits a synthetic event into the same stream (title, severity, site, message). When severity is critical or warning and channels are configured, it is delivered like any other event.

Example seed: `manage.py seed_automation` creates “Example – Trading day start check failed”.

## Test delivery

**Administration › Integrations** → **Send test notification** on webhooks or e-mail. That uses a one-off warning event and never writes to the event stream.

## Welcome e-mail (personalized)

When a global admin creates a user in **Administration › Users › New user**, LinkedEye sends a welcome message if the account has an e-mail address and `LINKEDEYE_WELCOME_EMAIL` is enabled (default: on).

| Template | Purpose |
|---|---|
| `core/templates/notify/welcome_email.html` | Branded HTML with greeting, role, site scope, portal button |
| `core/templates/notify/welcome_email.txt` | Plain-text fallback |

If the admin chose **Generate a strong password**, the temporary password is included once in that e-mail (and still shown once in the admin UI). If the admin set the password manually, the mail omits credentials and only links to the portal.

Rendering: `core/welcome_mail.py`. Optional override for the sign-in link: `LINKEDEYE_PORTAL_URL` (otherwise the login URL from the creating request is used).
