import sys, os, json, ast
from pynag import Model
from json import *
from django.shortcuts import render,HttpResponse
from jinja2 import Environment, FileSystemLoader,meta
from lib.Jinja.CreateCfg import CreateCFG
from lib.LinkedEyeEntity.Node import Node
from dashboard.views import snmpFileCreate
from lib.LinkedEyeStruct.LinkedEyeStruct import K8S
from django.shortcuts import render, redirect
from django.template import loader
from autodiscover.models import AutoDiscoveryModel
from .models import allonboardModel
from dashboard.models import SnmpModel
from .models import allmanagementModel
from django.contrib.auth.models import User, auth, Group
from useronboard.models import Usersite
from auditlogs.models import AuditlogsModel
from addservice.models import ServerstypesModel,ServersosModel,ServerssoftwareModel, SwitcheslayersModel,SwitchesmodelModel, FirewalltypeModel, FirewallmodelModel, ServersnicModel, RoutertypeModel, RoutermodelModel
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
import subprocess
import requests
import ipaddress
from requests.auth import HTTPBasicAuth
import os
import os.path
from os import path
from django.conf import settings
from django.db import connection, transaction
from django.forms.models import model_to_dict
import datetime
import time
import yaml
import threading  # Import the threading module
from django.http import JsonResponse
from django.core.exceptions import ObjectDoesNotExist
from lib.LinkedEyeValidation import ilo, letelnet, snmp
from django.views.decorators.csrf import csrf_exempt
import smtplib
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart

template_path = "template/"
#finonb_path = "onboardOptions/"
cfg_path = "monitor/"

@login_required(login_url="/")
# @role_required(allowed_roles = ["Admin"])
@role_required(allowed_roles=["Admin", "Onboard", "ViewOnly"])
def index(request):
    return render(request, 'app/all-onboard.html')
def getfilenames(request):
    try:
        response = {}
        all_filenames = []
        path = request.GET['fileName']
        local_template_path = template_path + str(path)
        tecnologyNames = os.listdir(local_template_path)
        for tech in tecnologyNames:
            technologyFileNames = local_template_path+"/"+tech
            if get_file_extension(tech) == '.yaml':
                all_filenames.append(tech)
        json_data = all_filenames
        response['status'] = 200
        response['data'] = json_data
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400

def get_file_extension(file_name):
    return os.path.splitext(file_name)[1]

def send_onboard_notification_email(emailid, message, device_data, sitename):
    # Non-blocking: SMTP egress is unavailable in the 443-only network, so run the
    # notification send in a background daemon thread. Onboarding must never block
    # or 504 on email delivery. Delivery is best-effort and still works if SMTP
    # becomes reachable.
    import threading
    threading.Thread(target=_impl_send_onboard_notification_email, args=(emailid, message, device_data, sitename,), daemon=True).start()
    return True

def _impl_send_onboard_notification_email(emailid, message, device_data, sitename):

    try:
        smtp_server = "smtp.office365.com"
        smtp_port = 587
        # FIXED: Use settings instead of hardcoded SMTP credentials
        smtp_user = getattr(settings, 'SMTP_USER', '')
        smtp_pass = getattr(settings, 'SMTP_PASS', '')
        cc_list = ["devops@finspot.in"]  # Add more CCs if needed

        msg = MIMEMultipart("alternative")
        msg['From'] = smtp_user
        msg['To'] = emailid
        msg['Cc'] = ', '.join(cc_list)
        msg['Subject'] = sitename + " - Onboarding Device Report"

        # Construct the HTML table
        table_headers = """
            <tr>
                <th>Selecthost</th>
                <th>IPaddress</th>
                <th>SubIPaddress</th>
                <th>Environment</th>
                <th>EmailID</th>
                <th>Service Name</th>
                <th>Text Name</th>
                <th>Path Host</th>
                <th>Physical IP</th>
            </tr>
        """
        table_rows = ""
        for data in device_data:
            cleaned_selecthost = data.get('selecthost', '').split('-')[0].replace('.yaml', '')
            row = f"""
                <tr>
                    <td>{cleaned_selecthost}</td>
                    <td>{data.get('ipaddress')}</td>
                    <td>{data.get('subipaddress')}</td>
                    <td>{data.get('servertype')}</td>
                    <td>{data.get('emailid')}</td>
                    <td>{data.get('servicename')}</td>
                    <td>{data.get('textname')}</td>
                    <td>{data.get('pathhost')}</td>
                    <td>{data.get('phyipaddr')}</td>
                </tr>
            """
            table_rows += row

        html_body = f"""
        <html>
        <body>
            <p>The following devices have been {message}:</p>
            <table border="1" cellpadding="5" cellspacing="0">
                {table_headers}
                {table_rows}
            </table>
        </body>
        </html>
        """

        msg.attach(MIMEText(html_body, "html"))
        all_recipients = [emailid] + cc_list

        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, all_recipients, msg.as_string())
        server.quit()

        print(f"Email sent successfully to {emailid} with CC to {cc_list}")
        return True

    except Exception as e:
        print(f"Failed to send onboarding email: {str(e)}")
        return False

@csrf_exempt
def Saveonboard(request):
    response = {}
    ipList = []
    userobj = User.objects.get(username=request.user)

    try:
        #print(request.POST.get('data'))
        data = json.loads(request.POST.get('data'))
        #print("Saveonboard--->"+str(data))
        hostData = data["host"]
        serviceData = data["service"]
        ipList = data["iplist"]
        selecthost = data["hostname"]
        servertype = data["ApplicationName"]
        subipaddress = data["subIpAddress"]
        mainipAddress = data["ipAddress"]
        emailid = data["Email"]
        textname = data["FriendlyName"]
        pathhost = data["pathhost"]
        phyipaddr = data["physicalIP"]
        mgmntData = data["ilomgnt"]
        idracData = data["idracmgnt"]
        nodemgmtData = data["nodemgmt"]
        winmgmtData = data["winmgmt"]
        nginxmgmtData = data["ngnixmgmt"]
        sitename = data["sitename"]
        isEdit = data["isedit"]
        modelList = []
        failureList = []
        ipKey = "COMMON_IPADDRESS"
        monitoringPath = 'DIRECT/' # change in future in case gateway logic added
        index = 0
        ip = ipList

        index = index + 1
        for ip in ipList:
            tempHostName = ip
            tempStr = "\'"+ipKey+"\':\'"+ip+"\'"
            for key, val in hostData.items():
                tempStr += ','
                if key == "COMMON_HOSTNAME":
                    tempStr += "\'"+key+"\':\'"+tempHostName+"\'"
                else:
                    if isinstance(val, list):  # Check if val is a list
                        tempStr += "\'"+key+"\':" + json.dumps(val)  # Convert the list to a JSON-formatted string
                    else:
                        tempStr += "\'"+key+"\':\'"+val+"\'"

            #print("tempStr--tempStr---->" + str(tempStr))
            cfg_file_name = ip + "_linkedeye_" + hostData["HOST_TEMPLATE"].replace(".yaml", ".yaml")
            template_location = template_path + monitoringPath + hostData["HOST_TEMPLATE"].replace("_", "/")
            obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
            output = obj_createcfg.createCFGFile()
            
            # Check if an entry with the same IP address already exists in allonboardModel
            existing_entry = allonboardModel.objects.filter(ipaddress=ip).first()

            if existing_entry:
                # Update in place -- unconditionally, not just when
                # textname/emailid/selecthost changed. That narrow check used
                # to silently drop edits to every other field (e.g.
                # subipaddress, phyipaddr); re-assigning identical values is
                # a harmless no-op when nothing actually changed.
                existing_entry.selecthost = selecthost
                existing_entry.servertype = servertype
                existing_entry.subipaddress = subipaddress
                existing_entry.emailid = emailid
                existing_entry.servicename = serviceData
                existing_entry.json = json.dumps(hostData)
                existing_entry.textname = textname
                existing_entry.pathhost = pathhost
                existing_entry.hostname = ipList
                existing_entry.phyipaddr = phyipaddr
                existing_entry.mainipaddress = mainipAddress
                existing_entry.save()
            else:
                onboardHostModel = allonboardModel(
                    hostname=tempHostName,
                    ipaddress=ip,
                    servicename="",
                    selecthost=selecthost,
                    subipaddress=subipaddress,
                    servertype=servertype,
                    emailid=emailid,
                    textname=textname,
                    pathhost=pathhost,
                    phyipaddr=phyipaddr,
                    mainipaddress=mainipAddress,
                    json=json.dumps(hostData),
                )
                modelList.append(onboardHostModel)

        json_output = []
        for ip in ipList:
            yaml_file_path = os.path.join(cfg_path, ip + "_linkedeye_" + hostData["HOST_TEMPLATE"].replace(".yaml", ".yaml"))
            with open(yaml_file_path, "r") as yaml_file:
                yaml_content = yaml.safe_load(yaml_file)
                json_output += yaml_content
        #print("json_output--json_output---->"+str(json_output))

        if len(failureList) == 0:
            allonboardModel.objects.bulk_create(modelList)
            cursor = connection.cursor()
            addList = pingFileCreate(cursor)

            for mgmt in mgmntData:
                try:
                    for ip in mgmt["selectilo"]:
                        # allmanagementModel.save() (models.py) already updates the
                        # existing (ipaddress, prototype) row in place if one exists --
                        # no need to delete it first.
                        onboard_model = allonboardModel.objects.get(ipaddress= ip).id
                        obj = allmanagementModel(ip_id=onboard_model, ipaddress=ip, prototype=mgmt["prototype"], username=mgmt["username"], password=mgmt["password"], iloip=mgmt["iloip"], port=mgmt["port"], threshold=mgmt["threshold"])
                        obj.save()
                        cursor = connection.cursor()
                        addiloList = iloFileCreate(cursor)
                except Exception as e:
                    response['status'] = 400
                    response['data'] = f"Failure in Management-ILO table for ip {ip}: {str(e)}"
                    return HttpResponse(json.dumps(response))
            for idrac in idracData:
                try:
                    for ip in idrac["selectilo"]:
                        onboard_model = allonboardModel.objects.get(ipaddress=ip).id
                        obj = allmanagementModel(ip_id=onboard_model, ipaddress=ip, prototype=idrac["prototype"], username=idrac["username"], password=idrac["password"], iloip=idrac["iloip"], port=idrac["port"], threshold=idrac["threshold"])
                        obj.save()
                        cursor = connection.cursor()
                        addidracList = idracFileCreate(cursor)
                except Exception as e:
                    response['status'] = 400
                    response['data'] = f"Failure in Management-IDRAC table for ip {ip}: {str(e)}"
                    return HttpResponse(json.dumps(response))
            for nodesmgmt in nodemgmtData:
                try:
                    for ip in nodesmgmt["selectilo"]:
                        ip_id = allonboardModel.objects.get(ipaddress=ip).id
                        obj = allmanagementModel(ip_id=ip_id, ipaddress=ip, prototype=nodesmgmt["prototype"], username=nodesmgmt["username"], password=nodesmgmt["password"], iloip=nodesmgmt["iloip"], port=nodesmgmt["port"], threshold=nodesmgmt["threshold"])
                        obj.save()
                        cursor = connection.cursor()
                        addnexpoList = nexpoFileCreate(cursor)
                except Exception as e:
                    response['status'] = 400
                    response['data'] = f"Failure in Management-NODE EXPO table for ip {ip}: {str(e)}"
                    return HttpResponse(json.dumps(response))
            for winsmgmt in winmgmtData:
                try:
                    for ip in winsmgmt["selectilo"]:
                        ip_id = allonboardModel.objects.get(ipaddress=ip).id
                        obj = allmanagementModel(ip_id=ip_id, ipaddress=ip, prototype=winsmgmt["prototype"], username=winsmgmt["username"], password=winsmgmt["password"], iloip=winsmgmt["iloip"], port=winsmgmt["port"], threshold=winsmgmt["threshold"])
                        obj.save()
                        cursor = connection.cursor()
                        addwexpoList = wexpoFileCreate(cursor)
                except Exception as e:
                    response['status'] = 400
                    response['data'] = f"Failure in Management-Window Expo table for ip {ip}: {str(e)}"
                    return HttpResponse(json.dumps(response))
            for nginxData in nginxmgmtData:
                try:
                    for ip in nginxData["selectilo"]:
                        onboard_model = allonboardModel.objects.get(ipaddress=ip).id
                        obj = allmanagementModel(ip_id=onboard_model, ipaddress=ip, prototype=nginxData["prototype"], username=nginxData["username"], password=nginxData["password"], iloip=nginxData["iloip"], port=nginxData["port"], threshold=nginxData["threshold"])
                        obj.save()
                        cursor = connection.cursor()
                        addnginxList = NginxFileCreate(cursor)
                except Exception as e:
                    response['status'] = 400
                    response['data'] = f"Failure in Management-Nginx Expo table for ip {ip}: {str(e)}"
                    return HttpResponse(json.dumps(response))

            
            out = createnodes(json_output)  # Pass the json_output list directly
            device_info_list = []
            for ip in ipList:
                device_info_list.append({
                    'selecthost': selecthost,
                    'ipaddress': ip,
                    'subipaddress': subipaddress,
                    'servertype': servertype,
                    'emailid': emailid,
                    'servicename': serviceData,
                    'textname': textname,
                    'pathhost': pathhost,
                    'phyipaddr': phyipaddr
                })
            if out['status'] == 200:
                response['status'] = out['status']
                response['data'] = "Successfully nodes were created!!"
                log = AuditlogsModel(username=userobj, action='Onboard Device', status='Success', message=f'IP: {ipList} onboarded successfully')
                log.save()
                send_onboard_notification_email(emailid, "Onboarded Successfully", device_info_list, sitename)
            else:
                response['status'] = out['status']
                response['data'] = out['data']
                send_onboard_notification_email(emailid, "Onboarded Successfully with Error", device_info_list, sitename)
            return HttpResponse(json.dumps(response))

        response['status'] = 200
        response['data'] = "Successfully created table!"
        return HttpResponse(json.dumps(response))

    except Exception as e:
        response['status'] = 400
        response['data'] = "Failure in creating table: " + str(e)
        if "allonboardModel" in str(e):
            response['data'] += " in allonboardModel table"
        elif "allmanagementModel" in str(e):
            response['data'] += " in allmanagementModel table"
        log = AuditlogsModel(username=userobj, action='Onboard Device', status='Failure', message=f'error: ' + str(e))
        log.save()
        device_info_list = [{
            'selecthost': selecthost,
            'ipaddress': ",".join(ipList),
            'subipaddress': subipaddress,
            'servertype': servertype,
            'emailid': emailid,
            'servicename': serviceData,
            'textname': textname,
            'pathhost': pathhost,
            'phyipaddr': phyipaddr
        }]
        send_onboard_notification_email(emailid, "Onboarded Failed", device_info_list, sitename)
        return HttpResponse(json.dumps(response))

def createnodes(json_output):
    client = Node()
    response = {}
    try:
        if not len(json_output) == 0:
            for hostObj in json_output:
                hostDic = {}
                for key in hostObj.keys():
                    if key.startswith("_NEO4j_"):
                        hostDic[key] = hostObj[key]
                if not hostDic == {}:
                    #client.execute("MATCH (a {  hostIp:'"+hostDic["_NEO4j_address"]+"' } ) DETACH DELETE a")
                    _struct = K8S("").convertor('nagios',hostDic)
                    # FIXED: json.loads instead of ast.literal_eval
                    createResponse = client.create(json.loads(_struct))


            for serviceObj in json_output:
                serviceDic = {}
                for key in serviceObj.keys():
                    if key.startswith("_NEO4j_"):
                        serviceDic[key] = serviceObj[key]
                if not serviceDic == {} and (not serviceDic["_NEO4j_parent"] == "\"\""or not serviceDic["_NEO4j_title"]):
                    if serviceObj.get("_NEO4J_RELATIONSHIP_RQD", "Yes") == 'No':
                        response['status'] = 200
                        response['data'] = "No relationship !!"
                    else:
                        createResponse = client.addRel(serviceDic["_NEO4j_parent"],serviceDic["_NEO4j_title"])
                                                
                        if createResponse['status'] == 200:
                            response['status'] = createResponse['status']
                            response['data'] = "Successfully nodes were created!!"
                        else:
                            response['status'] = createResponse['status']
                            response['data'] = "Failure in relationship creation {} - {}".format(serviceDic["_NEO4j_parent"],serviceDic["_NEO4j_title"])

                    #else:
                    #    response['status'] = createResponse['status'] 
                    #    response['data'] = "Failure in host creation"
        return response
    except Exception as e:
        # print(str(e))
        response['status'] = 400
        response['data'] = "Failure in node creation!! "+str(e)
        return response

def deletehost(request):
    response = {}
    ipList = ""
    try:
        ipaddress = json.loads(request.POST["ipaddress"])
        subipaddress = json.loads(request.POST["subipaddress"])
        hostname = json.loads(request.POST["hostname"])
        
        # Check if subipaddress is an empty string or an empty list and treat it as empty
        if subipaddress == "" or (isinstance(subipaddress, list) and len(subipaddress) == 0):
            subipaddress = []
        
        all_ip = ipaddress + subipaddress  # Combine both addresses into a single list
        all_ip = [ip for ip in all_ip if isinstance(ip, str) and ip.strip() != ""]
        ipList = ", ".join(all_ip)

        for ip in all_ip:
            if ip:
                allonboardModel.objects.filter(ipaddress=ip).delete()
                SnmpModel.objects.filter(ipaddress=ip).delete()
                cursor = connection.cursor()
                addiloList = iloFileCreate(cursor)
                addidracList = idracFileCreate(cursor)
                addnexpoList = nexpoFileCreate(cursor)
                addwexpoList = wexpoFileCreate(cursor)
                addnginxList = NginxFileCreate(cursor)
                addList = pingFileCreate(cursor)
                client = Node()
                
                # Check if the node is available before deleting
                if client._check(ip, key='hostIp', resOut=True):
                    client.execute("MATCH (a { hostIp:'" + ip + "' }) DETACH DELETE a")
                else:
                    # Log that host was already deleted
                    log = AuditlogsModel(username=request.user.username, action='Delete Device', status='Warning', message=f'IP: {ip} already deleted')
                    log.save()
                    # Handle the case when the node is not available
                    response['status'] = 200 
                    response['data'] = "Hosts already deleted"
                    return HttpResponse(json.dumps(response))
        
        # Log successful deletion
        log = AuditlogsModel(username=request.user.username, action='Delete Device', status='Success', message=f'IP(s): {ipList} deleted successfully')
        log.save()
        response['status'] = 200 
        response['data'] = "Hosts deleted successfully"
        return HttpResponse(json.dumps(response))
    except Exception as e:
        # Log failure
        log = AuditlogsModel(username=request.user.username, action='Delete Device', status='Failed', message=f'Error deleting IP(s): {ipList}. Reason: {str(e)}')
        log.save()
        response['status'] = 400 
        response['data'] = "Not able to delete hosts"
        return HttpResponse(json.dumps(response))

def getserverstype(request):
    response = {}
    #print("inside getserverstype==========>")
    try:
        temp_list = []
        app_obj = ServerstypesModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["types"] = temp.types
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====getserverstype===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Serversos(request):
    response = {}
    #print("inside ServersosModel==========>")
    try:
        temp_list = []
        app_obj = ServersosModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["os"] = temp.os
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====ServersosModel===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Serverssoftware(request):
    response = {}
    #print("inside Serverssoftware==========>")
    try:
        temp_list = []
        app_obj = ServerssoftwareModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["software"] = temp.software
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Serverssoftware===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Switcheslayer(request):
    response = {}
    #print("inside Switcheslayer==========>")
    try:
        temp_list = []
        app_obj = SwitcheslayersModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["layers"] = temp.layers
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switcheslayer===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Switchesmodel(request):
    response = {}
    #print("inside Switchesmodel==========>")
    try:
        temp_list = []
        app_obj = SwitchesmodelModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["model"] = temp.model
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switchesmodel===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Serversnic(request):
    response = {}
    #print("inside Switchesmodel==========>")
    try:
        temp_list = []
        app_obj = ServersnicModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["nic"] = temp.nic
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switchesmodel===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Firewalltype(request):
    response = {}
    #print("inside Firewalltype==========>")
    try:
        temp_list = []
        app_obj = FirewalltypeModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["types"] = temp.types
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Firewalltype===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Firewallmodel(request):
    response = {}
    #print("inside Firewalltype==========>")
    try:
        temp_list = []
        app_obj = FirewallmodelModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["model"] = temp.model
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Firewalltype===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Routermodel(request):
    response = {}
    #print("inside Routermodel==========>")
    try:
        temp_list = []
        app_obj = RoutermodelModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["model"] = temp.model
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Routermodel===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def Routertypes(request):
    response = {}
    #print("inside Routertypes==========>")
    try:
        temp_list = []
        app_obj = RoutertypeModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["types"] = temp.types
            json_obj["model"] = temp.types
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Routertypes===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def getiplist(request):
    response = {}
    try:
        temp_list = []
        ip_list_obj = AutoDiscoveryModel.objects.all()
        for p in ip_list_obj:
            json_obj = {}
            json_obj["ip"] = p.ipaddress
            json_obj["os"] = p.ostype
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        return HttpResponse(json.dumps(response))


def getfilecontentdata(request):
    response = {}
    try:
        file_name = request.POST['filename']
        file_path = file_name.replace("_", "/")
        fine_name_array = file_name.split("_")
        jfile_name = fine_name_array[len(fine_name_array) - 1]
        path = os.path.dirname(os.path.abspath(template_path+file_path+""))
        template_env = Environment(autoescape=False,loader=FileSystemLoader(os.path.join(path)),trim_blocks=False)
        template_source = template_env.loader.get_source(template_env, jfile_name) #replace template_filename with your template file relative to current file
        parsed_content = template_env.parse(template_source)
        variables= meta.find_undeclared_variables(parsed_content)
        content = []
        for data in variables:
            content.append(data)
        sorted_content = sorted(content)
        str_data = json.dumps(sorted_content)
        parsed_json = json.loads(str_data)
        response['status'] = 200
        response['data'] = parsed_json
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400

"""def deletehost(request):
    response = {}
    try:
        ipaddress = request.POST["ipaddress"]
        hostname = request.POST["hostname"]
        subipaddress = request.POST["subipaddress"]
        allonboardModel.objects.filter(ipaddress=ipaddress).delete()
        SnmpModel.objects.filter(ipaddress=ipaddress).delete()
        cursor = connection.cursor()
        addiloList = iloFileCreate(cursor)
        addidracList = idracFileCreate(cursor)
        addnexpoList = nexpoFileCreate(cursor)
        Model.cfg_file = template_path+"../nagios/nagios.cfg" # commit server uncomment this line(all place)
        #Model.cfg_file = "c:/nagios/nagios.cfg" # commit server comment this line(all place)
        cfgHost = Model.Host.objects.filter(address=ipaddress)
        for service in Model.Service.objects.filter(_NEO4j_address=ipaddress):
            if (path.exists("monitor/"+os.path.basename(service["meta"]["filename"])) == True):
                os.remove("monitor/"+os.path.basename(service["meta"]["filename"]))
        for temphostObj in cfgHost:
            if (path.exists("monitor/"+os.path.basename(temphostObj["meta"]["filename"])) == True):
                os.remove("monitor/"+os.path.basename(temphostObj["meta"]["filename"]))
       
        client = Node()
        client.execute("MATCH (a {  hostIp:'"+ipaddress+"' } ) DETACH DELETE a")
        #client.execute("MATCH (a:Host {  host:'"+hostname+"' } ) DETACH DELETE a")
        #client.execute("MATCH (a:HostMS {  parent:'"+hostname+"' } ) DETACH DELETE a")
        #client.execute("MATCH (a:Service {  parent:'"+hostname+"' } ) DETACH DELETE a")
        #client.execute("MATCH (a:ServiceMS) WHERE a.parent CONTAINS '"+hostname+":' DETACH DELETE a")
        #subprocess.run(['sh', '../script/restartdocker.sh', ''], shell=False, timeout=1800) 
        client = Node()
        client.execute("MATCH (a {  hostIp:'"+ipaddress+"' } ) DETACH DELETE a")
        response['status'] = 200 
        response['data'] = "Host deleted successfully"
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400 
        response['data'] = "Not able to delete host"
        return HttpResponse(json.dumps(response))"""

def gethostservicedata(request):
    response = {}
    try:
        Model.cfg_file = template_path+"../nagios/nagios.cfg"
        #Model.cfg_file = "c:/nagios/nagios.cfg" # command on upload by server
        all_hosts = Model.Host.objects.all
        tempList = []
        for hostObj in all_hosts:
           # print("gethostservicedata ----->" +str(hostObj))
            hostDic = {}
            hostDic["host_name"] = hostObj["host_name"]
            hostDic["address"] = hostObj["address"]
            hostDic["contact_email"] = hostObj["_NEO4j_contactEmail"]
            hostDic["application"] = hostObj["_NEO4j_application"]
            hostDic["automation"] = "No" if hostObj["_NEO4j_Automation"] == ''else hostObj["_NEO4j_Automation"] 
            hostDic["layer"] = hostObj["_NEO4j_layer"]
            hostDic["friend"] = hostObj["_NEO4j_FrdlyName"]
            hostDic["server_type"] = hostObj["_NEO4j_sertype"]
            ###
            # onboard card more details can be added here
            ###
            tempList.append(hostDic)
        response['status'] = 200
        response['data'] = tempList
        #print("gethostservicedata ----->" +str(tempList))
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        return HttpResponse(json.dumps(response))

def edithostdetails(request):
    #print('edithostdetails-->'+str(request))
    response = {}
    try:
        temp_list = []
        ipaddress = request.GET['ipaddress']
        #print('edithostd-->'+str(ipaddress))
        editData = allonboardModel.objects.filter(ipaddress=ipaddress)
        memntData = allmanagementModel.objects.filter(ipaddress=ipaddress)
        memntnode = allmanagementModel.objects.filter(ipaddress=ipaddress)
        cursor = connection.cursor()
        addList = pingFileCreate(cursor)
        for mgmts in memntData:
            ip_id = allonboardModel.objects.get(ipaddress=ipaddress).id
            obj = allmanagementModel(ip_id=ip_id, ipaddress=ipaddress, prototype=mgmts.prototype, username=mgmts.username, password=mgmts.password, iloip=mgmts.iloip, port=mgmts.port)
            #print("===================="+str(obj))
            #obj.save()
            if mgmts.prototype == 'ilo':
                cursor = connection.cursor()
                addiloList = iloFileCreate(cursor)
            if mgmts.prototype == 'idrac':
                cursor = connection.cursor()
                addidracList = idracFileCreate(cursor)
        for mgmtexpo in memntnode:
            ip_id = allonboardModel.objects.get(ipaddress=ipaddress).id
            obj = allmanagementModel(ip_id=ip_id, ipaddress=ipaddress, prototype=mgmtexpo.prototype, username=mgmtexpo.username, password=mgmtexpo.password, iloip=mgmtexpo.iloip, port=mgmtexpo.port)
            if mgmtexpo.prototype == 'Node Expo':
                cursor = connection.cursor()
                addnexpoList = nexpoFileCreate(cursor)
            if mgmtexpo.prototype == 'Window Expo':
                cursor = connection.cursor()
                addwexpoList = wexpoFileCreate(cursor)
            if mgmtexpo.prototype == 'Nginx Expo':
                cursor = connection.cursor()
                addnginxList = NginxFileCreate(cursor)
        #print('edithost-->'+str(editData))
        for p in editData:
            json_obj = {}
            json_obj["json"] = p.json
            json_obj["selecthost"] = p.selecthost
            json_obj["hostname"] = p.hostname
            json_obj["ipaddress"] = p.ipaddress
            json_obj["servertype"] = p.servertype
            json_obj["emailid"] = p.emailid
            json_obj["servicename"] = p.servicename
            json_obj["pathhost"] = p.pathhost
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        #print('edithostdetails---->'+str(temp_list))
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong in Edit'+str(e)
        return HttpResponse(json.dumps(response))

def newonbtable(request):
    response = {}
    #print("inside Switcheslayer==========>")
    try:
        temp_list = []
        app_obj = allonboardModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["selecthost"] = temp.selecthost
            json_obj["ipaddress"] = temp.ipaddress
            json_obj["subipaddress"] = temp.subipaddress
            json_obj["servertype"] = temp.servertype
            json_obj["emailid"] = temp.emailid
            json_obj["servicename"] = temp.servicename
            json_obj["json"] = temp.json
            json_obj["textname"] = temp.textname
            json_obj["pathhost"] = temp.pathhost
            json_obj["hostname"] = temp.hostname
            json_obj["physical_ip"] = temp.phyipaddr
            json_obj["mainipaddress"] = temp.mainipaddress
            temp_list.append(json_obj)
            ##      fetch all    ###
        cursor = connection.cursor()
        #cursor.execute("SELECT * FROM snmp")
        addList = pingFileCreate(cursor)
        #response['data'] = addList
        ##      fetch all    ###
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switcheslayer===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))

def mgmtdeletehost(request):
    response = {}
    try:
        ipaddress = request.POST["ipaddress"]
        prototypes = request.POST["prototype"]
        allmanagementModel.objects.filter(ipaddress=ipaddress, prototype=prototypes).delete()
        cursor = connection.cursor()
        addiloList = iloFileCreate(cursor)
        addidracList = idracFileCreate(cursor)
        addnexpoList = nexpoFileCreate(cursor)
        addwexpoList = wexpoFileCreate(cursor)
        addnginxList = NginxFileCreate(cursor)
        response['status'] = 200 
        response['data'] = str(prototypes)+" deleted successfully"
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400 
        response['data'] = "Not able to delete management"+str(e)
        return HttpResponse(json.dumps(response))

def getmgmntdata(request):
    response = {}
    try:
        temp_list = []
        req_ip = request.GET['ipaddress']
        temp = allmanagementModel.objects.filter(ipaddress=req_ip)
        if temp:
            for obj in temp:
                json_obj = {}
                json_obj["id"] = obj.id
                json_obj["prototype"] = obj.prototype
                json_obj["iloip"] = obj.iloip
                json_obj["username"] = obj.username
                json_obj["password"] = obj.password
                json_obj["port"] = obj.port
                json_obj["ip_id"] = obj.ip_id
                json_obj["ipaddress"] = obj.ipaddress
                json_obj["threshold"] = obj.threshold
                temp_list.append(json_obj)
            response['status'] = 200
            response['data'] = temp_list
            return HttpResponse(json.dumps(response))
        else:
            response['status'] = 400
            response['msg'] = 'No matching records found'
            return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))

def snmpdatatable(request):
    response = {}
    #print("inside Switcheslayer==========>")
    try:
        temp_list = []
        req_ip = request.GET['ipaddress']
        app_obj = SnmpModel.objects.filter(ipaddress=req_ip)
        if app_obj:
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
        else:
            response['status'] = 400
            response['msg'] = 'No matching records found'
            return HttpResponse(json.dumps(response))
    except Exception as e:
        #print("===Exception====Switcheslayer===")
        #print("getserverstype Exception --->"+str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))

def getallmgmntdata(request):
    response = {}
    try:
        temp_list = []
        #req_ip = request.GET['ipaddress']
        temp = allmanagementModel.objects.all()
        if temp:
            for obj in temp:
                json_obj = {}
                json_obj["id"] = obj.id
                json_obj["prototype"] = obj.prototype
                json_obj["iloip"] = obj.iloip
                json_obj["username"] = obj.username
                json_obj["password"] = obj.password
                json_obj["port"] = obj.port
                json_obj["ip_id"] = obj.ip_id
                json_obj["ipaddress"] = obj.ipaddress
                json_obj["threshold"] = obj.threshold
                temp_list.append(json_obj)
            response['status'] = 200
            response['data'] = temp_list
            return HttpResponse(json.dumps(response))
        else:
            response['status'] = 400
            response['msg'] = 'No matching records found'
            return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))

def idracvalidation(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        prototype = data["prototype"]
        ip = data["ip"]
        port = data["port"]

        result = letelnet(ip=ip , port=port).check()

        #print(result)
        #print(type(result))
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

def ilovalidation(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        prototype = data["prototype"]
        username = data["username"]
        ips = data["ip"]
        ip = data["iloip"]
        password = data["password"]

        result = ilo(ip=ip , username=username,
                          password=password).check()

        #print(result)
        #print(type(result))
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

def nodeexpvalidation(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        prototype = data["prototype"]
        ip = data["ip"]
        port = data["port"]

        result = letelnet(ip=ip , port=port).check()

        #print(result)
        #print(type(result))
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

def ngnixexpvalidation(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        prototype = data["prototype"]
        ip = data["ip"]
        port = data["port"]

        result = letelnet(ip=ip , port=port).check()

        #print(result)
        #print(type(result))
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

def winexpvalidation(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        prototype = data["prototype"]
        ip = data["ip"]
        port = data["port"]

        result = letelnet(ip=ip , port=port).check()

        #print(result)
        #print(type(result))
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

def pingFileCreate(cursor): 
    ret = {"ping" : []}
    cursor.execute("SELECT ipaddress FROM newonb")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        i = 0
        _ip=ip[i]
        #print(_ip)
        ####
        values = allonboardModel.objects.filter(ipaddress=_ip).values()
        i = i+1
        ret["ping"].append(_ip)
    #print(ret)
    with open("./prom-conf/ping.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def iloFileCreate(cursor): 
    ret = {"ilo" : []}
    cursor.execute("SELECT ipaddress FROM management where prototype='ilo'")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='ilo').values()
        for k,v in values[0].items():
            if v == '':
                continue
            item = {}
            item[k] = v
            sub[_ip].append(item)
        ####

        i = i+1
        ret["ilo"].append(sub)
    #print(ret)
    with open("./prom-conf/ilo.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def idracFileCreate(cursor): 
    ret = {"idrac" : []}
    cursor.execute("SELECT ipaddress FROM management where prototype='idrac'")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='idrac').values()
        for k,v in values[0].items():
            if v == '':
                continue
            item = {}
            item[k] = v
            sub[_ip].append(item)
        ####

        i = i+1
        ret["idrac"].append(sub)
    #print(ret)
    with open("./prom-conf/idrac.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def nexpoFileCreate(cursor): 
    ret = {"node_exporter" : []}
    cursor.execute("SELECT ipaddress FROM management where prototype = 'Node Expo'")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Node Expo').values()
        for k, v in values[0].items():
            if v == '':
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

        ret["node_exporter"].append(sub)

    with open("./prom-conf/node.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def wexpoFileCreate(cursor): 
    ret = {"windows_exporter" : []}
    cursor.execute("SELECT ipaddress FROM management where prototype = 'Window Expo'")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Window Expo').values()
        for k, v in values[0].items():
            if v == '':
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

        ret["windows_exporter"].append(sub)

    #if not ret["windows_exporter"] == []:
    with open("./prom-conf/windows.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def NginxFileCreate(cursor): 
    ret = {"nginx_exporter" : []}
    cursor.execute("SELECT ipaddress FROM management where prototype='Nginx Expo'")
    IPs = cursor.fetchall()
    #print(IPs)
    for ip in IPs:
        sub = {}
        i = 0
        _ip=ip[i]
        sub[_ip] = []

        ####
        values = allmanagementModel.objects.filter(ipaddress=_ip, prototype='Nginx Expo').values()
        for k,v in values[0].items():
            if v == '':
                continue
            item = {}
            item[k] = v
            sub[_ip].append(item)
        ####

        i = i+1
        ret["nginx_exporter"].append(sub)
    #print(ret)
    with open("./prom-conf/nginx.yaml", "w+") as f:
        yaml.safe_dump(ret, f, default_flow_style=False)

def process_allmanagement_entries(allmanagement_entries):
    threshold_default = {
        'disk_w': 65, 'disk_c': 70, 'disk_t': 600, 'cpu_w': 65, 'cpu_c': 70, 'cpu_t': 10, 'mem_w': 65, 'mem_c': 70, 'mem_t': 10,
        'load_w': 0.6, 'load_c': 0.8, 'load_t': 10, 'uptime_w': 120, 'uptime_c': 150, 'uptime_t': 72000, 'login_w': 2, 'login_c': 5, 'login_t': 10
    }

    validated_ip_addresses = []
    non_validated_ip_addresses = []
    offboarded_ips = []
    onboard_model_ids = {}  # Cache onboard IPs

    for entry_data in allmanagement_entries:
        nodesmgmt = entry_data.get('entry', {})
        ip = nodesmgmt.get('ipaddress')
        prototype = nodesmgmt.get('prototype', '')

        if not ip:
            continue
        if ip in offboarded_ips:
            continue

        port = nodesmgmt.get('port', '')
        username = nodesmgmt.get('username', '')
        password = nodesmgmt.get('password', '')
        iloip = nodesmgmt.get('iloip', '')

        # ======== Validation Based on Prototype ========
        validation_result = None
        if prototype in ["idrac", "Node Expo", "Window Expo", "Nginx Expo"]:
            validation_result = letelnet(ip=ip, port=port).check()
        elif prototype == "ilo":
            validation_result = ilo(ip=iloip, username=username, password=password).check()

        is_reachable = isinstance(validation_result, bool) and validation_result

        # ======== STRICT GATE: validation failure offboards the device ========
        # A failed reachability check (node_exporter port, iLO login, ...)
        # removes the device completely: MySQL host row (management rows go
        # with it via the FK cascade) and the Neo4j topology node. The device
        # is reported back in non_validated so the onboarding popup lists it.
        # NOTE: a transient network blip during onboarding therefore drops the
        # device; re-run the sheet to onboard it again once reachable.
        if not is_reachable:
            non_validated_ip_addresses.append({'ip': ip, 'prototype': prototype})
            offboarded_ips.append(ip)

            allonboardModel.objects.filter(ipaddress=ip).delete()

            # is_valid_ip guard keeps a sheet value from being injected into
            # Cypher; a Neo4j failure must not abort the rest of the batch.
            try:
                if is_valid_ip(ip):
                    client = Node()
                    if client._check(ip, key='hostIp', resOut=True):
                        client.execute(f"MATCH (a {{ hostIp:'{ip}' }}) DETACH DELETE a")
            except Exception as _neo_exc:
                print(f"[LE] Neo4j offboard failed for {ip}: {_neo_exc}")
            print(f"[LE] Offboarded {ip} after {prototype} validation failure")
            continue

        # ======== Check if already present ========
        # (plain exists-check + create, NOT update_or_create: routing an
        # existing row through models.py's save() crashes on dict thresholds)
        if allmanagementModel.objects.filter(ipaddress=ip, prototype=prototype).exists():
            validated_ip_addresses.append({'ip': ip, 'prototype': prototype})
            continue

        try:
            ip_id = allonboardModel.objects.get(ipaddress=ip).id
        except ObjectDoesNotExist:
            print(f"Skipping IP {ip}: not found in allonboardModel")
            non_validated_ip_addresses.append({'ip': ip, 'prototype': prototype, 'error': 'Not in allonboardModel'})
            continue

        # ======== Threshold Handling for Specific Prototypes ========
        threshold_data = {}
        if prototype in ["Node Expo", "Window Expo"]:
            threshold_data = nodesmgmt.get('threshold', {}) or threshold_default

        # ======== Save Management Entry ========
        allmanagementModel.objects.create(
            ip_id=ip_id,
            ipaddress=ip,
            prototype=prototype,
            username=username,
            password=password,
            iloip=iloip,
            port=port,
            threshold=threshold_data
        )

        validated_ip_addresses.append({'ip': ip, 'prototype': prototype})

    # ======== File Generation (Run Once) ========
    cursor = connection.cursor()
    iloFileCreate(cursor)
    idracFileCreate(cursor)
    nexpoFileCreate(cursor)
    wexpoFileCreate(cursor)
    NginxFileCreate(cursor)

    return {
        'validated': validated_ip_addresses,
        'non_validated': non_validated_ip_addresses
    }

def create_nodes_async(json_output):
    try:
        # Call your node creation function
        out = createnodes(json_output)  # Modify this as needed
        # You can optionally perform any post-processing here

    except Exception as e:
        # Handle exceptions if necessary
        print(f"Error creating nodes: {e}")

def is_valid_ip(ip):
    try:
        ipaddress.ip_address(ip)
        return True
    except ValueError:
        return False

def save_data_to_database(request):
    monitoringPath = 'DIRECT/'  # Change in the future if gateway logic is added
    ipKey = "COMMON_IPADDRESS"
    valid_ip_addresses = []  # List to store valid IP addresses
    invalid_ip_addresses = []  # List to store invalid IP addresses
    non_validated_snmp_ipaddresses = []
    already_onboarded_ip_addresses = []
    #offboarded_ips = []
    sitename = ''
    default_threshold={'uptime_w': 90, 'uptime_c': 120, 'uptime_t': 72000, 'temp_w': 70, 'temp_c': 80,
                        'mem_w': 70, 'mem_c': 80, 'mem_t': 10, 'cpu_w': 70, 'cpu_c': 80, 'cpu_t': 10}

    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid request method'})

    try:
        data = json.loads(request.body)
        if not data:
            return JsonResponse({'status': 'error', 'message': 'Contents are empty'})
        
        devices_map, mgmt_map, snmp_map = {}, {}, {}
        for entry in data:
            ip = entry.get('ipaddress')
            if not ip: continue
            sitename = entry.get('sitename', '')
            if 'selecthost' in entry:
                devices_map[ip] = entry
            elif 'prototype' in entry:
                mgmt_map.setdefault(ip, []).append(entry)
            elif 'version' in entry:
                snmp_map[ip] = entry

        all_ip_addresses = set(devices_map) | set(mgmt_map) | set(snmp_map)

        for ip in all_ip_addresses:
            entry = devices_map.get(ip)
            if not entry: continue

            subip_string = entry.get('subipaddress', '')
            sub_ip_addresses = [ip.strip() for ip in subip_string.strip('[]').split(',') if is_valid_ip(ip.strip())]
            ip_and_subip = [ip] + sub_ip_addresses

            for iplist in ip_and_subip:
                if not is_valid_ip(iplist):
                    continue

                valid_ip_addresses.append(iplist)
                existing_entry = allonboardModel.objects.filter(ipaddress=iplist).first()

                if 'pathhost' not in entry:
                    invalid_ip_addresses.append(iplist)
                    continue

                pathhost = entry['pathhost'].split()[0]
                selecthost = entry['selecthost']
                formatted_selecthost = f"{selecthost}-{pathhost}.yaml"
                template_location = os.path.join(template_path, monitoringPath, formatted_selecthost.replace("_", "/"))

                if not os.path.isfile(template_location):
                    invalid_ip_addresses.append(f"{iplist} - {selecthost} {pathhost} - Syntax Mismatch")
                    allonboardModel.objects.filter(ipaddress=iplist).delete()
                    userobj = User.objects.get(username=request.user)
                    log = AuditlogsModel(username=userobj, action='Onboard Device', status='Failure', message=categorize_issue_message(iplist, "Template not found"))
                    log.save()
                    continue

                json_field_data = {
                    "HOST_TEMPLATE": formatted_selecthost,
                    "IP_ADDRESS": iplist,
                    "REUSABLE_EMAIL": entry.get('emailid'),
                    "GLOBAL_APPLICATION": entry.get('servertype'),
                    "FRNDLY_NAME": entry.get('textname'),
                    "PHYSICAL_IP": entry.get('physical_ip', ''),
                    "COMMON_HOSTNAME": iplist
                }
                if entry.get('json'):
                    json_field_data.update(json.loads(entry['json']))

                json_field_str = json.dumps(json_field_data)
                is_new_or_updated = False

                if existing_entry:
                    already_onboarded_ip_addresses.append(iplist)
                    if (
                        existing_entry.textname != entry.get('textname') or
                        existing_entry.emailid != entry.get('emailid') or
                        existing_entry.selecthost != formatted_selecthost
                    ):
                        existing_entry.selecthost = formatted_selecthost
                        existing_entry.servertype = entry.get('servertype')
                        existing_entry.subipaddress = entry.get('subipaddress')
                        existing_entry.emailid = entry.get('emailid')
                        existing_entry.servicename = entry.get('servicename', '')
                        existing_entry.json = json_field_str
                        existing_entry.textname = entry.get('textname')
                        existing_entry.pathhost = entry.get('pathhost')
                        existing_entry.hostname = iplist
                        existing_entry.phyipaddr = entry.get('physical_ip', '')
                        existing_entry.mainipaddress = entry.get('ipaddress')
                        existing_entry.save()
                        is_new_or_updated = True
                else:
                    allonboardModel.objects.create(
                        selecthost=formatted_selecthost,
                        ipaddress=iplist,
                        servertype=entry.get('servertype'),
                        subipaddress=entry.get('subipaddress'),
                        emailid=entry.get('emailid'),
                        servicename=entry.get('servicename', ''),
                        json=json_field_str,
                        textname=entry.get('textname'),
                        pathhost=entry.get('pathhost'),
                        hostname=iplist,
                        phyipaddr=entry.get('physical_ip', ''),
                        mainipaddress=entry.get('ipaddress')
                    )
                    is_new_or_updated = True
                    cursor = connection.cursor()
                    addList = pingFileCreate(cursor)

                if is_new_or_updated:
                    tempStr = f"'{ipKey}':'{iplist}'"
                    for key, val in json_field_data.items():
                        tempStr += ',' + (f"'{key}':'{val}'" if not isinstance(val, list) else f"'{key}':{json.dumps(val)}")

                    cfg_file_name = f"{iplist}_linkedeye_{formatted_selecthost}"
                    obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
                    obj_createcfg.createCFGFile()

                    with open(os.path.join(cfg_path, cfg_file_name), "r") as yaml_file:
                        yaml_content = yaml.safe_load(yaml_file)
                    threading.Thread(target=create_nodes_async, args=(yaml_content,)).start()

        # Mgmt-addon processing
        allmanagement_entries = [{'entry': e, 'ipaddress': e['ipaddress']} for entries in mgmt_map.values() for e in entries]
        response_from_process = process_allmanagement_entries(allmanagement_entries)
        non_validated_management_entries = response_from_process.get('non_validated', [])

        # SNMP processing
        for ip, snmptable in snmp_map.items():
            version = snmptable["version"]
            user_name = snmptable.get("username", "NOVALUE")
            validation_snmp = snmp(
                version=version,
                securitylevel=snmptable.get("sec_level", "NOVALUE"),
                username=user_name,
                community_string=snmptable.get("comm_string", "NOVALUE"),
                authenticationmethod=snmptable.get("auth_method", "NOVALUE"),
                authenticationpassword=snmptable.get("auth_password", "NOVALUE"),
                privacymethod=snmptable.get("priv_method", "NOVALUE"),
                privacypassword=snmptable.get("priv_password", "NOVALUE"),
                ip=ip
            ).check()

            if isinstance(validation_snmp, bool) and validation_snmp:
                if not SnmpModel.objects.filter(ipaddress=ip).exists():
                    SnmpModel.objects.create(
                        ipaddress=ip,
                        protocol=version,
                        username=user_name,
                        model=snmptable["model"],
                        securitylevel=snmptable.get("sec_level", "NOVALUE"),
                        authenticationmethod=snmptable.get("auth_method", "NOVALUE"),
                        privacymethod=snmptable.get("priv_method", "NOVALUE"),
                        authenticationpassword=snmptable.get("auth_password", "NOVALUE"),
                        privacypassword=snmptable.get("priv_password", "NOVALUE"),
                        communitystring=snmptable.get("comm_string", "NOVALUE"),
                        threshold=snmptable.get("snmp_threshold", default_threshold)
                    )
                    cursor = connection.cursor()
                    snmpFileCreate(cursor)
            else:
                # STRICT GATE, matching process_allmanagement_entries: a failed
                # SNMP check offboards the device completely, same as a failed
                # mgmt check. Was advisory-only for a stretch (see git blame),
                # but that only ever applied to mgmt validation by explicit
                # request -- SNMP was left on the advisory branch by omission,
                # not by decision, and diverged from production's strict
                # offboard-on-failure behavior. Restored to match.
                non_validated_snmp_ipaddresses.append({'ip': ip, 'version': version})
                allonboardModel.objects.filter(ipaddress=ip).delete()
                cursor = connection.cursor()
                snmpFileCreate(cursor)
                try:
                    if is_valid_ip(ip):
                        client = Node()
                        if client._check(ip, key='hostIp', resOut=True):
                            client.execute(f"MATCH (a {{ hostIp:'{ip}' }}) DETACH DELETE a")
                except Exception as _neo_exc:
                    print(f"[LE] Neo4j offboard failed for {ip}: {_neo_exc}")

        # Logging
        userobj = User.objects.get(username=request.user)
        if not non_validated_snmp_ipaddresses and not non_validated_management_entries:
            onboarded_ips = [ip for ip in valid_ip_addresses if ip not in already_onboarded_ip_addresses and ip not in invalid_ip_addresses]
            for iplist in onboarded_ips:
                AuditlogsModel.objects.create(username=userobj, action='Onboard Device', status='Success', message=f'IP: {iplist} onboarded successfully')
            response_message = f'Data saved for IPs: {", ".join(onboarded_ips)}'
        else:
            for ipentry in non_validated_snmp_ipaddresses:
                AuditlogsModel.objects.create(username=userobj, action='Onboard Device', status='Failure', message=categorize_issue_message(ipentry['ip'], "SNMP issue"))
            for ipentry in non_validated_management_entries:
                AuditlogsModel.objects.create(username=userobj, action='Onboard Device', status='Failure', message=categorize_issue_message(ipentry['ip'], ipentry.get('prototype', '') + " issue"))
            response_message = 'Completed with errors for some IPs.'

        # ✅ Assemble device_data for email
        #device_data = []
        device_data_map = {}
        all_processed_ips = (set(valid_ip_addresses) | set(d['ip'] for d in non_validated_snmp_ipaddresses) | set(d['ip'] for d in non_validated_management_entries) | set(already_onboarded_ip_addresses))

        priority = {"Failure": 4, "Warning": 3, "Info": 2, "Success": 1}

        for ip in all_processed_ips:
            # Try direct match
            source = devices_map.get(ip, {})

            # Try value-based fallback (when ip is a value inside devices_map)
            fallback = next((v for v in devices_map.values() if v.get('ipaddress') == ip), {})

            # Merge source and fallback giving priority to non-empty fields
            def get_field(field):
                return source.get(field) or fallback.get(field, '')

            merged_data = {
                'selecthost': get_field('selecthost'),
                'ipaddress': ip,
                'subipaddress': get_field('subipaddress'),
                'servertype': get_field('servertype'),
                'emailid': get_field('emailid'),
                'servicename': get_field('servicename'),
                'textname': get_field('textname'),
                'pathhost': get_field('pathhost'),
                'phyipaddr': get_field('physical_ip'),
                'status': 'Success',
                'reason': 'No issue'
            }

            # Determine final status
            if any(d['ip'] == ip for d in non_validated_snmp_ipaddresses):
                version = next((d['version'] for d in non_validated_snmp_ipaddresses if d['ip'] == ip), 'Unknown')
                merged_data['status'] = "Failure"
                merged_data['reason'] = f"SNMP issue (Version: {version})"

            elif any(d['ip'] == ip for d in non_validated_management_entries):
                prototype = next((d['prototype'] for d in non_validated_management_entries if d['ip'] == ip), '')
                merged_data['status'] = "Failure"
                merged_data['reason'] = f"Mgmt issue ({prototype})"

            elif ip in valid_ip_addresses and ip in already_onboarded_ip_addresses:
                merged_data['status'] = "Warning"
                merged_data['reason'] = "Updated existing onboarded entry"

            elif ip in already_onboarded_ip_addresses:
                merged_data['status'] = "Info"
                merged_data['reason'] = "Already onboarded"

            elif any(ip in err for err in invalid_ip_addresses):
                merged_data['status'] = "Failure"
                merged_data['reason'] = "Template not found"

            # Update only if new or higher priority
            existing = device_data_map.get(ip)
            if not existing or priority[merged_data['status']] > priority[existing['status']]:
                device_data_map[ip] = merged_data

        # Final device list for email
        device_data = list(device_data_map.values())

        return JsonResponse({
            'status': 'success' if not non_validated_snmp_ipaddresses and not non_validated_management_entries else 'warning',
            'message': response_message,
            'valid_ip_addresses': valid_ip_addresses,
            'invalid_ip_addresses': invalid_ip_addresses,
            'validated': response_from_process.get('validated', []),
            'non_validated': non_validated_management_entries,
            'non_validated_snmp_ipaddresses': non_validated_snmp_ipaddresses,
            'already_onboarded_ip_addresses': already_onboarded_ip_addresses,
            'device_data': device_data  # ✅ for email summary
        })

    except json.JSONDecodeError:
        return JsonResponse({'status': 'error', 'message': 'Invalid JSON data'})

def send_onboard_summary(request):
    if request.method != 'POST':
        return JsonResponse({'status': 'error', 'message': 'Invalid method'})

    try:
        payload = json.loads(request.body)
        emailid = payload.get('emailid')
        device_data = payload.get('device_data', [])
        sitename = payload.get('sitename', 'Unknown Site')

        if not emailid or not device_data:
            return JsonResponse({'status': 'error', 'message': 'Missing data'})

        status = send_all_onboard_notification_email(emailid, "Summary Email", device_data, sitename)
        return JsonResponse({'status': 'success' if status else 'error'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'message': str(e)})

def send_all_onboard_notification_email(emailid, message, device_data, sitename):
    try:
        smtp_server = "smtp.office365.com"
        smtp_port = 587
        # FIXED: Use settings instead of hardcoded SMTP credentials
        smtp_user = getattr(settings, 'SMTP_USER', '')
        smtp_pass = getattr(settings, 'SMTP_PASS', '')
        cc_list = ["devops@finspot.in"]

        msg = MIMEMultipart("alternative")
        msg['From'] = smtp_user
        msg['To'] = emailid
        msg['Cc'] = ', '.join(cc_list)
        msg['Subject'] = f"{sitename} - Onboarding Device Report"

        # Build single HTML table with all device data
        table_headers = """
            <tr>
                <th>Select Host</th>
                <th>IPaddress</th>
                <th>subipaddress</th>
                <th>Server Type</th>
                <th>EmailID</th>
                <th>Service Name</th>
                <th>Text Name</th>
                <th>Path Host</th>
                <th>Physical IP</th>
                <th>Status</th>
                <th>Message</th>
            </tr>
        """

        table_rows = ""
        for d in device_data:
            host_type = d.get('selecthost', 'Unknown').split('-')[0].replace('.yaml', '')
            row = f"""
                <tr>
                    <td>{host_type}</td>
                    <td>{d.get('ipaddress')}</td>
                    <td>{d.get('subipaddress')}</td>
                    <td>{d.get('servertype', 'Unknown')}</td>
                    <td>{d.get('emailid')}</td>
                    <td>{d.get('servicename')}</td>
                    <td>{d.get('textname')}</td>
                    <td>{d.get('pathhost')}</td>
                    <td>{d.get('phyipaddr')}</td>
                    <td>{d.get('status')}</td>
                    <td>{d.get('reason')}</td>
                </tr>
            """
            table_rows += row

        html_body = f"""
        <html>
        <body>
            <p>The following devices have been onboarded:</p>
            <table border="1" cellpadding="5" cellspacing="0">
                {table_headers}
                {table_rows}
            </table>
        </body>
        </html>
        """

        msg.attach(MIMEText(html_body, "html"))
        all_recipients = [emailid] + cc_list

        server = smtplib.SMTP(smtp_server, smtp_port, timeout=10)
        server.starttls()
        server.login(smtp_user, smtp_pass)
        server.sendmail(smtp_user, all_recipients, msg.as_string())
        server.quit()

        print(f"Email sent successfully to {emailid} with CC to {cc_list}")
        return True

    except Exception as e:
        print(f"Failed to send onboarding email: {str(e)}")
        return False

# Define a function to categorize issue messages
def categorize_issue_message(ip, issue_message):
    #print("categorize_issue_message--1--->"+str(issue_message))
    if "ilo issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    elif "idrac issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    elif "Node Expo issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    elif "Nginx Expo issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    elif "Window Expo issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    elif "snmp issue" in issue_message:
        return f"IP: {ip} Reason: {issue_message}"
    else:
        return f"IP: {ip} Reason: {issue_message}"

def download_file(request):
    finonb_path = os.path.join(settings.BASE_DIR, "onboardOptions")
    testname = request.GET.get('testname', '')
    extension = '.xlsx'
    filename = '{}{}'.format(testname, extension)
    #path = os.path.join(r"C:\finalui\linkedeye-new-ui\LinkedEyeWebProject\onboardOptions", filename)
    path = os.path.join(finonb_path, filename)

    try:
        with open(path, 'rb') as f:
            response = HttpResponse(f.read(), content_type='application/xlsx')
            response['Content-Disposition'] = 'attachment; filename={}'.format(filename)
            return response
    except Exception as Error:
        return HttpResponse(Error)

    return redirect('emissions_dashboard:overview_view_record')


#DELETE OR DON'T USE CODE BELOW
def createcfg(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        hostData = data["host"]
        serviceData = data["service"]
        mgmntData = data["mgmnt"]
        nodemgmtData = data["nodemgmt"]
        #print("nodemgmtData---->"+str(nodemgmtData))
        ipList = data["iplist"]
        #print("ipList---->"+str(ipList))
        selecthost = data["selecthost"]
        servertype = data["servertype"]
        emailid = data["emailid"]
        textname = data["textname"]
        pathhost = data["pathhost"]
        phyipaddr = data["phyipaddress"]

        ipKey = "COMMON_IPADDRESS"
        #monitoringPath = hostData["PATH_TEMPLATE"]
        monitoringPath = 'DIRECT/' # change in future in case gateway logic added
        isEdit = data["isedit"]
        failureList = []
        modelList = []
        index = 0
        #print(type(ipList))
        #for ip in ipList:
        ip = ipList
        #print("ip---->"+str(ip))
        tempHostName = ip
        if(isEdit == True):
            allonboardModel.objects.filter(ipaddress=ip).delete()
            Model.cfg_file = template_path+"../nagios/nagios.cfg"
            #Model.cfg_file = "c:/nagios/nagios.cfg"
            cfgHost = Model.Host.objects.filter(address=ip)
            for temphostObj in cfgHost:
                if (path.exists("monitor/"+os.path.basename(temphostObj["meta"]["filename"])) == True):
                    os.remove("monitor/"+os.path.basename(temphostObj["meta"]["filename"]))
            for service in Model.Service.objects.filter(_NEO4j_address=ip):
                if (path.exists("monitor/"+os.path.basename(service["meta"]["filename"])) == True):
                    os.remove("monitor/"+os.path.basename(service["meta"]["filename"]))
            
        index = index + 1
        tempStr = "\'"+ipKey+"\':\'"+ip+"\'"
        for key, val in hostData.items():
            tempStr = tempStr+','
            if key == "COMMON_HOSTNAME":
                tempStr = tempStr+"\'"+key+"\':\'"+tempHostName+"\'"
            else:
                tempStr = tempStr+"\'"+key+"\':\'"+val+"\'"
            
        hostData["COMMON_HOSTNAME"] = tempHostName
        onboardHostModel = allonboardModel()
        onboardHostModel.hostname = tempHostName
        onboardHostModel.ipaddress = ip
        onboardHostModel.servicename = ""
        onboardHostModel.selecthost = selecthost
        onboardHostModel.servertype = servertype
        onboardHostModel.emailid = emailid
        onboardHostModel.textname = textname
        onboardHostModel.pathhost = pathhost
        onboardHostModel.phyipaddr = phyipaddr
        onboardHostModel.json = json.dumps(hostData)
        modelList.append(onboardHostModel)

        cfg_file_name = ip+"_linkedeye_"+hostData["HOST_TEMPLATE"].replace(".j2", ".cfg")
        #template_location = template_path+monitoringPath+"/HOSTS/"+hostData["HOST_TEMPLATE"].replace("_", "/")
        template_location = template_path+monitoringPath+hostData["HOST_TEMPLATE"].replace("_", "/")
        # print("template_location --->"+ str(template_location))
        obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
        #print('create cfg host -tempStr------->'+str(tempStr))
        #print('create cfg host -cfg_file_name------->'+str(cfg_file_name))
        #print('create cfg host -template_location------->'+str(template_location))
        #print('create cfg host -cfg_path------->'+str(cfg_path))
        output = obj_createcfg.createCFGFile()
        if(output != "Success"):
            failureList.append(hostData["HOST_TEMPLATE"])

        serviceIndex = 0
        for service in serviceData:
            tempStr = "\'"+ipKey+"\':\'"+ip+"\'"
            for skey, sval in service.items():
                tempStr = tempStr+','
                if skey == "COMMON_HOSTNAME":
                    tempStr = tempStr+"\'"+skey+"\':\'"+tempHostName+"\'"
                else:
                    tempStr = tempStr+"\'"+skey+"\':\'"+str(sval)+"\'"
                        
            onboardServiceModel = allonboardModel()
            onboardServiceModel.hostname = tempHostName
            onboardServiceModel.ipaddress = ip
            onboardServiceModel.servicename = service["SERVICE_TEMPLATE"].replace(".j2", "")
            onboardServiceModel.selecthost = selecthost
            onboardServiceModel.servertype = servertype
            onboardServiceModel.emailid = emailid
            onboardServiceModel.textname = textname
            onboardServiceModel.pathhost = pathhost
            onboardServiceModel.phyipaddr = phyipaddr
            onboardServiceModel.json = json.dumps(service)
            modelList.append(onboardServiceModel)

            cfg_file_name = ip+"_service_"+str(serviceIndex)+"__linkedeye_"+service["SERVICE_TEMPLATE"].replace(".j2", ".cfg")
            #template_location = template_path+monitoringPath+"/SERVICES/"+service["SERVICE_TEMPLATE"].replace("_", "/")
            template_location = template_path+monitoringPath+service["SERVICE_TEMPLATE"].replace("_", "/")
            obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
            #print('create cfg service -------->'+str(obj_createcfg))
            #print('create cfg service -cfg_file_name------->'+str(cfg_file_name))
            #print('create cfg service -template_location------->'+str(template_location))
            #print('create cfg service -cfg_path------->'+str(cfg_path))
            output = obj_createcfg.createCFGFile()
            serviceIndex = serviceIndex + 1
            if output.strip() != 'Success':
                failureList.append(service["SERVICE_TEMPLATE"])

            
        if len(failureList) == 0:
            allonboardModel.objects.bulk_create(modelList)

        #######
            #for _modelList in modelList:
                #print(_modelList)
            for mgmt in mgmntData:
                ip_id = allonboardModel.objects.get(ipaddress = ip).id
                obj = allmanagementModel(ip_id = ip_id, ipaddress = ip, prototype = mgmt["prototype"], username = mgmt["username"], password=mgmt["password"], iloip=mgmt["iloip"], port=mgmt["port"])
                obj.save()
                #cursor = connection.cursor()
                #addiloList = iloFileCreate(cursor)
                if mgmt["prototype"] == 'ilo':
                    cursor = connection.cursor()
                    addiloList = iloFileCreate(cursor)
                if mgmt["prototype"] == 'idrac':
                    cursor = connection.cursor()
                    addidracList = idracFileCreate(cursor)
            for nodesmgmt in nodemgmtData:
                ip_id = allonboardModel.objects.get(ipaddress = ip).id
                obj = allmanagementModel(ip_id = ip_id, ipaddress = ip, prototype = nodesmgmt["prototype"], username = nodesmgmt["username"], password=nodesmgmt["password"], iloip=nodesmgmt["iloip"], port=nodesmgmt["port"])
                obj.save()
                if nodesmgmt["prototype"] == 'Node Expo':
                    cursor = connection.cursor()
                    addnexpoList = nexpoFileCreate(cursor)

        #######
            out = createnodes(ipList)
            if out['status'] == 200:
                #subprocess.run(['sh', '../script/restartdocker.sh', ''], shell=False, timeout=1800) 
                response['status'] = out['status']
                response['data'] = "Successfully nodes were created!!"
            else:
                response['status'] = out['status']
                response['data'] = out['data']
            return HttpResponse(json.dumps(response))
        else:
            response['status'] = 400
            response['data'] = "Failure in creating CFG!! "
            return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['data'] = "Failure in creating CFG!! "+str(e)
        return HttpResponse(json.dumps(response))

#DELETE OR DON'T USE CODE ABOVE
