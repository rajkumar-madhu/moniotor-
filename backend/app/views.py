"""
Definition of views.
"""

from pickle import TRUE
from django.shortcuts import render, redirect
from datetime import datetime, timedelta
from django.http import HttpResponse , HttpRequest , JsonResponse
from django.urls import reverse
from django.contrib import messages
from django.contrib.auth.models import User, auth
from django.db import models
from django.contrib.auth.models import Group
import json, smtplib, ssl
from django.views.decorators.csrf import csrf_exempt
from notification.models import ServiceModel, UserNotificationSetingsModel
from useronboard.models import Usersite
from random import randint
from notification.models import ServiceModel
from lib.LinkedEyeNotification import Notification
from django.template.loader import render_to_string
from useronboard.models import Userotp, UserTOTP
from auditlogs.models import AuditlogsModel
import pyotp
import qrcode
import io
import base64
import hashlib
import hmac
import struct
from django.utils import timezone
from django.contrib.auth.decorators import login_required
from json import dumps as jdumps
import logging

app_logger = logging.getLogger('linkedeye')
from lib.LinkedEyeRedis import Redis
from login.decorators import role_required
from django.conf import settings
from requests.auth import HTTPBasicAuth
import requests
import psycopg2
import os
import re
from urllib.parse import urlparse, unquote

json_path = "iframeGraphs/"
json_paths = "snmp/"
# Was hardcoded to 24h, independent of settings.SESSION_COOKIE_AGE -- every OTP-verified
# login called apply_session_timeout() below, which silently overrode SESSION_COOKIE_AGE
# back to this constant via request.session.set_expiry(). Deriving from the same setting
# keeps them from drifting apart again.
SESSION_TIMEOUT_SECONDS = getattr(settings, 'SESSION_COOKIE_AGE', 8 * 60 * 60)
# FIXED: Use settings instead of hardcoded password
ADMIN_DEFAULT_PASSWORD = getattr(settings, 'ADMIN_DEFAULT_PASSWORD', '') or os.getenv('ADMIN_DEFAULT_PASSWORD', '')
# TOTP master key must be set via Vault or TOTP_MASTER_KEY env var in production.
DEFAULT_MASTER_KEY = ""

PROMETHEUS_USERNAME = getattr(settings, 'PROMETHEUS_USERNAME', 'prometheus')
PROMETHEUS_PASSWORD = getattr(settings, 'PROMETHEUS_PASSWORD', '')
GRAFANA_USERNAME = getattr(settings, 'GRAFANA_USERNAME', 'grafana')
GRAFANA_PASSWORD = getattr(settings, 'GRAFANA_PASSWORD', '')

from lib.LinkedEyeMonitoring.token import (
    monitoring_credentials as _monitoring_credentials,
    service_configured as _monitoring_configured,
    bearer_mode as _bearer_mode,
    verify_ssl as _monitoring_verify,
)


def _configured_monitoring_url(kind, raw_url):
    """Return a configured monitoring URL only when it belongs to a saved site.

    These proxy endpoints make server-side requests with privileged credentials,
    so accepting arbitrary browser-provided URLs would turn them into SSRF
    endpoints. SiteModel is the existing allowlist managed by site admins.
    """
    candidate = (raw_url or '').strip().rstrip('/')
    if not candidate:
        return ''

    from lesites.models import SiteModel

    field_name = {
        'prometheus': 'prometheus_url',
        'grafana': 'analytics_Prefix_URL',
    }.get(kind)
    if not field_name:
        return ''

    # Values are stored as URLs in SiteModel. Exact matching after trimming a
    # trailing slash prevents callers from changing the host, port, or path.
    for configured_url in SiteModel.objects.values_list(field_name, flat=True):
        if (configured_url or '').strip().rstrip('/') == candidate:
            return (configured_url or '').strip()
    return ''


@csrf_exempt
@login_required(login_url='/')
def prometheus_proxy(request):
    """
    Proxy view that forwards Prometheus API requests server-side with Basic Auth.
    Resolves CORS issues when the browser tries to call Prometheus directly.
    """
    prometheus_url = request.GET.get('prometheus_url', '').rstrip('/')
    api_path = request.GET.get('path', '/api/v1/query_range')

    if not prometheus_url:
        return JsonResponse({'status': 'error', 'error': 'prometheus_url is required'}, status=400)

    configured_url = _configured_monitoring_url('prometheus', prometheus_url)
    if not configured_url:
        return JsonResponse({'status': 'error', 'error': 'Unknown Prometheus site URL'}, status=403)
    prometheus_url = configured_url.rstrip('/')

    # The dashboards may pass Prometheus's public URL, which isn't routable from
    # inside the pod. Rewrite it to the in-cluster service. Configurable via
    # PROMETHEUS_EXTERNAL_URL / PROMETHEUS_INTERNAL_URL (defaults suit the mirai
    # deployment; empty external disables the rewrite).
    _prom_external = getattr(settings, 'PROMETHEUS_EXTERNAL_URL', '').rstrip('/')
    _prom_internal = getattr(settings, 'PROMETHEUS_INTERNAL_URL', '').rstrip('/')
    if _prom_external and _prom_internal and prometheus_url == _prom_external:
        prometheus_url = _prom_internal

    if not _monitoring_configured('prometheus'):
        return JsonResponse({
            'status': 'error',
            'error': 'Monitoring auth is not configured on the server '
                     '(set KEYCLOAK_MONITORING_CLIENT_SECRET for bearer mode, '
                     'or PROMETHEUS_PASSWORD for basic mode)',
        }, status=503)

    forward_params = {k: v for k, v in request.GET.items() if k not in ('prometheus_url', 'path')}
    target_url = prometheus_url + api_path
    # Per-site Prometheus host (e.g. Global LE proxying to NextAlpha/Mobikwik's
    # own Prometheus, each behind its own oauth2-proxy/Keycloak client) --
    # resolved from the *pre-rewrite* public URL, since that's the host each
    # site's KEYCLOAK_PROMETHEUS_SITE_MAP entry is keyed on.
    entity_host = urlparse(request.GET.get('prometheus_url', '')).hostname or ''

    def _do_get(force_refresh=False):
        auth, extra_headers = _monitoring_credentials('prometheus', force_refresh=force_refresh, entity_host=entity_host)
        return requests.get(
            target_url,
            params=forward_params,
            auth=auth,
            headers=extra_headers,
            timeout=30,
            verify=_monitoring_verify(),
        )

    try:
        resp = _do_get()
        # In bearer mode a stale/rejected token makes oauth2-proxy answer 401/403
        # (or 302 to the login page). Refresh once and retry before giving up.
        if _bearer_mode() and resp.status_code in (401, 403, 302):
            resp = _do_get(force_refresh=True)
        content_type = resp.headers.get('Content-Type', '')
        if 'application/json' in content_type:
            return JsonResponse(resp.json(), status=resp.status_code, safe=False)

        return JsonResponse({
            'status': 'error',
            'error': f'Prometheus returned HTTP {resp.status_code}',
            'detail': resp.text[:500],
        }, status=resp.status_code if resp.status_code >= 400 else 502)
    except requests.exceptions.RequestException as e:
        app_logger.error('prometheus_proxy failed for %s: %s', target_url, e)
        return JsonResponse({'status': 'error', 'error': str(e)}, status=502)


@csrf_exempt
@login_required(login_url='/')
def grafana_generate_token(request):
    """
    Generates a short-lived Grafana Viewer API key using Basic Auth (server-side).
    """
    grafana_url = request.GET.get('grafana_url', '').rstrip('/')
    if not grafana_url:
        return JsonResponse({'token': '', 'error': 'grafana_url is required'}, status=400)

    configured_url = _configured_monitoring_url('grafana', grafana_url)
    if not configured_url:
        return JsonResponse({'token': '', 'error': 'Unknown Grafana site URL'}, status=403)
    grafana_url = configured_url.rstrip('/')

    if not _monitoring_configured('grafana'):
        return JsonResponse({'token': '', 'error': 'Monitoring auth is not configured on the server '
                             '(set KEYCLOAK_MONITORING_CLIENT_SECRET or GRAFANA_PASSWORD)'}, status=503)

    key_name = f'le_viewer_{int(datetime.now().timestamp())}'
    grafana_auth, grafana_headers = _monitoring_credentials('grafana')

    try:
        resp = requests.post(
            f'{grafana_url}/api/auth/keys',
            json={'name': key_name, 'role': 'Viewer', 'secondsToLive': 3600},
            auth=grafana_auth,
            headers=grafana_headers,
            verify=_monitoring_verify(),
            timeout=10,
        )
        if resp.ok:
            token = resp.json().get('key', '')
            if token:
                return JsonResponse({'token': token})

        sa_resp = requests.post(
            f'{grafana_url}/api/serviceaccounts',
            json={'name': 'le_embed_viewer', 'role': 'Viewer', 'isDisabled': False},
            auth=grafana_auth,
            headers=grafana_headers,
            verify=_monitoring_verify(),
            timeout=10,
        )
        if sa_resp.ok:
            sa_id = sa_resp.json().get('id')
            if sa_id:
                tok_resp = requests.post(
                    f'{grafana_url}/api/serviceaccounts/{sa_id}/tokens',
                    json={'name': key_name, 'secondsToLive': 3600},
                    auth=grafana_auth,
                    headers=grafana_headers,
                    verify=_monitoring_verify(),
                    timeout=10,
                )
                if tok_resp.ok:
                    token = tok_resp.json().get('key', '')
                    if token:
                        return JsonResponse({'token': token})

        return JsonResponse({'token': '', 'error': 'Could not generate token: ' + resp.text})

    except requests.exceptions.RequestException as e:
        return JsonResponse({'token': '', 'error': str(e)}, status=502)


def _split_grafana_site(path):
    """ADP embeds /grafana-proxy/<sitename>/d/<uid>/....

    The sitename is not a Grafana path. When it matches lesite.sitename, return
    the site, the path Grafana should see, and that site's analytics URL.
    """
    clean = (path or '').lstrip('/')
    if not clean:
        return '', path or '', ''
    first, _, rest = clean.partition('/')
    if first in ('d', 'public', 'api', 'apis', 'avatar', 'login', 'dashboards', 'explore'):
        return '', path or '', ''
    from lesites.models import SiteModel
    site_url = SiteModel.objects.filter(sitename=unquote(first)).values_list(
        'analytics_Prefix_URL', flat=True
    ).first()
    if not site_url:
        return '', path or '', ''
    return unquote(first), rest, site_url.rstrip('/')


@csrf_exempt
@login_required(login_url='/')
def grafana_full_proxy(request, path=''):
    """
    Full reverse proxy for Grafana — browser never contacts Grafana directly.
    """
    site_name, path, site_base = _split_grafana_site(path)
    proxy_prefix = '/grafana-proxy/' + site_name if site_name else '/grafana-proxy'
    if site_name and site_base and '_g' not in request.GET:
        request.session['grafana_proxy_url'] = site_base
        request.session['grafana_proxy_site'] = site_name
        request.session.modified = True

    if '_g' in request.GET:
        requested_url = request.GET['_g'].rstrip('/')
        configured_url = _configured_monitoring_url('grafana', requested_url)
        if not configured_url:
            return HttpResponse('Unknown Grafana site URL', status=403)
        request.session['grafana_proxy_url'] = configured_url.rstrip('/')
        request.session.modified = True

    grafana_base = request.session.get('grafana_proxy_url', '').rstrip('/')
    configured_url = _configured_monitoring_url('grafana', grafana_base)
    if not configured_url:
        request.session.pop('grafana_proxy_url', None)
        request.session.pop('grafana_proxy_site', None)
        return HttpResponse('Grafana URL is not configured for a site.', status=403)
    grafana_base = configured_url.rstrip('/')
    if not grafana_base:
        return HttpResponse(
            'Grafana URL not configured. Include ?_g=<grafana_url> in the first request.',
            status=400
        )

    if not _monitoring_configured('grafana'):
        return HttpResponse('Monitoring auth is not configured on the server '
                            '(set KEYCLOAK_MONITORING_CLIENT_SECRET or GRAFANA_PASSWORD)', status=503)

    clean_path = path.lstrip('/')
    target_url = f"{grafana_base}/{clean_path}"
    params = {k: v for k, v in request.GET.items() if k != '_g'}
    # Per-site Grafana host (e.g. Global LE proxying to NextAlpha/Mobikwik's own
    # Grafana, each behind its own oauth2-proxy/Keycloak client) -- same
    # KEYCLOAK_GRAFANA_SITE_MAP mechanism as prometheus_proxy.
    entity_host = urlparse(grafana_base).hostname or ''

    try:
        base_headers = {
            'Accept': request.META.get('HTTP_ACCEPT', '*/*'),
            'Accept-Encoding': 'identity',
            'User-Agent': 'Mozilla/5.0 (LinkedEye Proxy)',
        }
        if request.content_type:
            base_headers['Content-Type'] = request.content_type

        def _do_grafana(force_refresh=False):
            auth, extra_headers = _monitoring_credentials('grafana', force_refresh=force_refresh, entity_host=entity_host)
            headers = dict(base_headers)
            headers.update(extra_headers)
            return requests.request(
                method=request.method,
                url=target_url,
                params=params,
                data=request.body,
                auth=auth,
                headers=headers,
                verify=_monitoring_verify(),
                timeout=30,
                allow_redirects=True,
            )

        resp = _do_grafana()
        # oauth2-proxy answers 401/403 (or 302 to Keycloak) on a stale token —
        # refresh once and retry in bearer mode.
        if _bearer_mode() and resp.status_code in (401, 403, 302):
            resp = _do_grafana(force_refresh=True)

        content_type = resp.headers.get('Content-Type', 'application/octet-stream')
        status_code = resp.status_code

        if 'text/html' in content_type:
            content = resp.text

            content = re.sub(
                r'((?:src|href|action)=")(/(?!grafana-proxy/))',
                r'\1' + proxy_prefix + '/',
                content
            )
            content = re.sub(
                r"((?:src|href|action)=')(/(?!grafana-proxy/))",
                r"\1" + proxy_prefix + "/",
                content
            )

            content = content.replace(grafana_base + '/', proxy_prefix + '/')
            content = content.replace(grafana_base, proxy_prefix)

            fetch_patch = r"""<script>
(function(){
    var PROXY='__PROXY_PREFIX__';
    function patchUrl(u){
        if(typeof u==='string'){
            if(u.charAt(0)==='/'&&u.indexOf(PROXY)!==0) return PROXY+u;
            try {
                var parsed=new URL(u, window.location.origin);
                if(parsed.origin===window.location.origin && parsed.pathname.charAt(0)==='/' && parsed.pathname.indexOf(PROXY)!==0){
                    parsed.pathname=PROXY+parsed.pathname;
                    return parsed.toString();
                }
            } catch(e) {}
            return u;
        }
        if(u && typeof u.url==='string') return new Request(patchUrl(u.url), u);
        return u;
    }
    var _f=window.fetch;
    window.fetch=function(u,o){return _f.call(window,patchUrl(u),o);};
    var _X=window.XMLHttpRequest;
    window.XMLHttpRequest=function(){
        var x=new _X();
        var _o=x.open.bind(x);
        x.open=function(m,u){
            return _o.apply(x,[m,patchUrl(u)].concat(Array.prototype.slice.call(arguments,2)));
        };
        return x;
    };
    window.XMLHttpRequest.prototype=_X.prototype;
    if (window.location.pathname.indexOf(PROXY) === 0) {
        var realPath = window.location.pathname.substring(PROXY.length);
        if (!realPath.startsWith('/')) realPath = '/' + realPath;
        window.history.replaceState(null, '', realPath + window.location.search + window.location.hash);
    }
    var _ce = document.createElement;
    document.createElement = function(tagName) {
        var el = _ce.call(document, tagName);
        if (tagName.toLowerCase() === 'script') {
            Object.defineProperty(el, 'src', {
                set: function(val) { el.setAttribute('src', patchUrl(val)); },
                get: function() { return el.getAttribute('src'); }
            });
        }
        if (tagName.toLowerCase() === 'link') {
            Object.defineProperty(el, 'href', {
                set: function(val) { el.setAttribute('href', patchUrl(val)); },
                get: function() { return el.getAttribute('href'); }
            });
        }
        return el;
    };
})();
</script>"""

            fetch_patch = fetch_patch.replace('__PROXY_PREFIX__', proxy_prefix)
            if '<head>' in content:
                content = content.replace('<head>', '<head>' + fetch_patch, 1)
            else:
                content = fetch_patch + content

            return HttpResponse(content, content_type=content_type, status=status_code)

        django_resp = HttpResponse(resp.content, content_type=content_type, status=status_code)
        for header in ('Cache-Control', 'ETag', 'Last-Modified', 'Expires'):
            if header in resp.headers:
                django_resp[header] = resp.headers[header]
        return django_resp

    except requests.exceptions.ConnectionError as e:
        return HttpResponse(f'Cannot connect to Grafana: {e}', status=502)
    except requests.exceptions.Timeout:
        return HttpResponse('Grafana request timed out', status=504)
    except Exception as e:
        return HttpResponse(f'Grafana proxy error: {e}', status=500)


def generate_deterministic_secret(username):
    """
    Generate a deterministic Base32 TOTP secret based on the username and a Master Key.
    This ensures the same user gets the same secret across different environments 
    (DEV, UAT, PROD) without needing database synchronization.
    """
    master_key = getattr(settings, 'TOTP_MASTER_KEY', '') or os.getenv('TOTP_MASTER_KEY', DEFAULT_MASTER_KEY)
    if not master_key:
        raise ValueError("TOTP_MASTER_KEY is not configured")
    
    # Create HMAC-SHA1 hash of the username using the master key
    key = master_key.encode('utf-8')
    msg = username.encode('utf-8')
    digest = hmac.new(key, msg, hashlib.sha1).digest()
    
    # Take the first 10 bytes (80 bits) to generate a 16-character Base32 secret
    # Google Authenticator expects 16 chars (80 bits) minimum, 32 chars recommended.
    # We'll use the full 20 byte digest (160 bits) -> 32 char Base32
    # But standard Base32 encoding of 20 bytes is 32 chars.
    
    return base64.b32encode(digest).decode('utf-8').rstrip('=')

def send_otp_email(recipient_email, display_name, otp):
    """Helper function to send OTP email via Office365 SMTP"""
    from email.message import EmailMessage
    try:
        # FIXED: Use settings instead of hardcoded SMTP credentials
        smtp_server = getattr(settings, 'SMTP_HOST', 'smtp.office365.com')
        smtp_port = int(getattr(settings, 'SMTP_PORT', 587))
        # Use LINKEDEYE_EMAIL and APPKEY as defined in settings.py
        sender_email = getattr(settings, 'LINKEDEYE_EMAIL', '') or ''
        sender_password = getattr(settings, 'LINKEDEYE_EMAIL_APPKEY', '') or ''
        
        if not sender_email or not sender_password:
            return False, "Failed to send OTP: Email credentials (LINKEDEYE_EMAIL or LINKEDEYE_EMAIL_APPKEY) are missing in .env configuration."
        
        msg = EmailMessage()
        msg.set_content(f"Dear {display_name},\n\nYour One-Time Password (OTP) for logging into LinkedEye is {otp}.\nThis OTP is valid for the next **5 minutes**. Please do not share it with anyone.\n\nThank you,\nLinkedeye Teams")
        msg['Subject'] = 'LinkedEye Login OTP'
        msg['From'] = f"Eva <{sender_email}>"
        msg['To'] = recipient_email

        context = ssl.create_default_context()
        with smtplib.SMTP(smtp_server, smtp_port) as server:
            server.starttls(context=context)
            server.login(sender_email, sender_password)
            server.send_message(msg)
            
        return True, "OTP sent successfully"
    except Exception as e:
        return False, f"Failed to send OTP: {str(e)}"

def apply_session_timeout(request):
    """Enforce 24-hour idle timeout for authenticated sessions."""
    request.session.set_expiry(SESSION_TIMEOUT_SECONDS)

def home(request):
    """Renders the home page."""
    assert isinstance(request, HttpRequest)
    is_google=os.getenv('GOOGLE_ALLOW_DOMAINS')
    from django.conf import settings as django_settings
    keycloak_enabled = getattr(django_settings, 'KEYCLOAK_ENABLED', False)
    # Only show the SSO button when Keycloak is enabled AND currently reachable —
    # if it's down, the login page falls back to the normal username/password form.
    keycloak_available = False
    if keycloak_enabled:
        try:
            from lib.LinkedEyeKeycloakAdmin.sync import keycloak_reachable
            keycloak_available = keycloak_reachable()
        except Exception:
            keycloak_available = False

    # A Finspot SSO login already authenticated the Django session (see
    # keycloak_verify), but still owes an email OTP. Re-render the same modal
    # the normal-login flow uses instead of a fresh page, so the user finishes
    # the OTP step here without a separate template/endpoint.
    keycloak_otp_payload = None
    if request.session.get('otp_pending') and request.user.is_authenticated:
        keycloak_otp_payload = json.dumps({
            'status': 201,
            'msg': 'Please enter the OTP sent to your registered email id',
            'redirectUrl': request.session.get('otp_pending_next') or '/dashboard',
            'email': request.session.get('otp_pending_email') or getattr(request.user, 'email', ''),
        })

    # One-shot: set by keycloak_verify when the OTP email itself failed to
    # send, so pop it rather than leave it to reappear on the next page load.
    keycloak_otp_error = request.session.pop('keycloak_otp_error', None)

    # One-shot: set by LinkedEyeKeycloakBackend.authenticate() when the OIDC
    # token exchange/verification itself failed (see login/keycloak_backend.py).
    keycloak_login_error = request.session.pop('keycloak_login_error', None)

    return render(
        request,
        'app/login.html',
        {
            'title':'Home Page',
            'year':datetime.now().year,
            'is_google' : is_google,
            'keycloak_enabled': keycloak_enabled and keycloak_available,
            'keycloak_configured': keycloak_enabled,
            'keycloak_available': keycloak_available,
            'keycloak_otp_payload': keycloak_otp_payload,
            'keycloak_otp_error': keycloak_otp_error,
            'keycloak_login_error': keycloak_login_error,
        }
    )

@login_required(login_url='/') #this makes the user to login first to enter other pages 

def getfilecontent(request):
    filename = request.GET["filename"]
    filedata = open(json_path+filename)
    filecontent = filedata.read()
    filedata.close()
    return HttpResponse(filecontent, content_type="string")


def getfilecontents(request):
    filenames = json.loads(request.GET.get("filenames", "{}")) 

    file_contents = []

    for filename in filenames.keys():
        try:
            with open(json_path + filename, 'r') as file:
                file_content = file.read()
                file_contents.append({filename: file_content})
        except Exception as e:
            file_contents.append({filename: str(e)})

    return JsonResponse(file_contents, safe=False)

def getsnmpfilecontent(request):
    filename = request.GET["filename"]
    filedata = open(json_paths+filename)
    filecontent = filedata.read()
    filedata.close()
    return HttpResponse(filecontent, content_type="string")

def siteErr(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/login-error.html',
        {       }
    )

def invalidDomain(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/invalid-domain.html',
        {       }
    )

@login_required(login_url='/')
def dashboard(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/dashboard.html',
        {       }
    )
@login_required(login_url='/')
def analytics(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/analytics.html',
        {       }
    )

"""def getprefixurlData(request):
    userData = {
        'user': settings.ANALYTICS_DASHBOARD_USER,
        "analysticsDashboardurl": settings.ANALYTICS_DASHBOARD_PREFIXURL
        }
    json_data = json.dumps(userData)
    return HttpResponse(json_data)"""
	
@login_required(login_url='/')    
def bodeod(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/bodeod.html',
        {       }
    )

@login_required(login_url='/')
def calendar(request):
     """Renders the about page."""
     assert isinstance(request, HttpRequest)
     return render(
        request,
        'app/calendar.html',
        {       }
     )

@login_required(login_url='/')
def userprofile(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/profile.html',
        {       }
    )

@login_required(login_url='/')
def sites(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/sites.html',
        {       }
    )

@login_required(login_url="/")
@role_required(allowed_roles = ["Admin"])
def leadmin(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    context={'domain_name':os.getenv('DOMAIN_NAME')}
    return render(
        request,
        'app/admin.html',
        context
    )
def india(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/india.html',
        {       }
    )

@role_required(allowed_roles = ["Admin", "ViewOnly", "Management", "Onboard", "UserView", "TechInfra"])
def switch(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/24-port-switch.html',
        {       }
    )

@login_required(login_url='/')
def comparision(request):
    """Renders the about page."""
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/comparision.html',
        {       }
    )

#===================
"""ms_identity_web = settings.MS_IDENTITY_WEB

@ms_identity_web.login_required"""

def ms_verify(request):    
    isauthenticated='Authentication status is - ' + str(request.identity_context_data.authenticated)
    print('Redirected to Ms verify function',request.identity_context_data)
    return HttpResponse(isauthenticated)

"""def ms_verify(request):
    assert isinstance(request, HttpRequest)
    return render(
        request,
        'app/mstest.html',
        {       }
    )"""

#===================
def google_verify(request):
    email=request.user.email
    nextUrl = request.GET.get('next')
    if request.method == 'GET' and  request.user.is_authenticated:
        response = { }

        obj = User.objects.get(email=email)
        #sites = SiteModel.objects.filter(id = obj.id)
        sites = Usersite.objects.filter(user_id = obj.id)
        if len(sites)>0:
            if nextUrl is None:
                response["status"] = 200
                services = ServiceModel.objects.filter()
                if services:
                    service_ids = []
                    for service in services:
                        service_ids.append(service.id)
                        if UserNotificationSetingsModel.objects.filter(service_id = service.id,user_id=obj.id, is_saved = True).exists():
                            response["redirectUrl"] = '/dashboard'
                        else:
                            response["redirectUrl"] = '/profile?next=/dashboard' 
                else:
                    response["redirectUrl"] = '/dashboard'
                    
            else:
                response["status"] = 200
                response["redirectUrl"] = nextUrl          
        else:
            response["status"] = 200
            response["redirectUrl"] = '/siteError'
        if response["status"] == 200 or response["status"] == 201:
            request.session['user_permissions'] = get_user_permissions("Google")
            log = AuditlogsModel(username = request.user,  action = 'Google User login', status = 'Success', message='User '+email+' login  successfully.')
        else:
            log = AuditlogsModel(username = request.user,  action = 'Google User login', status = 'Failure', message='User '+email+' not able to login')
        log.save()

    return redirect(response["redirectUrl"])

def keycloak_verify(request):
    """Post-Keycloak SSO handler — mirrors google_verify for OIDC users."""
    if request.method != 'GET' or not request.user.is_authenticated:
        return redirect('/')

    # Drop permissions from any prior SSO/password session so a downgraded
    # Keycloak role (e.g. ViewOnly) cannot keep Admin access or skip OTP.
    for stale_key in ('user_permissions', 'otp_pending', 'otp_pending_email', 'otp_pending_next'):
        request.session.pop(stale_key, None)

    obj = request.user
    email = getattr(obj, 'email', None) or getattr(obj, 'username', '') or ''
    nextUrl = request.GET.get('next')
    response = {}

    if not getattr(obj, 'id', None):
        return redirect('/')
    sites = Usersite.objects.filter(user_id=obj.id)
    if len(sites) > 0:
        if nextUrl is None:
            response["status"] = 200
            services = ServiceModel.objects.filter()
            if services:
                for service in services:
                    if UserNotificationSetingsModel.objects.filter(
                        service_id=service.id, user_id=obj.id, is_saved=True
                    ).exists():
                        response["redirectUrl"] = '/dashboard'
                        break
                else:
                    response["redirectUrl"] = '/profile?next=/dashboard'
            else:
                response["redirectUrl"] = '/dashboard'
        else:
            response["status"] = 200
            response["redirectUrl"] = nextUrl
    else:
        response["status"] = 200
        response["redirectUrl"] = '/siteError'

    if response["status"] == 200 or response["status"] == 201:
        log = AuditlogsModel(
            username=request.user,
            action='Finspot SSO login',
            status='Success',
            message='User ' + email + ' login successfully.',
        )
    else:
        log = AuditlogsModel(
            username=request.user,
            action='Finspot SSO login',
            status='Failure',
            message='User ' + email + ' not able to login',
        )
    log.save()

    # DjangoAdmin is a Keycloak realm role synced onto the Django user's group
    # by sync_keycloak_user (see login/keycloak_utils.py) -- not tied to any
    # specific username. A user whose Keycloak role maps to it skips the
    # email-OTP step on Finspot SSO login. verify_otps() normally sets these
    # two things after OTP succeeds; set them here too since that path is
    # never reached for this bypass.
    if obj.groups.filter(name='DjangoAdmin').exists():
        apply_session_timeout(request)
        # Was hardcoded to get_user_permissions('DjangoAdmin'), which ignored
        # every other group the user has (e.g. Admin) and silently capped
        # their permissions at DjangoAdmin's weightage no matter what else
        # was assigned in Keycloak. Match the pattern used everywhere else in
        # this file (e.g. the OTP-verified login path) instead.
        request.session['user_permissions'] = get_user_permissions(highest_weightage_group_name(obj))
        return redirect(response["redirectUrl"])

    # mozilla-django-oidc has already called auth.login() by this point, so the
    # Django session is authenticated — but Finspot SSO still owes the same
    # email-OTP bar as normal username/password login. Mark the session
    # pending and hand off to /verify-otps/ (existing endpoint) via the login
    # page's OTP modal; KeycloakOTPGateMiddleware blocks every other page
    # until it's cleared.
    otp = randint(100000, 999999)
    Userotp.objects.update_or_create(user=obj, defaults={'otp': otp, 'created_at': datetime.now()})
    display_name = obj.first_name or obj.username
    otp_sent, otp_send_message = send_otp_email(email, display_name, otp)

    if not otp_sent:
        # The Django session is already authenticated (see comment above), so
        # leaving it as-is here would let the user in with no OTP ever
        # delivered — silently skipping the 2FA gate. Undo the SSO login
        # entirely instead of stranding them on an OTP screen that can never
        # receive a valid code; they can retry Finspot SSO from scratch.
        app_logger.warning('Finspot SSO OTP email failed for %s: %s', email, otp_send_message)
        AuditlogsModel(
            username=request.user,
            action='Finspot SSO login',
            status='Failure',
            message='OTP email failed for ' + email + ': ' + otp_send_message,
        ).save()
        auth.logout(request)
        request.session['keycloak_otp_error'] = (
            "We couldn't send your login code. Please try Finspot SSO again in a moment."
        )
        return redirect('/')

    request.session['otp_pending'] = True
    request.session['otp_pending_email'] = email
    request.session['otp_pending_next'] = response["redirectUrl"]

    return redirect('/')

def verify(request):
    nextUrl = request.GET.get('next')
    if request.method == 'POST':
        response = { }
        parsed_json = json.loads(request.POST['alldata'])
        email=parsed_json['username']
        password=parsed_json['password']
        if not User.objects.filter(username=email).exists():
            if email != "admin" and email != "djangoadmin" :
                response['msg'] = "The username does not exist. Try again"
                response["status"] = 500
            else:
                if Group.objects.filter(name='Admin').exists():
                    pass
                else:
                    ob_role = Group(name='Admin', weightage = 21) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                    ob_role = Group(name='ViewOnly', weightage = 20) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                    ob_role = Group(name='Google', weightage = 20) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                    ob_role = Group(name='O365', weightage = 20) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                if Group.objects.filter(name='DjangoAdmin').exists():
                    pass
                else:
                    ob_role = Group(name='DjangoAdmin', weightage = 20) # 21=010101 ['VSA','VA','ESA','EA','DSA','DA']
                    ob_role.save()
                if email == "admin":
                    group = Group.objects.get(name = 'Admin').id
                    user = User.objects.create_user(username=email,password=ADMIN_DEFAULT_PASSWORD,email=email,first_name=email,last_name=email,is_active=True)
                    user.save()
                    user.groups.add(group)
                    auth.login(request, user, backend='django.contrib.auth.backends.ModelBackend')
                    apply_session_timeout(request)
                    response["redirectUrl"] = '/dashboard'
                if email == "djangoadmin":
                    DjangoAdmingroup = Group.objects.get(name = 'DjangoAdmin').id
                    if not ADMIN_DEFAULT_PASSWORD:
                        raise RuntimeError("ADMIN_DEFAULT_PASSWORD is not configured")
                    superuser = User.objects.create_superuser(
                        username='djangoadmin',
                        password=ADMIN_DEFAULT_PASSWORD,
                        email=email,
                        first_name='django',
                        last_name='admin',
                        is_active=True
                    )
                    superuser.save()
                    superuser.groups.add(DjangoAdmingroup)
                    auth.login(request, superuser, backend='django.contrib.auth.backends.ModelBackend')
                    apply_session_timeout(request)
                    response["redirectUrl"] = '/admin'
                response["status"] = 200     
        else:
            obj = User.objects.get(username=email)
            if obj.is_active == True:
                user = auth.authenticate(request, username=email, password=password)
                if user is None and getattr(settings, 'KEYCLOAK_ENABLED', False):
                    # Local password check failed (wrong password, or the account
                    # has no usable password — e.g. it was auto-created by an SSO
                    # login and never onboarded through the app). Fall back to
                    # Keycloak; on success, cache the password locally so future
                    # logins work from MySQL too.
                    from login.keycloak_utils import verify_password_via_keycloak
                    if verify_password_via_keycloak(obj.email or email, password):
                        obj.set_password(password)
                        obj.save(update_fields=['password'])
                        user = auth.authenticate(request, username=email, password=password)
                if user is not None:
                    # admin/djangoadmin must complete the same email-OTP / Google
                    # Authenticator step as everyone else -- no bypass -- but they
                    # still get their own fixed destination instead of running
                    # through the ServiceModel-based redirect logic below, which
                    # doesn't apply to them.
                    if email == 'djangoadmin':
                        redirect_url = '/admin'
                    elif email == 'admin':
                        redirect_url = '/dashboard'
                    elif nextUrl is None:
                        redirect_url = None
                        services = ServiceModel.objects.filter()
                        if services:
                            service_ids = []
                            for service in services:
                                service_ids.append(service.id)
                                if UserNotificationSetingsModel.objects.filter(service_id=service.id, user_id=obj.id, is_saved=True).exists():
                                    redirect_url = '/dashboard'
                                else:
                                    redirect_url = '/profile?next=/dashboard'
                        else:
                            redirect_url = '/dashboard'
                    else:
                        redirect_url = nextUrl

                    # Check if user has Google Authenticator set up and enabled
                    has_google_auth = False
                    try:
                        totp_obj = UserTOTP.objects.get(user=obj)
                        has_google_auth = totp_obj.is_enabled
                    except UserTOTP.DoesNotExist:
                        pass
                    except Exception:
                        pass

                    # Always show the 2FA choice modal -- admin/djangoadmin
                    # included. If Google Authenticator is enabled, show both
                    # options; otherwise only Email OTP is available.
                    response["status"] = 202
                    response["msg"] = "Choose your verification method"
                    response["redirectUrl"] = redirect_url
                    response["has_google_auth"] = has_google_auth
                    return HttpResponse(json.dumps(response), content_type="json")
                else:
                    response["status"] = 500
                    response["msg"] = "The username or password you entered is incorrect. Try again"
            else:
                response["status"] = 500
                response["msg"] = "The account has been disabled. Contact the Admin!"

        if request.user.is_authenticated:
            request.session['user_permissions'] = get_user_permissions(highest_weightage_group_name(request.user))

        if response.get("status") == 200:
            log = AuditlogsModel(username=request.user, action='User login', status='Success', message='User '+email+' login successfully.')
            log.save()
        elif response.get("status") == 500:
            log = AuditlogsModel(username=request.user, action='User login', status='Failure', message='User '+email+' not able to login - '+response["msg"])
            log.save()
        print(response)
    return HttpResponse(json.dumps(response), content_type="json")

def logout(request):
    response = { }
    # Capture before auth.logout()/session.flush() wipe the session - this is
    # only present for users who came in via Finspot SSO (mozilla-django-oidc
    # stores it when OIDC_STORE_ID_TOKEN=True).
    oidc_id_token = request.session.get('oidc_id_token')
    if request.session.has_key('user_permissions'):
        request.session.flush()
    try:
        auth.logout(request)
        response["status"] = 200
        # Logging out locally doesn't end the Keycloak SSO session, so
        # revisiting the app silently re-authenticates via Keycloak (no
        # login prompt) and lands straight back on the OTP gate. Redirect
        # through Keycloak's own end-session endpoint too when this was an
        # SSO session, so the next visit needs a real Keycloak login again.
        logout_endpoint = getattr(settings, 'OIDC_OP_LOGOUT_ENDPOINT', '')
        if oidc_id_token and logout_endpoint:
            post_logout_uri = getattr(settings, 'OIDC_RP_POST_LOGOUT_REDIRECT_URI', '/')
            from urllib.parse import urlencode
            response["redirectUrl"] = logout_endpoint + '?' + urlencode({
                'id_token_hint': oidc_id_token,
                'post_logout_redirect_uri': post_logout_uri,
            })
        else:
            response["redirectUrl"] = '/'
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Not able to logout'
    return HttpResponse(json.dumps(response), content_type="json")


def keycloak_aware_logout(request):
    """
    The 'logout' URL (linked as a plain <a href> from commonlayout.html) needs
    an HTTP redirect, not the JSON {status, redirectUrl} shape logout() above
    returns for AJAX callers -- so this duplicates its Keycloak end-session
    logic as a redirect instead of reusing it directly.
    """
    oidc_id_token = request.session.get('oidc_id_token')
    if request.session.has_key('user_permissions'):
        request.session.flush()
    auth.logout(request)
    logout_endpoint = getattr(settings, 'OIDC_OP_LOGOUT_ENDPOINT', '')
    if oidc_id_token and logout_endpoint:
        post_logout_uri = getattr(settings, 'OIDC_RP_POST_LOGOUT_REDIRECT_URI', '/')
        from urllib.parse import urlencode
        return redirect(logout_endpoint + '?' + urlencode({
            'id_token_hint': oidc_id_token,
            'post_logout_redirect_uri': post_logout_uri,
        }))
    return redirect('/')


def get_user_permissions(userGroup):
    userPermissions = []
    weightage = Group.objects.get(name = userGroup).weightage
    binaryValue = decimalToBinary(weightage)
    permisionOrder = ['VSA','VA','ESA','EA','DSA','DA']
    for bValue, permision in zip(reversed(binaryValue), reversed(permisionOrder)):
        if bValue == '1':
            userPermissions.append(permision)

    return userPermissions

def highest_weightage_group_name(user):
    """Return the name of the user's highest-weightage group.

    `user.groups.all()[0]` (used at every call site below before this) picks
    whichever group happens to be first with no ordering guarantee -- in
    practice, MySQL row-insertion order. A user who got DjangoAdmin assigned
    before Admin (a real, observed case) would have their permissions capped
    at DjangoAdmin's lower weightage even though Admin grants more. Ordering
    by weightage descending picks the actual highest-privilege group instead.
    """
    group = user.groups.order_by('-weightage').first()
    return group.name if group else None
def decimalToBinary(number):
    result = ''
    while number != 0:
        remainder = number % 2  # gives the exact remainder
        number = number // 2
        result = str(remainder) + result
    return result
def random_with_N_digits(n):
    range_start = 10**(n-1)
    range_end = (10**n)-1
    return randint(range_start, range_end)

def generate_otp(request):
    response = { }
    try:
        parsed_json = json.loads(request.POST['allData'])
        if User.objects.filter(username = parsed_json['username']).exists():
            otp = random_with_N_digits(6)
            service = ServiceModel.objects.get(name = 'mail')
            if service:
                url = service.syntax
                firstname = User.objects.get(username = parsed_json['username']).first_name
                url = url.replace('{email}', parsed_json['username'])
                content = {'name': firstname,
                            'otp':otp}
                html_content = render_to_string('app/forgotpassword-otp.html', content)
                notification_obj = Notification()
                #apobj = notification_obj.instantiate()  #Rajkumar command for forget password notification
                #url = url  #Rajkumar command for forget password notification
               # apobj.add_url(url)  #Rajkumar command for forget password notification
               # result = apobj.sendnotifications(title='Linkedeye Forgot Password request OTP', message_body=html_content)  #Rajkumar command for forget password notification
                notification_obj.add_url(url)  #Rajkumar added for forget password notification
                result = notification_obj.sendnotifications(title='Linkedeye Forgot Password request OTP', message_body=html_content)   #Rajkumar added for forget password notification
                if result['data']:
                    user_id = User.objects.get(username = parsed_json['username']).id
                    if not Userotp.objects.filter(user_id = user_id).exists():
                        obj = Userotp(user_id = user_id, otp = otp)
                        obj.save()
                    else:
                        obj = Userotp.objects.get(user_id = user_id)
                        obj.otp = otp
                        obj.save()
                    response['status'] = 200
                    response['msg'] = 'Please enter OTP sent on you registered email id'
                else:
                    response['status'] = 500
                    response['msg'] = 'Not able to sent OTP to your registered email id'
        else:
            response['status'] = 500
            response['msg'] = 'User Name not found' 
    except Exception as e:
        print('========Exception===generate_otp===')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
    return HttpResponse(json.dumps(response), content_type="json" )

def verify_OTP(request):
    response = { }
    try:
        parsed_json = json.loads(request.POST['allData'])
        username = parsed_json["username"]
        otp = int(parsed_json["otp"])
        user_id = User.objects.get(username=username).id
        user_obj = Userotp.objects.get(user_id=user_id)
        if int(user_obj.otp) == otp:
            response['status'] = 200
        else:
            response['status'] = 500
            response['msg'] = 'Please enter a valid OTP'
    except Exception as e:
        print('========Exception===verify_OTP===')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
    return HttpResponse(json.dumps(response), content_type="json")

def forgot_password(request):
    response = {}
    try:
        parsed_json = json.loads(request.POST['clientData'])
        username = parsed_json["username"]
        userobj =  User.objects.get(username=parsed_json["username"])
        userobj.set_password(parsed_json['newpsw'])
        userobj.save()
        response['status'] = 200
        response['msg'] = 'Password changed successfully. Now login with new password'
        log = AuditlogsModel(username=userobj, action='Password Reset', status='Success', message=f'User {username} changed password successfully.')
        log.save()
    except Exception as e:
        print('========Exception===change_password===')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        try:
            username = parsed_json.get("username", "Unknown")
        except:
            username = "Unknown"
        log = AuditlogsModel(username=None, action='Password Reset', status='Failure', message=f'User {username} failed to change password. Error: {str(e)}')
        log.save()
    return HttpResponse(json.dumps(response), content_type="json")

#======== calendar =========#
def get_calendar_data(request):
    response = {}
    responseObj = []
    try:
        redisObj = Redis()
        keys = ['Holiday','Mock']
        for key in keys:
            tempObj = {}
            tempObj["key"] = key
            tempObj["key_data"] = json.loads(redisObj.get(key))
            responseObj.append(tempObj)
            print(responseObj)
        response["status"] = 200
        response["responseData"] = responseObj
    except Exception as e:
        print('===Exception===get_calendar_data==')
        print(str(e)) 
        response["status"] = 400
        response["msg"] = 'Something went wrong'
    return HttpResponse(jdumps(response), content_type="json")

def getuserinfo(request):
 if request.method == 'POST':
        response = { }
        parsed_json = json.loads(request.POST['alldata'])
        email=parsed_json['username']
        if email == "admin":
                response['msg'] = "Admin menu is not added"
                response["status"] = 204
                return HttpResponse(json.dumps(response), content_type="json")

@csrf_exempt
def verify_otps(request):
    """Verify OTP entered by user"""
    if request.method == 'POST':
        response = {}
        try:
            parsed_json = json.loads(request.POST.get('alldata', '{}'))
            email = parsed_json.get('username')
            otp_entered = parsed_json.get('otp')
            
            if not email or not otp_entered:
                response['status'] = 400
                response['msg'] = "Email and OTP are required"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Get user
            if not User.objects.filter(username=email).exists():
                response['status'] = 404
                response['msg'] = "User not found"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            user_obj = User.objects.get(username=email)
            
            # Get OTP record
            if not Userotp.objects.filter(user=user_obj).exists():
                response['status'] = 404
                response['msg'] = "OTP not found. Please request a new OTP"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            otp_record = Userotp.objects.get(user=user_obj)
            
            # Check OTP expiry (5 minutes)
            otp_created_time = otp_record.created_at
            current_time = datetime.now()
            
            # Handle timezone-aware datetime
            if otp_created_time.tzinfo is not None:
                from django.utils import timezone
                current_time = timezone.now()
            
            time_diff = current_time - otp_created_time
            
            if time_diff > timedelta(minutes=5):
                response['status'] = 400
                response['msg'] = "OTP has expired. Please request a new OTP"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Verify OTP
            if str(otp_record.otp) == str(otp_entered):
                # OTP is correct - log user in
                auth.login(request, user_obj, backend='django.contrib.auth.backends.ModelBackend')
                apply_session_timeout(request)

                # Set user permissions
                if user_obj.groups.exists():
                    request.session['user_permissions'] = get_user_permissions(highest_weightage_group_name(user_obj))

                # Clear the Finspot SSO OTP-pending gate, if this verification
                # came from that flow (no-op for normal-login OTP, which never
                # sets these keys).
                request.session.pop('otp_pending', None)
                request.session.pop('otp_pending_email', None)
                request.session.pop('otp_pending_next', None)

                # Delete OTP after successful verification
                otp_record.delete()
                
                # Log success
                log = AuditlogsModel(username=user_obj, action='User login', status='Success', message=f'User {email} logged in successfully with OTP')
                log.save()
                
                response['status'] = 200
                response['msg'] = "OTP verified successfully"
                
                # Determine redirect URL
                nextUrl = request.GET.get('next')
                if nextUrl:
                    response['redirectUrl'] = nextUrl
                else:
                    services = ServiceModel.objects.filter()
                    if services:
                        redirect_url = '/dashboard'
                        for service in services:
                            if not UserNotificationSetingsModel.objects.filter(
                                service_id=service.id, 
                                user_id=user_obj.id, 
                                is_saved=True
                            ).exists():
                                redirect_url = '/profile?next=/dashboard'
                                break
                        response['redirectUrl'] = redirect_url
                    else:
                        response['redirectUrl'] = '/dashboard'

                # Check if Google Authenticator needs setup
                try:
                    totp_obj = UserTOTP.objects.get(user=user_obj)
                    if not totp_obj.is_enabled:
                         response['needs_google_auth_setup'] = True
                except UserTOTP.DoesNotExist:
                    response['needs_google_auth_setup'] = True
                except Exception:
                     # If table doesn't exist or other error, assume we can't setup
                     pass
            else:
                response['status'] = 400
                response['msg'] = "Invalid OTP. Please try again"
                
                # Log failure
                log = AuditlogsModel(username=user_obj, action='User login', status='Failure', message=f'User {email} entered incorrect OTP')
                log.save()
                
        except json.JSONDecodeError:
            response['status'] = 400
            response['msg'] = "Invalid request data"
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error verifying OTP: {str(e)}"
        
        return HttpResponse(json.dumps(response), content_type="application/json")
    
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
def resend_otps(request):
    """Resend OTP to user's email"""
    if request.method == 'POST':
        response = {}
        try:
            parsed_json = json.loads(request.POST.get('alldata', '{}'))
            email = parsed_json.get('username')

            if not email:
                response['status'] = 400
                response['msg'] = "Email is required"
                return HttpResponse(json.dumps(response), content_type="application/json")

            # Get user
            try:
                user_obj = User.objects.get(username=email)
            except User.DoesNotExist:
                response['status'] = 404
                response['msg'] = "User not found"
                return HttpResponse(json.dumps(response), content_type="application/json")

            # Check if user is active
            if not user_obj.is_active:
                response['status'] = 403
                response['msg'] = "Account is disabled. Contact the Admin"
                return HttpResponse(json.dumps(response), content_type="application/json")

            # Generate new OTP
            otp = randint(100000, 999999)
            Userotp.objects.update_or_create(
                user=user_obj, defaults={'otp': otp, 'created_at': datetime.now()}
            )

            # Prepare display name (fallback if empty)
            display_name = user_obj.first_name or user_obj.username

            # Send OTP email using helper function
            success, message = send_otp_email(email, display_name, otp)
            
            if success:
                response['status'] = 200
                response['msg'] = f"OTP resent successfully to {email}"

                # Log success
                AuditlogsModel.objects.create(username=user_obj, action='OTP Resend', status='Success', message=f'OTP resent successfully to {email}')
            else:
                response['status'] = 500
                response['msg'] = message

                # Log failure
                AuditlogsModel.objects.create(username=user_obj, action='OTP Resend', status='Failure', message=f'Failed to resend OTP to {email}: {message}')

        except json.JSONDecodeError:
            response['status'] = 400
            response['msg'] = "Invalid request data"

        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error resending OTP: {str(e)}"

        return HttpResponse(json.dumps(response), content_type="application/json")

    # Method not allowed
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
@login_required
def setup_google_authenticator(request):
    """Generate QR code for Google Authenticator setup"""
    if request.method == 'POST':
        response = {}
        try:
            user = request.user
            
            # Check if user already has TOTP enabled
            totp_obj, created = UserTOTP.objects.get_or_create(user=user)
            
            # Generate new secret if not exists or if user wants to reset
            # Use deterministic secret generation for Single Entry across environments
            if not totp_obj.secret_key or request.POST.get('reset') == 'true':
                secret = generate_deterministic_secret(user.username)
                totp_obj.secret_key = secret
                totp_obj.is_enabled = False  # Not enabled until verified
                totp_obj.save()
            
            # Generate provisioning URI
            totp = pyotp.TOTP(totp_obj.secret_key)
            issuer_name = "LinkedEye"
            provisioning_uri = totp.provisioning_uri(
                name=user.email or user.username,
                issuer_name=issuer_name
            )
            
            # Generate QR code
            qr = qrcode.QRCode(version=1, box_size=10, border=5)
            qr.add_data(provisioning_uri)
            qr.make(fit=True)
            
            img = qr.make_image(fill_color="black", back_color="white")
            buffer = io.BytesIO()
            img.save(buffer, format='PNG')
            buffer.seek(0)
            
            # Convert to base64 for embedding in HTML
            img_str = base64.b64encode(buffer.getvalue()).decode()
            
            response['status'] = 200
            response['qr_code'] = f'data:image/png;base64,{img_str}'
            response['secret'] = totp_obj.secret_key
            response['manual_entry_key'] = totp_obj.secret_key  # For manual entry
            
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error setting up Google Authenticator: {str(e)}"
        
        return HttpResponse(json.dumps(response), content_type="application/json")
    
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
@login_required
def verify_google_authenticator_setup(request):
    """Verify Google Authenticator code during setup"""
    if request.method == 'POST':
        response = {}
        try:
            parsed_json = json.loads(request.POST.get('alldata', '{}'))
            code = parsed_json.get('code')
            
            if not code:
                response['status'] = 400
                response['msg'] = "Code is required"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            user = request.user
            
            try:
                totp_obj = UserTOTP.objects.get(user=user)
            except UserTOTP.DoesNotExist:
                response['status'] = 404
                response['msg'] = "Google Authenticator not set up. Please set it up first."
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Verify the code
            totp = pyotp.TOTP(totp_obj.secret_key)
            if totp.verify(code, valid_window=1):
                # Code is valid, enable Google Authenticator
                totp_obj.is_enabled = True
                totp_obj.save()
                
                response['status'] = 200
                response['msg'] = "Google Authenticator enabled successfully"
                
                # Log success
                log = AuditlogsModel(
                    username=user,
                    action='Google Authenticator Setup',
                    status='Success',
                    message=f'User {user.username} enabled Google Authenticator'
                )
                log.save()
            else:
                response['status'] = 400
                response['msg'] = "Invalid code. Please try again."
        
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error verifying code: {str(e)}"
        
        return HttpResponse(json.dumps(response), content_type="application/json")
    
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
@login_required
def disable_google_authenticator(request):
    """Disable Google Authenticator for a user"""
    if request.method == 'POST':
        response = {}
        try:
            user = request.user
            
            try:
                totp_obj = UserTOTP.objects.get(user=user)
                totp_obj.is_enabled = False
                totp_obj.save()
                
                response['status'] = 200
                response['msg'] = "Google Authenticator disabled successfully"
                
                # Log action
                log = AuditlogsModel(
                    username=user,
                    action='Google Authenticator Disable',
                    status='Success',
                    message=f'User {user.username} disabled Google Authenticator'
                )
                log.save()
            except UserTOTP.DoesNotExist:
                response['status'] = 404
                response['msg'] = "Google Authenticator is not set up"
        
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error disabling Google Authenticator: {str(e)}"
        
        return HttpResponse(json.dumps(response), content_type="application/json")
    
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
def check_google_authenticator_status(request):
    """Check if user has Google Authenticator enabled (for login flow)"""
    if request.method == 'POST':
        response = {}
        try:
            parsed_json = json.loads(request.POST.get('alldata', '{}'))
            email = parsed_json.get('username')
            
            if not email:
                response['status'] = 400
                response['msg'] = "Email is required"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            try:
                user_obj = User.objects.get(username=email)
                totp_obj = UserTOTP.objects.get(user=user_obj)
                
                response['status'] = 200
                response['has_google_auth'] = totp_obj.is_enabled
            except User.DoesNotExist:
                response['status'] = 404
                response['msg'] = "User not found"
            except UserTOTP.DoesNotExist:
                response['status'] = 200
                response['has_google_auth'] = False
        
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error checking status: {str(e)}"
        
        return HttpResponse(json.dumps(response), content_type="application/json")
    
    return HttpResponse(json.dumps(response), content_type="application/json")

@csrf_exempt
def verify_google_authenticator_login(request):
    """Verify Google Authenticator code during login"""
    if request.method == 'POST':
        response = {}
        try:
            parsed_json = json.loads(request.POST.get('alldata', '{}'))
            email = parsed_json.get('username')
            code = parsed_json.get('code')
            
            if not email or not code:
                response['status'] = 400
                response['msg'] = "Email and code are required"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Get user
            if not User.objects.filter(username=email).exists():
                response['status'] = 404
                response['msg'] = "User not found"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            user_obj = User.objects.get(username=email)
            
            # Get TOTP record
            try:
                totp_obj = UserTOTP.objects.get(user=user_obj)
            except UserTOTP.DoesNotExist:
                response['status'] = 404
                response['msg'] = "Google Authenticator is not set up for this user"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            if not totp_obj.is_enabled:
                response['status'] = 400
                response['msg'] = "Google Authenticator is not enabled for this user"
                return HttpResponse(json.dumps(response), content_type="application/json")
            
            # Verify the code
            totp = pyotp.TOTP(totp_obj.secret_key)
            if totp.verify(code, valid_window=1):
                # Code is valid - log user in
                auth.login(request, user_obj, backend='django.contrib.auth.backends.ModelBackend')
                apply_session_timeout(request)

                # Set user permissions
                if user_obj.groups.exists():
                    request.session['user_permissions'] = get_user_permissions(highest_weightage_group_name(user_obj))

                # Clear the Finspot SSO OTP-pending gate, if this verification
                # came from that flow (no-op for normal-login, which never
                # sets these keys).
                request.session.pop('otp_pending', None)
                request.session.pop('otp_pending_email', None)
                request.session.pop('otp_pending_next', None)

                # Update last used timestamp
                totp_obj.last_used = timezone.now()
                totp_obj.save()
                
                # Log success
                log = AuditlogsModel(
                    username=user_obj,
                    action='User login',
                    status='Success',
                    message=f'User {email} logged in successfully with Google Authenticator'
                )
                log.save()
                
                response['status'] = 200
                response['msg'] = "Google Authenticator verified successfully"
                
                # Determine redirect URL
                nextUrl = request.GET.get('next')
                if nextUrl:
                    response['redirectUrl'] = nextUrl
                else:
                    services = ServiceModel.objects.filter()
                    if services:
                        redirect_url = '/dashboard'
                        for service in services:
                            if not UserNotificationSetingsModel.objects.filter(
                                service_id=service.id,
                                user_id=user_obj.id,
                                is_saved=True
                            ).exists():
                                redirect_url = '/profile?next=/dashboard'
                                break
                        response['redirectUrl'] = redirect_url
                    else:
                        response['redirectUrl'] = '/dashboard'
            else:
                response['status'] = 400
                response['msg'] = "Invalid Google Authenticator code. Please try again."
                
                # Log failure
                log = AuditlogsModel(
                    username=user_obj,
                    action='User login',
                    status='Failure',
                    message=f'User {email} entered incorrect Google Authenticator code'
                )
                log.save()
        
        except json.JSONDecodeError:
            response['status'] = 400
            response['msg'] = "Invalid request data"
        except Exception as e:
            response['status'] = 500
            response['msg'] = f"Error verifying Google Authenticator: {str(e)}"

        return HttpResponse(json.dumps(response), content_type="application/json")

    response = {'status': 405, 'msg': 'Method not allowed'}
    return HttpResponse(json.dumps(response), content_type="application/json")

# ─────────────────────────────────────────────────
# HIGH FIX #3: Health Check Endpoint for K8s Probes
# ─────────────────────────────────────────────────
def health_check(request):
    """
    Health check endpoint for Kubernetes liveness/readiness probes.
    Returns process stats without hitting the database.
    HIGH FIX #3: Required for K8s deployment
    """
    import os
    import threading
    import psutil
    
    try:
        proc = psutil.Process(os.getpid())
        health_data = {
            "status": "ok",
            "rss_mb": round(proc.memory_info().rss / 1024 / 1024, 1),
            "threads": threading.active_count(),
            "connections": len(proc.connections()),
            "cpu_percent": proc.cpu_percent(interval=0.1),
        }
        return JsonResponse(health_data, status=200)
    except Exception as e:
        return JsonResponse({"status": "error", "message": str(e)}, status=500)


@login_required
def ws_config(request):
    """Serve the STOMP credentials the dashboards use, at request time.

    These were hardcoded as connect('linkedeye', 'linkedeye', ...) in 19 static
    JS files. That had two problems beyond being stale:

      * /static/**.js is served without authentication, so the broker password
        was readable by anyone who could reach the host -- and the account is a
        RabbitMQ administrator with '.*' on every vhost.
      * Rotating linkedeye-mq-secret silently broke every websocket in the UI
        until all 19 files were hand-edited to match, which is exactly how they
        came to be broken.

    Reading the same env var the backend already uses (RMQ_PASS, wired from
    linkedeye-mq-secret) keeps the credential out of source control and in step
    with the secret: rotate the secret, restart the pod, done.

    login_required so the credential is not served to anonymous callers.
    no-store so a rotated password is never replayed from a browser cache.

    Longer term this should hand out a short-lived, read-only token scoped to
    the topics the dashboards subscribe to, rather than the admin password.
    """
    body = "window.LE_WS_USER = %s;\nwindow.LE_WS_PASS = %s;\n" % (
        json.dumps(os.getenv('RMQ_USER', 'linkedeye')),
        json.dumps(os.getenv('RMQ_PASS', '')),
    )
    response = HttpResponse(body, content_type='application/javascript')
    response['Cache-Control'] = 'no-store, no-cache, must-revalidate'
    return response
