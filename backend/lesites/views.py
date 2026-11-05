"""
LE Sites views.
FIXED: SQL injection replaced with parameterized queries.
FIXED: print() replaced with logger.
"""

from django.views.decorators.clickjacking import xframe_options_exempt
from django.contrib.auth.decorators import login_required
from django.template import loader
from lesites.models import SiteModel, LocationModel, CountryModel, StateModel
from django.contrib.auth.decorators import login_required

from django.shortcuts import render,HttpResponse
import json
import logging
from django.contrib.auth.models import User
from useronboard.models import Usersite
from django.forms.models import model_to_dict
from django.db import connection
from auditlogs.models import AuditlogsModel
from lesites.site_queries import LESITE_SELECT, REMOVED_SITE_KEYS
from django.conf import settings

logger = logging.getLogger('linkedeye')


def _apply_local_le_url(site_list, local_url, ws_url=None):
    """When running locally (DEBUG=True), replace cluster-internal URLs with local ones."""
    url = local_url.rstrip('/') + '/'
    for site in site_list:
        site['le_url'] = url
        if ws_url is not None:
            site['websocket_url'] = ws_url

@login_required(login_url="/")

def siteactions(request):
    if request.method == 'POST':
        response = {}
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                sitename = parsed_json['sitename']
                if SiteModel.objects.filter(sitename = sitename).exists():
                    response['msg'] = 'Site name already exist.'
                    response['status'] = 500
                    log = AuditlogsModel(username = request.user,  action = 'Site Onboarding', status = 'Failure', message= 'Site name ' +sitename+ 'already exist.')
                else:
                    obj = SiteModel(
                        sitename=sitename,
                        location=parsed_json['location'],
                        websocket_url=parsed_json['websocketurl'],
                        entity_host=parsed_json['entityhost'],
                        entity_port=parsed_json['entityport'],
                        is_URLSecured=parsed_json['isURLSecured'],
                        environment=parsed_json["environment"],
                        analytics_Prefix_URL=parsed_json['prefixurl'],
                        redis_host=parsed_json['redishost'],
                        redis_port=parsed_json['redisport'],
                        is_enable=True,
                        prometheus_url=parsed_json['prometheusurl'],
                        elastic_host=parsed_json['elastichost'],
                        elastic_port=parsed_json['elasticport'],
                        grafana_api=parsed_json['grafapi'],
                        le_url=parsed_json['leurl'],
                        lat=parsed_json['lat'],
                        lng=parsed_json['lng'],
                        incident_url=parsed_json.get('incidenturl'),
                        incident_api=parsed_json.get('incidentapi'),
                    )
                    logger.debug("lesites views: %s", obj)
                    obj.save()
                    site_id = SiteModel.objects.get(sitename = sitename).id
                    for obj in parsed_json['users']:
                        obj = Usersite(user_id = obj['user_id'],  site_id = site_id, is_enable = obj['isEnabled'])
                        obj.save()
                    response['msg'] = 'Successfully added site'
                    response['status'] = 200
                    response['rowid'] = site_id
                    log = AuditlogsModel(username = request.user,  action = 'Site Onboarding', status = 'Success', message= 'Site '+sitename +' onboarded successfully')
            elif parsed_json["operation"] == 'delete':
                deleteobj = SiteModel.objects.get(id=parsed_json["rowid"])
                log = AuditlogsModel(username = request.user,  action = 'Site Delete', status = 'Success', message= 'Site '+deleteobj.sitename +' deleted successfully')
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Successfully deleted site'
                response['rowid'] = parsed_json["rowid"]
            elif parsed_json["operation"] == 'update':
                obj = SiteModel.objects.get(id = parsed_json["rowid"], sitename = parsed_json["sitename"])
                obj.location = parsed_json["location"]
                obj.websocket_url = parsed_json["websocketurl"]
                obj.entity_host = parsed_json["entityhost"]
                obj.entity_port = parsed_json["entityport"]
                obj.is_URLSecured = parsed_json["isURLSecured"]
                obj.environment = parsed_json["environment"]
                obj.analytics_Prefix_URL = parsed_json["prefixurl"]
                obj.redis_host = parsed_json["redishost"]
                obj.redis_port = parsed_json["redisport"]
                obj.prometheus_url = parsed_json["prometheusurl"]
                obj.elastic_host = parsed_json["elastichost"]
                obj.elastic_port = parsed_json["elasticport"]
                obj.grafana_api = parsed_json["grafapi"]
                obj.le_url = parsed_json["leurl"]
                obj.lat = parsed_json["lat"]
                obj.lng = parsed_json["lng"]
                obj.incident_url = parsed_json["incidenturl"]
                obj.incident_api = parsed_json["incidentapi"]
                obj.save()
                if Usersite.objects.filter(site_id=parsed_json["rowid"]).exists():
                    Usersite.objects.filter(site_id=parsed_json["rowid"]).delete()
                for obj in parsed_json['users']:
                    obj = Usersite(user_id = obj['user_id'],  site_id = parsed_json["rowid"], is_enable = True)
                    obj.save()
                response['status'] = 200
                response['msg'] = 'Successfully updated site'
                response['rowid'] =  parsed_json["rowid"]
                log = AuditlogsModel(username = request.user,  action = 'Site Update', status = 'Success', message= 'Site '+parsed_json["sitename"] +' updated successfully')
            elif parsed_json["operation"] == 'changestatus':
                obj = SiteModel.objects.get(id=parsed_json["rowid"])
                if parsed_json["status"] == 'Enable':
                    obj.is_enable = False
                    obj.save() 
                    response['msg'] = 'Disable'
                    response['status'] = 200
                    log = AuditlogsModel(username = request.user,  action = 'Change Site Staus', status = 'Success', message= 'Site '+obj.sitename +' disabled successfully')
                else:
                    obj.is_enable = True
                    obj.save() 
                    response['msg'] = 'Enable'
                    response['status'] = 200
                    log = AuditlogsModel(username = request.user,  action = 'Change Site Staus', status = 'Success', message= 'Site '+obj.sitename +' enabled successfully')
        except Exception as e:
            logger.error("siteactions exception: %s", e)
            response['status'] = 400
            response['msg'] = str(e)
            log = AuditlogsModel(username = request.user,  action = 'Change Site Staus', status = 'Failure', message=str(e))
        log.save()
        return HttpResponse(json.dumps(response), content_type="json")
def getallsitenames(request):
    response = {}
    try:
        if request.method == 'POST':
            user_id = json.loads(request.POST['userId'])
            siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
            response['data'] = [x for x in siteid_list]
        else:
            user_id = User.objects.get(username=request.user).id
            cursor = connection.cursor()
            if request.GET["type"] == 'clicksite':
                # FIXED: parameterized query to prevent SQL injection
                cursor.execute(
                    f"select {LESITE_SELECT} from lesite INNER JOIN user_sites on (user_sites.site_id = lesite.id) where (user_sites.user_id=%s and lesite.is_enable=%s and lesite.sitename=%s)",
                    [user_id, True, request.GET["site"]]
                )
                resultList = fetchall(cursor)
                if settings.DEBUG:
                    _apply_local_le_url(resultList, request.scheme + '://' + request.get_host(), settings.WEBSOCKET_URL)
                response['data'] = resultList
            elif request.GET["type"] == 'userbased':
                if request.GET["isOnlyEnabled"] == 'true':
                    cursor.execute(
                        f"select {LESITE_SELECT} from lesite INNER JOIN user_sites on (user_sites.site_id = lesite.id) where (user_sites.user_id=%s and lesite.is_enable=%s)",
                        [user_id, True]
                    )
                else:
                    cursor.execute(
                        f"select {LESITE_SELECT}, user_sites.is_enable from lesite INNER JOIN user_sites on (user_sites.site_id = lesite.id) where (user_sites.user_id=%s)",
                        [user_id]
                    )
                resultList = fetchall(cursor)
                if settings.DEBUG:
                    _apply_local_le_url(resultList, request.scheme + '://' + request.get_host(), settings.WEBSOCKET_URL)
                response['data'] = resultList
            elif request.GET["type"] == 'locationbased':
                location = request.GET['location']
                cursor.execute(
                    f"select {LESITE_SELECT} from lesite INNER JOIN user_sites on (user_sites.site_id = lesite.id) where (user_sites.user_id=%s and lesite.is_enable=%s and lesite.location=%s)",
                    [user_id, True, location]
                )
                resultList = fetchall(cursor)
                if settings.DEBUG:
                    _apply_local_le_url(resultList, request.scheme + '://' + request.get_host(), settings.WEBSOCKET_URL)
                response['data'] = resultList
            else:
                temp_list = []
                site_obj = SiteModel.objects.all()
                for temp in site_obj:
                    json_obj = {}
                    json_obj["id"] = temp.id
                    json_obj["sitename"] = temp.sitename
                    json_obj["websocket_url"] = temp.websocket_url if not settings.DEBUG else settings.WEBSOCKET_URL
                    json_obj["location"] = temp.location
                    json_obj["entity_host"] = temp.entity_host
                    json_obj["entity_port"] = temp.entity_port
                    json_obj["is_URLSecured"] = temp.is_URLSecured
                    json_obj["environment"] = temp.environment
                    json_obj["analytics_Prefix_URL"] = temp.analytics_Prefix_URL
                    json_obj["redis_host"] = temp.redis_host
                    json_obj["redis_port"] = temp.redis_port
                    json_obj["is_enable"] = temp.is_enable
                    json_obj["prometheus_url"] = temp.prometheus_url
                    json_obj["elastic_host"] = temp.elastic_host
                    json_obj["elastic_port"] = temp.elastic_port
                    json_obj["grafana_api"] = temp.grafana_api
                    json_obj["le_url"] = temp.le_url if not settings.DEBUG else request.scheme + '://' + request.get_host() + '/'
                    json_obj["lat"] = temp.lat
                    json_obj["lng"] = temp.lng
                    json_obj["incident_url"] = temp.incident_url
                    json_obj["incident_api"] = temp.incident_api
                    temp_list.append(json_obj)
                response['data'] = temp_list
        response['status'] = 200
        return HttpResponse(json.dumps(response))
    except Exception as e:
        logger.error("getallsitenames exception: %s", e)
        response['status'] = 400
        response['msg'] = 'No Sites Available'
        return HttpResponse(json.dumps(response))
def locationactions(request):
    if request.method == 'POST':
        response = {}
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                locationname = parsed_json['locationname']
                if LocationModel.objects.filter(locationname = locationname).exists():
                    response['msg'] = 'Location name already exist.'
                    response['status'] = 500
                else:
                    obj = LocationModel(locationname = locationname)
                    obj.save()
                    response['msg'] = 'Sucessfully added location'
                    response['status'] = 200
                    response['rowid'] = LocationModel.objects.get(locationname = locationname).id
            elif parsed_json["operation"] == 'delete':
                deleteobj = LocationModel.objects.get(id=parsed_json["rowid"])
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Successfully deleted location'
                response['rowid'] = parsed_json["rowid"]
        except Exception as e:
            logger.error("locationactions exception: %s", e)
            response['status'] = 400
            response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")
def countryactions(request):
    if request.method == 'POST':
        response = {}
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                country = parsed_json['countryname']
                if CountryModel.objects.filter(countryname = countryname).exists():
                    response['msg'] = 'country name already exist.'
                    response['status'] = 500
                else:
                    obj = CountryModel(countryname = countryname)
                    obj.save()
                    response['msg'] = 'Sucessfully added country'
                    response['status'] = 200
                    response['rowid'] = CountryModel.objects.get(countryname = countryname).id
            elif parsed_json["operation"] == 'delete':
                deleteobj = CountryModel.objects.get(id=parsed_json["rowid"])
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Successfully deleted countryname'
                response['rowid'] = parsed_json["rowid"]
        except Exception as e:
            logger.error("countryactions exception: %s", e)
            response['status'] = 400
            response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")

def stateactions(request):
    if request.method == 'POST':
        response = {}
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                state = parsed_json['statename']
                if StateModel.objects.filter(statename = statename).exists():
                    response['msg'] = 'state name already exist.'
                    response['status'] = 500
                else:
                    obj = StateModel(statename = statename)
                    obj.save()
                    response['msg'] = 'Sucessfully added country'
                    response['status'] = 200
                    response['rowid'] = StateModel.objects.get(statename = statename).id
            elif parsed_json["operation"] == 'delete':
                deleteobj = StateModel.objects.get(id=parsed_json["rowid"])
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Successfully deleted countryname'
                response['rowid'] = parsed_json["rowid"]
        except Exception as e:
            logger.error("stateactions exception: %s", e)
            response['status'] = 400
            response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")
def get_site_locations(request):
    response = {}
    try:
        temp_list = []
        app_obj = LocationModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["locationname"] = temp.locationname
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def get_site_country(request):
    response = {}
    logger.debug("inside get_site_country")
    try:
        temp_list = []
        app_obj = CountryModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["countryid"] = temp.countryid
            json_obj["countryname"] = temp.countryname
            json_obj["countryshortname"] = temp.countryshortname
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def get_site_state(request):
    response = {}
    logger.debug("inside get_site_state")
    try:
        temp_list = []
        countryid = request.GET.get('countryid') # countryid = 1
        app_obj = StateModel.objects.all()
        for temp in app_obj:  
            json_obj = {}
            json_obj["stateid"] = temp.stateid
            json_obj["countryid"] = temp.countryid
            json_obj["stateshortname"] = temp.stateshortname
            json_obj["statename"] = temp.statename
            json_obj["lat"] = temp.lat
            json_obj["lng"] = temp.lng
            if int(temp.countryid) == int(countryid):
                temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def fetchall(cursor): 
    objs = cursor.fetchall()
    description = cursor.description
    result = []   
    for obj in objs:
        i = 0
        item = {}
        while i < len(description):
            item[description[i][0]] = str(obj[i])
            i = i+1
        for key in REMOVED_SITE_KEYS:
            item.pop(key, None)
        result.append(item)
    return result

