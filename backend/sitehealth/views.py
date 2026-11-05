from django import template
from django.shortcuts import render
from django.db import connection, transaction
from django.template import loader
from django.http import HttpResponse
from datetime import datetime, timedelta, date
from json import dumps as jdumps
import sys, os, json
import ast
import requests
from requests.auth import HTTPBasicAuth
from django.contrib.auth.decorators import login_required
from django.conf import settings
from django.forms.models import model_to_dict
from lib.LinkedEyeRedis import Redis
from django.core import serializers
from django.http import JsonResponse
from django.contrib.auth.models import User
from lesites.models import SiteModel
from lib.LinkedEyeVault.AppSecrets import get_app_secret

#@login_required(login_url="/")

# Credential for the server-to-server sitehealth hop. Sister LE deployments
# (MobiKwik since 2026-09-01) refuse /sitehealth/overall with 401 unless the
# caller has a logged-in session or presents this shared secret. The browser
# calls GLE same-origin; GLE forwards to the site's le_url with this header.
# The same value must be provisioned as LE_SERVICE_TOKEN in Vault on every
# deployment that sends or receives it.
SERVICE_TOKEN_HEADER = 'X-LE-Service-Token'

# Marks a request that has already been proxied once, so a le_url that routes
# back to a deployment which would proxy again cannot recurse forever.
PROXY_HEADER = 'X-LE-Proxied'

# map.js getJSON uses xhr.timeout = 5000; a longer server-side timeout only
# keeps a gunicorn worker busy after the browser has already given up.
REMOTE_TIMEOUT_SECONDS = 4


def _service_token():
    """Shared secret for the cross-site sitehealth hop, Vault-backed like the rest."""
    return (get_app_secret('LE_SERVICE_TOKEN', env_var='LE_SERVICE_TOKEN', default='') or '').strip()


def _lesite_uses_local_redis(site_obj):
    """True when this process can read the site's Redis directly.

    Same rule for a single-site deploy and a multi-site aggregator: if
    lesite.redis_host is this process's REDIS_HOST, use it; otherwise the
    site's Redis is on another cluster and we use that row's le_url.
    """
    site_redis = (getattr(site_obj, 'redis_host', None) or '').strip()
    if not site_redis:
        return True
    local_redis = (os.getenv('REDIS_HOST') or '').strip()
    if not local_redis:
        return True
    return site_redis == local_redis


def _fetch_remote_sitehealth(site_obj, sitename):
    le_url = (getattr(site_obj, 'le_url', None) or '').strip()
    if not le_url:
        raise Exception("Missing le_url for remote sitehealth")
    remote_url = le_url.rstrip("/") + "/sitehealth/overall"
    headers = {PROXY_HEADER: '1'}
    token = _service_token()
    if token:
        headers[SERVICE_TOKEN_HEADER] = token
    else:
        print("LE_SERVICE_TOKEN is unset; the sitehealth hop to " + remote_url + " will be refused with 401")
    remote_response = requests.get(
        remote_url,
        params={"sitename": site_obj.sitename or sitename},
        headers=headers,
        timeout=REMOTE_TIMEOUT_SECONDS,
        # A redirect would re-point the fetch at a host the lesite row never
        # named, so follow none of them.
        allow_redirects=False,
    )
    if remote_response.status_code in (301, 302, 303, 307, 308):
        raise Exception(
            "Remote sitehealth redirected to %s; refusing to follow"
            % remote_response.headers.get('Location', '(no location)')
        )
    remote_response.raise_for_status()
    payload = remote_response.json()
    if not isinstance(payload, dict) or payload.get("data") is None:
        raise Exception("Remote sitehealth returned no data")
    return payload


def _local_sitehealth(sitename, site_obj=None):
    redis_kwargs = {}
    entity_kwargs = {}
    if site_obj:
        if getattr(site_obj, 'redis_host', None):
            redis_kwargs = {
                'host': site_obj.redis_host,
                'port': site_obj.redis_port,
            }
        entity_kwargs = {
            'entity_host': getattr(site_obj, 'entity_host', None) or '',
            'entity_port': getattr(site_obj, 'entity_port', None) or '',
            'entity_secure': bool(getattr(site_obj, 'is_URLSecured', False)),
        }
        sitename = site_obj.sitename or sitename
    return Redis(**redis_kwargs).getSiteHealthNew(site=sitename, **entity_kwargs)


def sitehealth(request):
    try:
        response = {}
        sitename = (request.GET.get('sitename') or '').strip()
        print("sitehealth sitename = "+sitename)
        site_obj = SiteModel.objects.filter(sitename=sitename).first() if sitename else None
        if site_obj and not _lesite_uses_local_redis(site_obj):
            try:
                payload = _fetch_remote_sitehealth(site_obj, sitename)
                return JsonResponse(payload, json_dumps_params={'default': str})
            except Exception as remote_ex:
                print('=======Remote sitehealth API failed=====')
                print(str(remote_ex))
                # The site's Redis is on another cluster and is not reachable
                # from here, so a direct read can only hang and then return
                # zeros. Fail fast with a non-200 so the browser paints the
                # row red and keeps polling, and the gunicorn worker is free
                # for the other sites within the remote timeout.
                return JsonResponse(
                    {"code": 503, "message": "Remote sitehealth unavailable for " + sitename},
                    status=503,
                    json_dumps_params={'default': str},
                )
        response["data"] = _local_sitehealth(sitename, site_obj)
        response["code"] = 200

    except Exception as ex:
        response["code"] = 500
        response["message"] = str(ex)
    return JsonResponse(response, json_dumps_params={'default': str})
