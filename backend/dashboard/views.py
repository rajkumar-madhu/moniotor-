import sys, os, json, ast
import hmac
import yaml
from django.shortcuts import render
from django.http import HttpResponse, JsonResponse
from lib.LinkedEyeEntity.Node import Node
from json import dumps as jdumps
from django.contrib.auth.decorators import login_required
from django.conf import settings
from lesites.models import SiteModel
from django.contrib.auth.models import User
from useronboard.models import Usersite
from .models import SnmpModel
from allonboard.models import allonboardModel
from django.db import connection
from lib.LinkedEyeValidation import *


template_path = "snmp/"
cfg_path = "monitor/"

@login_required(login_url="/")
def index(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL,
                    "analysticsDashboardurl": settings.ANALYTICS_DASHBOARD_PREFIXURL
                }
    return render(request, 'app/dashboard.html', temp_json)


def backend_summary_api(request):
    """Read-only, bearer-authenticated summary for the replacement console."""
    if request.method != 'GET':
        return JsonResponse({'status': 405, 'error': 'method_not_allowed'}, status=405)
    expected = getattr(settings, 'DASHBOARD_API_TOKEN', '').strip()
    supplied = request.META.get('HTTP_AUTHORIZATION', '')
    if not expected:
        return JsonResponse({'status': 503, 'error': 'not_configured'}, status=503)
    if not supplied.startswith('Bearer ') or not hmac.compare_digest(supplied[7:].strip(), expected):
        return JsonResponse({'status': 401, 'error': 'unauthorized'}, status=401)

    names = list(dict.fromkeys(name.strip() for name in request.GET.getlist('site') if name.strip()))
    if not names or len(names) > 50:
        return JsonResponse({'status': 400, 'error': 'invalid_site_scope'}, status=400)

    statuses = ('CRITICAL', 'WARNING', 'OK', 'UNKNOWN')
    indexes = {'CRITICAL': 0, 'WARNING': 1, 'OK': 2, 'UNKNOWN': 3}
    totals = {module: {state: 0 for state in statuses}
              for module in ('hardware', 'software', 'application')}
    sites = list(SiteModel.objects.filter(sitename__in=names, is_enable=True))
    sites_ok = 0
    sites_failed = len(names) - len(sites)
    for site in sites:
        try:
            node = Node(host=site.entity_host, port=site.entity_port, secure=site.is_URLSecured)
            result = node.overallStats()
            if result.get('status') != 200 or not isinstance(result.get('data'), dict):
                sites_failed += 1
                continue
            for module in totals:
                values = result['data'].get(module, {})
                for state, index in indexes.items():
                    count = values.get(index, values.get(str(index), 0)) if isinstance(values, dict) else 0
                    try:
                        totals[module][state] += max(0, int(count))
                    except (TypeError, ValueError, OverflowError):
                        continue
            sites_ok += 1
        except Exception:
            sites_failed += 1

    if not sites_ok:
        return JsonResponse({'status': 502, 'error': 'backend_unavailable'}, status=502)
    return JsonResponse({
        'status': 200,
        'data': totals,
        'site_count': sites_ok,
        'partial': sites_failed > 0,
    })

def getneo4jnodes(request):
    summary = request.GET.get("summary", "false").lower() == "true"
    if request.GET["sitename"] == ' ':
        user_id = User.objects.get(username=request.user).id
        siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
        site_ids = [x for x in siteid_list]
        layer = request.GET.get("layer","All")
        ip = request.GET.get("ip","All")
        site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)
    else:
        #site ='fs-uat-p1'
        site = request.GET["sitename"]
        layer = request.GET.get("layer","All")
        ip = request.GET.get("ip","All")
        # print("dashboard ---- view --->"+ str(layer))
        site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        if summary and request.GET["sitename"] == ' ':
            temp_obj['site_data'] = {}
            temp_obj["code"] = 200
            data.append(temp_obj)
            continue
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            node_response = {}
            rel_response = {}
            node_res = nodeObj._visspecificnodedata(mode=layer,ip=ip)
            if node_res['status'] == 200:
                node_response['status'] = node_res['status']
                node_response['data'] = node_res['data']
            else:
                node_response['status'] = node_res['status']
            rel_res = nodeObj._visspecificrelationshipdata(mode=layer,ip=ip.split('ip_')[-1].replace('_','.'))
            if rel_res['status'] == 200:
                rel_response['status'] = rel_res['status']
                rel_response['data'] = rel_res['data']
            else:
                rel_response['status'] = rel_res['status']
            temp_obj['site_data'] = {"nodes":node_response, "relationships":rel_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['site_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def gethostandservicedetails(request):
    nodeObj = Node()
    host_response = {}
    service_response = {}
    hostms_response = {}
    servicems_response = {}
    host_result = nodeObj._getdashboardspecificdata()
    if host_result['status'] == 200:
        host_response['status'] = host_result['status']
        host_response['data'] = host_result['data']
    else:
        host_response['status'] = host_result['status']
    service_result = nodeObj._getdashboardspecificdata(mode="Service")
    if service_result['status'] == 200:
        service_response['status'] = service_result['status']
        service_response['data'] = service_result['data']
    else:
        service_response['status'] = service_result['status']
    hostms_result = nodeObj._getdashboardspecificdata(mode="HostMS")
    if hostms_result['status'] == 200:
        hostms_response['status'] = hostms_result['status']
        hostms_response['data'] = hostms_result['data']
    else:
        hostms_response['status'] = hostms_result['status']
    servicems_result = nodeObj._getdashboardspecificdata(mode="ServiceMS")
    if servicems_result['status'] == 200:
        servicems_response['status'] = servicems_result['status']
        servicems_response['data'] = servicems_result['data']
    else:
        servicems_response['status'] = servicems_result['status']
    tempJsonObj = {"host":host_response, "hostms":hostms_response, "service":service_response, "servicems": servicems_response}
    return HttpResponse(jdumps(tempJsonObj), content_type="json")

def gethostandservicecount(request):
    nodeObj = Node()
    host_response = {}
    service_response = {}
    host_result = nodeObj._getdashboarddata(datafor='Host')
    if host_result['status'] == 200:
        host_response['status'] = host_result['status']
       
        host_response['data'] = host_result['data']
    else:
        host_response['status'] = host_result['status']
    service_result = nodeObj._getdashboarddata(datafor="Service")
    if service_result['status'] == 200:
        service_response['status'] = service_result['status']
        service_response['data'] = service_result['data']
    else:
        service_response['status'] = service_result['status']
    tempJsonObj = { "host":host_response, "service":service_response }
    return HttpResponse(jdumps(tempJsonObj), content_type="json")

def getnodespecificdetails(request):
   # print("inside getnodespecificdetails-----> ")
    response = {}
    nodeid = request.POST['nodeid']
    mode = request.POST['mode']
    site = request.POST['selectedSite']
    ip = request.POST['ip']
    site_objs = SiteModel.objects.filter(sitename = site)
    tempJsonObj={}
    for site_obj in site_objs:
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            result = nodeObj._getSpecificNodeDetails(nodeid,mode,ip)
            if result['status'] == 200:
                response['status'] = result['status']
                response['data'] = result['data']
            else:
                response['status'] = result['status']
            tempJsonObj = {"nodedetails":response}
        except Exception as ex:
            response['error_msg'] = "getnodespecificdetails : Ex = " + str(ex)
            response['status']=400
            return HttpResponse(jdumps(response), content_type="json")
    return HttpResponse(jdumps(tempJsonObj), content_type="json")




def getchartspecificdetails(request):
    #print("inside getchartspecificdetails-----> ")
    response = {}
    site = request.GET['sitename']
    site_objs = SiteModel.objects.filter(sitename = site)
    for site_obj in site_objs:
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            result = nodeObj.layerwiseCount()
            if result['status'] == 200:
                response['status'] = result['status']
                response['data'] = result
            else:
                response['status'] = result['status']
        except Exception as ex:
            response['error_msg'] = "chartspecificdetails : Ex = " + str(ex)
            response['status']=400
            return HttpResponse(jdumps(response), content_type="json")
    return HttpResponse(jdumps(response), content_type="json")


def getoverallchartdetails(request):
    #print("inside getoverallchartdetails-----> ")
    response = {}
    site = request.GET['sitename']
    site_type=request.GET['allsite']
    if not site_type =='false' :
        hardware_critical=0
        hardware_warning=0
        hardware_ok=0
        hardware_unknown=0
        software_critical = 0
        software_warning = 0
        software_ok = 0
        software_unknown = 0
        application_critical = 0
        application_warning = 0
        application_ok = 0
        application_unknown = 0
        chartresponse={}
        try:
            site_objs = SiteModel.objects.filter(sitename = site)
            for site_obj in site_objs:
                    
                try:
                    nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
                    result = nodeObj.overallStats()
                    if result['status'] == 200:
                        res_data=result['data']
                        hardware_critical= hardware_critical+res_data['hardware'][0]
                        hardware_ok= hardware_ok+res_data['hardware'][2]
                        hardware_warning= hardware_warning+res_data['hardware'][1]
                        hardware_unknown= hardware_unknown+res_data['hardware'][3]
                        software_critical= software_critical+res_data['software'][0]
                        software_ok= software_ok+res_data['software'][2]
                        software_warning= software_warning+res_data['software'][1]
                        software_unknown= software_unknown+res_data['software'][3]
                        application_critical= application_critical+res_data['application'][0]
                        application_ok= application_ok+res_data['application'][2]
                        application_warning= application_warning+res_data['application'][1]
                        application_unknown= application_unknown+res_data['application'][3]
                    else:
                        response['status'] = result['status']
                except Exception as ex:
                    response['error_msg'] = "overallchartdetails : Ex = " + str(ex)
            chartresponse = { "hardware": { "CRITICAL": hardware_critical, "OK": hardware_ok, "WARNING": hardware_warning, "UNKNOWN": hardware_unknown }, "software": { "CRITICAL": software_critical, "OK": software_ok, "WARNING": software_warning, "UNKNOWN": software_unknown },"application":{ "CRITICAL": application_critical, "OK": application_ok, "WARNING": application_warning, "UNKNOWN": application_unknown } };
            response['site'] = site
            response['status'] = result['status']
            response['data'] = chartresponse 
        except Exception as ex:
            response['error_msg'] = "overallchartdetails : Ex = " + str(ex)
            response['data'] = chartresponse
            response['status']=400
            return HttpResponse(jdumps(response), content_type="json")
    else:
        site_objs = SiteModel.objects.filter(sitename = site)
        for site_obj in site_objs:
            try:
                nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
                result = nodeObj.overviewStats()
                if result['status'] == 200:
                    response['status'] = result['status']
                    response['data'] = result['data']
                else:
                    response['status'] = result['status']
            except Exception as ex:
                response['error_msg'] = "overallchartdetails : Ex = " + str(ex)
                response['status']=400
                return HttpResponse(jdumps(response), content_type="json")
    
    return HttpResponse(jdumps(response), content_type="json")



def getwebsocupdate(request):
    #print("inside getoverallchartdetails-----> ")
    response = {}
    response['data']={}
    site = request.GET['sitename']
    query_ip = request.GET.get('ip') or settings.WEBSOCKET_STATS_IP
    overview_res=''
    overall_res=''
    try:
        if not query_ip:
            response['error_msg'] = 'Provide ip query param or set WEBSOCKET_STATS_IP in environment'
            response['status'] = 400
            return HttpResponse(jdumps(response), content_type="json")
        site_objs = SiteModel.objects.filter(sitename = site)
        for site_obj in site_objs:
            try:
                nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
                result = nodeObj.websocketStats(ip=query_ip)
                if result['status'] == 200:
                    overview_res=result['data']['overview']
                    overall_res=result['data']['overall']
                else:
                    response['status'] = result['status']
            except Exception as ex:
                response['error_msg'] = "overallchartdetails : Ex = " + str(ex)
        response['site'] = site
        response['status'] = result['status']
        response['data']['overview'] = overview_res
        response['data']['overall'] = overall_res
    except Exception as ex:
        response['error_msg'] = "overallchartdetails : Ex = " + str(ex)
        response['data']['overview'] = overview_res
        response['data']['overall'] = overall_res
        response['status']=400
        return HttpResponse(jdumps(response), content_type="json")
    return HttpResponse(jdumps(response), content_type="json")

def savedatabase(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        #snmphost = data["snmphost"]
        user_name = data.get("user_name","NOVALUE")
        ipList = data["iplists"]
        version = data["version"]
        models = data["models"]
        securitylevel = data.get("securitylevel","NOVALUE")
        Authentication = data.get("Authentication","NOVALUE")
        Privacy = data.get("Privacy","NOVALUE")
        auth_password = data.get("auth_password","NOVALUE")
        privacy_password = data.get("privacy_password","NOVALUE")
        comm_string = data.get("comm_string","NOVALUE")
        threshold = data.get("threshold")
        modelList = []

        #snmptableModel = SnmpModel.objects.filter(ipaddress=ipList, protocol=version).first()
        snmptableModel = SnmpModel.objects.filter(ipaddress=ipList).first()
        if snmptableModel:
            snmptableModel.username = user_name
            snmptableModel.protocol = version
            snmptableModel.model = models
            snmptableModel.securitylevel = securitylevel
            snmptableModel.authenticationmethod = Authentication
            snmptableModel.privacymethod = Privacy
            snmptableModel.authenticationpassword = auth_password
            snmptableModel.privacypassword = privacy_password
            snmptableModel.communitystring = comm_string
            snmptableModel.threshold = threshold
            snmptableModel.save()
            nodes_val = "Successfully table were edited!!"
        else:
            snmptableModel = SnmpModel(
                ipaddress=ipList,
                protocol=version,
                username=user_name,
                model=models,
                securitylevel=securitylevel,
                authenticationmethod=Authentication,
                privacymethod=Privacy,
                authenticationpassword=auth_password,
                privacypassword=privacy_password,
                communitystring=comm_string,
                threshold=threshold
            )
            snmptableModel.save()
            nodes_val = "Successfully table were created!!"
        ##      fetch all    ###
        cursor = connection.cursor()
        #cursor.execute("SELECT * FROM snmp")
        addList = snmpFileCreate(cursor)
        response['data'] = addList
        ##      fetch all    ###

        response['status'] = 200
        response['data'] = nodes_val
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['data'] = "Failure in creating table!! "+str(e)
        return HttpResponse(json.dumps(response))

def deleteactions(request):
    response = {}
    snmpData = json.loads(request.POST['data'])
    parsed_json = snmpData['data']
    try:
        deleteobj = SnmpModel.objects.get(id=parsed_json["rowid"])
        deleteobj.delete()

        ##      fetch all    ###
        cursor = connection.cursor()
        delList = snmpFileCreate(cursor)
        response['data'] = delList
        ##      fetch all    ###

        response['status'] = 200
        response['msg'] = 'Successfully deleted snmp'
        response['rowid'] = parsed_json["rowid"]
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['data'] = "Failure in delete table!! "+str(e)
        return HttpResponse(json.dumps(response))


def snmpnewtable(request):
    response = {}
    #print("inside Switcheslayer==========>")
    try:
        temp_list = []
        app_obj = SnmpModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["version"] = temp.protocol
            json_obj["ipaddress"] = temp.ipaddress
            json_obj["username"] = temp.username
            json_obj["model"] = temp.model
            json_obj["comm_string"] = temp.communitystring
            json_obj["sec_level"] = temp.securitylevel
            json_obj["auth_method"] = temp.authenticationmethod
            json_obj["auth_password"] = temp.authenticationpassword
            json_obj["priv_method"] = temp.privacymethod
            json_obj["priv_password"] = temp.privacypassword
            json_obj["snmp_threshold"] = temp.threshold
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switcheslayer===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))

def snmpFileCreate(cursor): 
    ret = {"snmp" : []}
    cursor.execute("SELECT ipaddress FROM snmp")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = SnmpModel.objects.filter(ipaddress=_ip).values()
        for k,v in values[0].items():
            if v == 'NOVALUE':
                continue
            item = {}
            if k == 'threshold':
                item[k] = []
                thresholds = json.loads(v.replace("''", "\"").replace("'", "\""))
                for threshold_name, threshold_value in thresholds.items():
                    threshold_item = {}
                    threshold_item[threshold_name] = threshold_value
                    item[k].append(threshold_item)
            else:
                item[k] = v
            sub[_ip].append(item)
        ####

        i = i+1
        ret["snmp"].append(sub)
    #print(ret)
    with open("./prom-conf/snmp.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def snmpvalidation(request):
    response = {}

    try:
        data = json.loads(request.POST.get('data'))
        user_name = data.get("user_name","NOVALUE")
        ipList = data["iplists"]
        version = data["version"]
        models = data["models"]
        securitylevel = data.get("securitylevel","NOVALUE")
        Authentication = data.get("Authentication","NOVALUE")
        Privacy = data.get("Privacy","NOVALUE")
        auth_password = data.get("auth_password","NOVALUE")
        privacy_password = data.get("privacy_password","NOVALUE")
        comm_string = data.get("comm_string","NOVALUE")

        result = snmp(version = version, 
              securitylevel = securitylevel,
              username = user_name,
              community_string = comm_string,
              authenticationmethod = Authentication,
              authenticationpassword = auth_password,
              privacymethod = Privacy,
              privacypassword = privacy_password,
              ip = ipList).check()

       # print(result)
       # print(type(result))
        response['status'] = 200
        response['result'] = result
        response['data'] = "validation Failed!!"
        if result:
            response['data'] = "Successfully validated!!"
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['result'] = 'false'
        response['data'] = "Failure in validated!! "+str(e)
        return HttpResponse(json.dumps(response))


def getspecificrelation(request):
    site = request.GET["sitename"]
    pod_name = request.GET.get("podname","")
    # print("dashboard ---- view --->"+ str(layer))
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            rel_response = {}
            rel_res = nodeObj.specificrelationshipdata(title=pod_name)
            if rel_res['status'] == 200:
                rel_response['status'] = rel_res['status']
                rel_response['data'] = rel_res['data']
            else:
                rel_response['status'] = rel_res['status']
            temp_obj['site_data'] = {"relationships":rel_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['site_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def getHostOrIconnodes(request):
    site = request.GET["sitename"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            host_response = {}
            icons_response = {}
            host_res = nodeObj._visallhost(mode='hosts')
            if host_res['status'] == 200:
                host_response['status'] = host_res['status']
                host_response['data'] = host_res['data']
            else:
                host_response['status'] = host_res['status']
            icons_res = nodeObj._visicon(mode='icons')
            if icons_res['status'] == 200:
                icons_response['status'] = icons_res['status']
                icons_response['data'] = icons_res['data']
            else:
                icons_response['status'] = icons_res['status']
            temp_obj['nodes_data'] = {"hosts":host_response, "icons":icons_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['nodes_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def getHostnodes(request):
    site = request.GET["sitename"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            host_response = {}
            host_res = nodeObj._vishost(mode='hosts')
            if host_res['status'] == 200:
                host_response['status'] = host_res['status']
                host_response['data'] = host_res['data']
            else:
                host_response['status'] = host_res['status']
            temp_obj['nodes_data'] = {"hosts":host_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['nodes_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def getIconspecificnodes(request):
    site = request.GET["sitename"]
    ip = request.GET["ip"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            icons_response = {}
            icon_res = nodeObj._visspecificicon(mode='icons',ip=ip)
            if icon_res['status'] == 200:
                icons_response['status'] = icon_res['status']
                icons_response['data'] = icon_res['data']
            else:
                icons_response['status'] = icon_res['status']
            temp_obj['nodes_data'] = {"icons":icons_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['nodes_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")


def getNicConnectnodes(request):
    site = request.GET["sitename"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            nic_response = {}
            icons_response = {}
            nic_res = nodeObj._visnicconnect()
            if nic_res['status'] == 200:
                nic_response['status'] = nic_res['status']
                nic_response['data'] = nic_res['data']
            else:
                nic_response['status'] = nic_res['status']
            temp_obj['nicconnect_data'] = {"Nicconnect":nic_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['nicconnect_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def getstatusAll(request):
    site = request.GET["sitename"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = []
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            nic_response = {}
            icons_response = {}
            nic_res = nodeObj._getstatusAll()
            if nic_res['status'] == 200:
                nic_response['status'] = nic_res['status']
                nic_response['data'] = {}
                for item in nic_res['data']:
                    item = item[0]
                    ip, values = item.split(':', 1)
                    values = values.strip('[]').split(',')
                    formatted_values = {}
                    for pair in values:
                        key, value = pair.strip('{}').split(':')
                        formatted_values[key] = int(value)
                    nic_response['data'][ip] = formatted_values
            else:
                nic_response['status'] = nic_res['status']
            
            temp_obj['status_data'] = {"Status_data":nic_response}
            temp_obj["code"] = 200
        except Exception as e:
            temp_obj['status_data'] = {}
            temp_obj["code"] = 500
        data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")


def getSwitches(request):
    site = request.GET["sitename"]
    site_objs = SiteModel.objects.filter(sitename = site)
    response = { }
    data = {}
    for site_obj in site_objs:
        #temp_obj = {}
        #temp_obj['site'] = site_obj.sitename
        data['site'] = site_obj.sitename
        try:
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            switch_response = {}
            icons_response = {}
            switch_res = nodeObj._getswitchesAll()
            if switch_res['status'] == 200:
                switch_response['status'] = switch_res['status']
                switch_response['data'] = []
                for item in switch_res['data']:
                    switch_response['data'].append(item[1])
            else:
                switch_response['status'] = switch_res['status']
            
            data['response'] = switch_response
            data["code"] = 200
        except Exception as e:
            data['response'] = {}
            data["code"] = 500
        #data.append(temp_obj)
    response["status"] = 200
    response["responseData"] = data
    return HttpResponse(jdumps(response), content_type="json")

def getSwitches_old(request):
    response = {}
    site = request.GET["sitename"]
    #print("inside Switches and Servers==========>")
    try:
        temp_list = []
        app_obj = allonboardModel.objects.exclude(pathhost__icontains="Physical").exclude(pathhost__icontains="VM")
        #app_obj = newOnbModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            #print('TEMP --- > IP {}  -  pathhost {}'.format(temp.ipaddress,temp.pathhost))
            json_obj["json"] = temp.json
            json_obj["yaml"] = temp.selecthost
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====getSwitches===")
        print("getSwitches Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))
