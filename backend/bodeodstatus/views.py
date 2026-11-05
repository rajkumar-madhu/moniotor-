from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
from lib.LinkedEyeRedis import Redis
from django.http import HttpResponse
from json import dumps as jdumps
import os
import ast
import requests
from django.conf import settings
from lesites.models import SiteModel
from django.contrib.auth.models import User
from useronboard.models import Usersite


def _use_remote_bod_api_first(site_obj):
    """Use the site's public API when lesite points at another cluster's Redis.

    Global LE stores remote worker ClusterIPs (e.g. 10.109.234.2) that are not
    routable from the aggregator node. Calling le_url first avoids a ~3s Redis
    timeout per site on every BOD/EOD request. Standalone clients keep local
    redis_host == REDIS_HOST and still use direct Redis.
    """
    le_url = (getattr(site_obj, 'le_url', None) or '').strip()
    if not le_url:
        return False
    local_redis = (os.getenv('REDIS_HOST') or '').strip()
    remote_redis = (getattr(site_obj, 'redis_host', None) or '').strip()
    if not remote_redis:
        return False
    if local_redis and remote_redis == local_redis:
        return False
    return True


def _fetch_remote_bod_eodkeys(site_obj, raw_sitename, mode, ip):
    """Fallback to the site's own public endpoint when direct Redis access fails."""
    if not getattr(site_obj, "le_url", ""):
        raise Exception("Missing le_url for remote fallback")

    params = {
        "sitename": site_obj.sitename or raw_sitename,
        "mode": mode,
    }
    if mode == "VER":
        params["ip"] = ip

    remote_url = site_obj.le_url.rstrip("/") + "/bod-eodstatus/getbodeodkeys"
    remote_response = requests.get(remote_url, params=params, timeout=20)
    remote_response.raise_for_status()
    payload = remote_response.json()
    if not isinstance(payload, dict) or not payload.get("responseData"):
        raise Exception("Remote BOD fallback returned no data")
    return payload

@login_required(login_url="/")

@role_required(allowed_roles=["Admin", "Management", "ViewOnly", "Onboard", "Risk", "UserView", "TradeSupport"])
def adpstatus(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL
                }
    return render(request, 'app/adp-status.html', temp_json)

@role_required(allowed_roles=["Admin", "Management", "ViewOnly", "Onboard", "Risk", "UserView", "TradeSupport"])
def bodeodstatus(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL
                }
    return render(request, 'app/bodeod.html', temp_json)

@role_required(allowed_roles=["Admin", "Management", "ViewOnly", "Onboard", "Risk", "UserView", "TradeSupport"])
def eodstatus(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL
                }
    return render(request, 'app/eod-status.html', temp_json)

@role_required(allowed_roles=["Admin", "Management", "ViewOnly", "Onboard", "Risk", "UserView", "TradeSupport"])
def leadp_status(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL
                }
    return render(request, 'app/linkedeye-adp-status.html', temp_json)



def getbodeodkeys(request):
    response = { }
    responseObj = []
    raw_sitename = request.GET.get("sitename", "")
    mode = request.GET.get("mode", "ALL")
    ip = request.GET.get("ip", "")
    if raw_sitename == ' ':
        user_id = User.objects.get(username=request.user).id
        siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
        site_ids = [x for x in siteid_list]
        site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)
        response["refreshedsite"] = ' '
    else:
        sitename = raw_sitename
        site_objs = SiteModel.objects.filter(sitename = sitename)
        response["refreshedsite"] = sitename
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        if _use_remote_bod_api_first(site_obj):
            try:
                remote_payload = _fetch_remote_bod_eodkeys(
                    site_obj=site_obj,
                    raw_sitename=raw_sitename,
                    mode=mode,
                    ip=ip,
                )
                remote_data = remote_payload.get("responseData", [])
                temp_obj = remote_data[0]
                temp_obj["site"] = site_obj.sitename
                temp_obj["code"] = remote_data[0].get("code", 200)
            except Exception as remote_ex:
                print('=======Remote BOD API failed=====')
                print(str(remote_ex))
                temp_obj["site_data"] = []
                temp_obj["code"] = 500
            responseObj.append(temp_obj)
            continue
        try:
            redisObj = Redis(host=site_obj.redis_host, port=site_obj.redis_port)
            if mode!="VER":
                redisKeys = redisObj.getBodEodkeys(site=site_obj.sitename,mode=mode)
            else:
                redisKeys = redisObj.getBodEodkeys(site=site_obj.sitename,mode=mode,ip=ip)
            keys = []
            for key in redisKeys:
                tempObj = {}
                tempObj["key"] = key
                test=redisObj.get(key)
                tempObj["key_data"] = ast.literal_eval(redisObj.get(key))
                keys.append(tempObj)
            temp_obj["site_data"] = keys
            temp_obj["code"] = 200
        except Exception as e:
            try:
                remote_payload = _fetch_remote_bod_eodkeys(
                    site_obj=site_obj,
                    raw_sitename=raw_sitename,
                    mode=mode,
                    ip=ip,
                )
                remote_data = remote_payload.get("responseData", [])
                temp_obj = remote_data[0]
                temp_obj["site"] = site_obj.sitename
                temp_obj["code"] = remote_data[0].get("code", 200)
            except Exception as remote_ex:
                print('=======Exception=====')
                print(str(e))
                print('=======Remote fallback failed=====')
                print(str(remote_ex))
                temp_obj["site_data"] = []
                temp_obj["code"] = 500
        responseObj.append(temp_obj)
    response["status"] = 200
    response["responseData"] = responseObj
    return HttpResponse(jdumps(response), content_type="json")

def read_file(request):
    response = {}
    try:
        print("HOME PATH - {}".format(os.getcwd()))
        filepath = request.POST["filepath"]
        f = open(filepath, 'r')
        response['file_content'] = f.read()
        f.close()
        response["status"] = 200
    except Exception as e:
        print('===Exception===read_file==')
        print(str(e))
        response["status"] = 500
        response["emsg"] = 'File not found.'
    return HttpResponse(jdumps(response), content_type="json")

def updatekeys(request):
    try:
        response = { }
        responsedata = ''
        site = request.GET["sitename"]
        existing_key = request.GET["existing_key"]
        value = request.GET["value"]
        site_objs = SiteModel.objects.filter(sitename = site)
        response["refreshedsite"] = site
        for site_obj in site_objs:
            temp_obj = {}
            temp_obj['site'] = site_obj.sitename
            try:
                redisObj = Redis(host=site_obj.redis_host, port=site_obj.redis_port)
                redisResult = redisObj.update(existing_key=existing_key,add_key='edit',add_value=value)
                for key in redisResult:
                    print(key)
                if(redisResult['status']==200):
                    temp_obj["site_data"] = existing_key+' has been updated.'
                    temp_obj["code"] = 200
                else:
                    temp_obj["site_data"] = existing_key+' is not updated.'
                    temp_obj["code"] = 500
            except Exception as e:
                print('=======Exception while updating EDIT keys=====')
                print(str(e))
                temp_obj["site_data"] = []
                temp_obj["code"] = 500
        response["status"] = 200
        response["responseData"] = temp_obj
        #return HttpResponse(jdumps(response), content_type="json")
    except Exception as e:
            print("===Exception====countryactions===")
            print(str(e))
            response['status'] = 400
            response['responseData'] = 'Something went wrong while updating EDIT keys'
    return HttpResponse(jdumps(response), content_type="json")
