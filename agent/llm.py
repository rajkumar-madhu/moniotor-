"""OpenAI-compatible chat and embeddings client.

Keys never leave the server. The configured base URL is resolved and pinned through
the same SSRF policy as probes, and redirects are refused so the Authorization header
cannot follow a hop to another host.
"""
import json
import logging
import ssl
from urllib.parse import urlsplit

from django.conf import settings

from probes.checks import ProbeBlocked, _PinnedHTTPConnection, _PinnedHTTPSConnection, resolve_allowed

log = logging.getLogger("linkedeye.agent")

USER_AGENT = "LinkedEye-Agent/1.0"
MAX_BODY = 256 * 1024


class LLMError(Exception):
    """The model HTTP call failed; the message is safe to show an operator."""


def configured():
    return bool((getattr(settings, "AGENT_API_KEY", "") or "").strip())


def _base_url():
    return (getattr(settings, "AGENT_BASE_URL", "") or "https://api.openai.com/v1").rstrip("/")


def _timeout():
    try:
        return max(1.0, min(120.0, float(getattr(settings, "AGENT_TIMEOUT", 30))))
    except (TypeError, ValueError):
        return 30.0


def _post(path, payload, api_key):
    """JSON POST to `{base}{path}` with a pinned connection. Never follows redirects."""
    base = _base_url()
    parsed = urlsplit(base)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise LLMError("LINKEDEYE_AGENT_BASE_URL must be an http(s) URL.")
    if parsed.username or parsed.password:
        raise LLMError("LINKEDEYE_AGENT_BASE_URL must not include credentials; set LINKEDEYE_AGENT_API_KEY.")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        ip = resolve_allowed(parsed.hostname, port)
    except ProbeBlocked as e:
        raise LLMError(f"Agent model URL is blocked by the probe policy: {e}") from e
    except OSError as e:
        raise LLMError(f"Could not resolve the agent model host: {e}") from e

    prefix = parsed.path.rstrip("/")
    url_path = f"{prefix}{path}" if path.startswith("/") else f"{prefix}/{path}"
    body = json.dumps(payload).encode("utf-8")
    headers = {
        "Accept": "application/json",
        "Content-Type": "application/json",
        "Authorization": f"Bearer {api_key}",
        "User-Agent": USER_AGENT,
        "Host": parsed.hostname if not parsed.port else f"{parsed.hostname}:{parsed.port}",
        "Content-Length": str(len(body)),
    }
    timeout = _timeout()
    if parsed.scheme == "https":
        ctx = ssl.create_default_context()
        conn = _PinnedHTTPSConnection(parsed.hostname, ip, port, timeout, ctx)
    else:
        conn = _PinnedHTTPConnection(parsed.hostname, ip, port, timeout)
    try:
        conn.request("POST", url_path or "/", body=body, headers=headers)
        response = conn.getresponse()
        raw = response.read(MAX_BODY + 1)
        status = response.status
    except (TimeoutError, OSError) as e:
        raise LLMError(f"Agent model unreachable: {e}") from e
    finally:
        try:
            conn.close()
        except OSError:
            pass
    if status in (301, 302, 303, 307, 308):
        raise LLMError("Agent model redirected; set LINKEDEYE_AGENT_BASE_URL to the final URL.")
    if status in (401, 403):
        raise LLMError("The agent model rejected the API key (check LINKEDEYE_AGENT_API_KEY).")
    if status >= 400:
        raise LLMError(f"Agent model returned HTTP {status}.")
    if len(raw) > MAX_BODY:
        raise LLMError("Agent model response was too large.")
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise LLMError("Agent model returned invalid JSON.") from e
    if not isinstance(data, dict):
        raise LLMError("Agent model returned an unexpected payload.")
    return data


def complete(messages, tools=None, model=None):
    """Chat completions. `messages` is the OpenAI-style list. Returns the first choice message dict."""
    key = (getattr(settings, "AGENT_API_KEY", "") or "").strip()
    if not key:
        raise LLMError("The language model is not connected (set LINKEDEYE_AGENT_API_KEY).")
    payload = {
        "model": model or getattr(settings, "AGENT_MODEL", "gpt-4o-mini"),
        "messages": messages,
        "temperature": 0.2,
        "max_tokens": 800,
    }
    if tools:
        payload["tools"] = tools
        payload["tool_choice"] = "auto"
    data = _post("/chat/completions", payload, key)
    choices = data.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        raise LLMError("Agent model returned no choices.")
    message = choices[0].get("message")
    if not isinstance(message, dict):
        raise LLMError("Agent model returned no message.")
    return message


def embed(texts):
    """Return a list of embedding vectors (same order as `texts`), or [] when embeddings are not configured."""
    key = (getattr(settings, "AGENT_API_KEY", "") or "").strip()
    if not key or not texts:
        return []
    payload = {
        "model": getattr(settings, "AGENT_EMBEDDING_MODEL", "text-embedding-3-small"),
        "input": [t[:8000] for t in texts],
    }
    data = _post("/embeddings", payload, key)
    rows = data.get("data") or []
    by_index = {int(r.get("index", i)): r.get("embedding") or [] for i, r in enumerate(rows) if isinstance(r, dict)}
    return [by_index.get(i, []) for i in range(len(texts))]


def ping():
    """Tiny chat call used by Administration › Integrations. Returns (ok, message). Never raises."""
    if not configured():
        return False, "The language model is not connected (set LINKEDEYE_AGENT_API_KEY)."
    try:
        message = complete([{"role": "user", "content": "Reply with pong only."}])
    except LLMError as e:
        return False, str(e)
    except Exception:
        log.exception("agent model ping failed")
        return False, "Unexpected error talking to the agent model."
    text = (message.get("content") or "").strip() or "(empty reply)"
    return True, f"Model answered: {text[:80]}"
