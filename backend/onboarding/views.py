import sys, os, json
from pynag import Model
from django.shortcuts import render,HttpResponse
from jinja2 import Environment, FileSystemLoader,meta
from lib.Jinja.CreateCfg import CreateCFG
from lib.LinkedEyeEntity.Node import Node
from lib.LinkedEyeStruct.LinkedEyeStruct import K8S
from django.shortcuts import render, redirect
from django.template import loader
from autodiscover.models import AutoDiscoveryModel
from .models import OnboardingModel
from django.contrib.auth.models import User, auth, Group
from django.contrib import messages
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
import subprocess
import requests
from requests.auth import HTTPBasicAuth
import os
import os.path
from os import path
from django.conf import settings
from django.db import connection, transaction
from django.forms.models import model_to_dict
import datetime
import time

template_path = "template/"
cfg_path = "monitor/"


def _cypher_escape(value):
    """Escape a value for safe inclusion inside a single-quoted Cypher string literal.

    Backslash MUST be escaped first, otherwise a trailing backslash in the input
    would escape the quote we add and let the caller break out of the literal.
    """
    return str(value).replace("\\", "\\\\").replace("'", "\\'").replace('"', '\\"')

@login_required(login_url="/")
# @role_required(allowed_roles = ["Admin"])
def index(request):
    return render(request, 'app/newonboard.html')

def getfilenames(request):
    try:
        response = {}
        all_filenames = []
        path = request.GET['fileName']
        local_template_path = template_path + str(path)
        tecnologyNames = os.listdir(local_template_path)
        for tech in tecnologyNames:
            techSubNames = os.listdir(local_template_path+"/"+tech)
            for techSub in techSubNames:
                technologyFileNames = os.listdir(local_template_path+"/"+tech+"/"+techSub)
                for filename in technologyFileNames:
                    if get_file_extension(filename) == '.j2':
                        all_filenames.append(tech+"_"+techSub+"_"+filename)
        json_data = all_filenames
        response['status'] = 200
        response['data'] = json_data
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400

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

def get_file_extension(file_name):
    return os.path.splitext(file_name)[1]

def getfilecontentdata(request):
    response = {}
    try:
        file_name = request.POST['filename']
        file_path = file_name.replace("_", "/")
        fine_name_array = file_name.split("_")
        jfile_name = fine_name_array[len(fine_name_array) - 1]
        path = os.path.dirname(os.path.abspath(template_path+file_path+""))
        template_env = Environment(autoescape=False,loader=FileSystemLoader(os.path.join(path)),trim_blocks=False)
        template_source = template_env.loader.get_source(template_env, jfile_name)
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

def deletehost(request):
    response = {}
    try:
        ipaddress = request.POST["ipaddress"]
        hostname = request.POST["hostname"]
        OnboardingModel.objects.filter(ipaddress=ipaddress).delete()
        Model.cfg_file = template_path+"../nagios/nagios.cfg"
        cfgHost = Model.Host.objects.filter(address=ipaddress)
        for service in Model.Service.objects.filter(_NEO4j_address=ipaddress):
            if (path.exists("monitor/"+os.path.basename(service["meta"]["filename"])) == True):
                os.remove("monitor/"+os.path.basename(service["meta"]["filename"]))
        for temphostObj in cfgHost:
            if (path.exists("monitor/"+os.path.basename(temphostObj["meta"]["filename"])) == True):
                os.remove("monitor/"+os.path.basename(temphostObj["meta"]["filename"]))

        client = Node()
        safe_hostname = _cypher_escape(hostname)
        client.execute("MATCH (a:Host {  host:'" + safe_hostname + "' } ) DETACH DELETE a")
        client.execute("MATCH (a:HostMS {  parent:'" + safe_hostname + "' } ) DETACH DELETE a")
        client.execute("MATCH (a:Service {  parent:'" + safe_hostname + "' } ) DETACH DELETE a")
        client.execute("MATCH (a:ServiceMS) WHERE a.parent CONTAINS '" + safe_hostname + ":' DETACH DELETE a")
        response['status'] = 200
        response['data'] = "Host deleted successfully"
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['data'] = "Not able to delete host"
        return HttpResponse(json.dumps(response))


def createcfg(request):
    response = {}
    try:
        data = json.loads(request.POST.get('data'))
        hostData = data["host"]
        serviceData = data["service"]
        ipList = data["iplist"]
        ipKey = "COMMON_IPADDRESS"
        monitoringPath = hostData["PATH_TEMPLATE"]
        isEdit = data["isedit"]
        failureList = []
        modelList = []
        index = 0
        for ip in ipList:
            tempHostName = ip
            if(isEdit == True):
                OnboardingModel.objects.filter(ipaddress=ip).delete()
                Model.cfg_file = template_path+"../nagios/nagios.cfg"
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
            onboardHostModel = OnboardingModel()
            onboardHostModel.hostname = tempHostName
            onboardHostModel.ipaddress = ip
            onboardHostModel.servicename = ""
            onboardHostModel.json = json.dumps(hostData)
            modelList.append(onboardHostModel)
            cfg_file_name = ip+"_linkedeye_"+hostData["HOST_TEMPLATE"].replace(".j2", ".cfg")
            template_location = template_path+monitoringPath+"/HOSTS/"+hostData["HOST_TEMPLATE"].replace("_", "/")
            obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
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

                onboardServiceModel = OnboardingModel()
                onboardServiceModel.hostname = tempHostName
                onboardServiceModel.ipaddress = ip
                onboardServiceModel.servicename = service["SERVICE_TEMPLATE"].replace(".j2", "")
                onboardServiceModel.json = json.dumps(service)
                modelList.append(onboardServiceModel)

                cfg_file_name = ip+"_service_"+str(serviceIndex)+"__linkedeye_"+service["SERVICE_TEMPLATE"].replace(".j2", ".cfg")
                template_location = template_path+monitoringPath+"/SERVICES/"+service["SERVICE_TEMPLATE"].replace("_", "/")
                obj_createcfg = CreateCFG("", tempStr, cfg_file_name, template_location, cfg_path)
                output = obj_createcfg.createCFGFile()
                serviceIndex = serviceIndex + 1
                if output.strip() != 'Success':
                    failureList.append(service["SERVICE_TEMPLATE"])
        if len(failureList) == 0:
            OnboardingModel.objects.bulk_create(modelList)
            out = createnodes(ipList)
            if out['status'] == 200:
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
        response['data'] = "Failure in creating CFG!! "
        return HttpResponse(json.dumps(response))


def createnodes(ipList):
    client = Node()
    response = {}
    try:
        if not len(ipList) == 0:
            for ip in ipList:
                Model.cfg_file = template_path+"../nagios/nagios.cfg"
                all_hosts = Model.Host.objects.filter(host_name=ip)
                all_services = Model.Service.objects.filter(host_name=ip)

                for hostObj in all_hosts:
                    hostDic = {}
                    for key in hostObj.keys():
                        if key.startswith("_NEO4j_"):
                            hostDic[key] = hostObj[key]
                    if not hostDic == {}:
                        safe_hn = _cypher_escape(hostDic["_NEO4j_hostname"])
                        client.execute("MATCH (a:Host {  hostname:'" + safe_hn + "' } ) DETACH DELETE a")
                        client.execute("MATCH (a:HostMS {  parent:'" + safe_hn + "' } ) DETACH DELETE a")
                        client.execute("MATCH (a:Service {  parent:'" + safe_hn + "' } ) DETACH DELETE a")
                        client.execute("MATCH (a:ServiceMS) WHERE a.parent CONTAINS '" + safe_hn + ":' DETACH DELETE a")
                        _struct = K8S("").convertor('nagios', hostDic)
                        createResponse = client.create(json.loads(_struct))
                        if createResponse['status'] == 200:
                            createResponse = {}
                            for serviceObj in all_services:
                                serviceDic = {}
                                for key in serviceObj.keys():
                                    if key.startswith("_NEO4j_"):
                                        serviceDic[key] = serviceObj[key]
                                if not serviceDic == {}:
                                    _struct = K8S("").convertor('nagios', serviceDic)
                                    createResponse = client.create(json.loads(_struct))
                                    time.sleep(5)
                                    if createResponse['status'] == 200:
                                        createResponse = {}
                                        for serviceObj in all_services:
                                            serviceDic = {}
                                            for key in serviceObj.keys():
                                                if key.startswith("_NEO4j_"):
                                                    serviceDic[key] = serviceObj[key]
                                            if not serviceDic == {} and (not serviceDic["_NEO4j_parent"] == "\"\""or not serviceDic["_NEO4j_title"]):
                                                createResponse = client.addRel(serviceDic["_NEO4j_parent"], serviceDic["_NEO4j_title"])
                                                if createResponse['status'] == 200:
                                                    response['status'] = createResponse['status']
                                                    response['data'] = "Successfully nodes were created!!"
                                                else:
                                                    response['status'] = createResponse['status']
                                                    response['data'] = "Failure in relationship creation"
                                    else:
                                        response['status'] = createResponse['status']
                                        response['data'] = "Failure in service creation"
                        else:
                            response['status'] = createResponse['status']
                            response['data'] = "Failure in host creation"
        return response
    except Exception as e:
        response['status'] = 400
        response['data'] = "Failure in node creation!! " + str(e)
        return response


def gethostservicedata(request):
    response = {}
    try:
        Model.cfg_file = template_path+"../nagios/nagios.cfg"
        all_hosts = Model.Host.objects.all
        tempList = []
        for hostObj in all_hosts:
            hostDic = {}
            hostDic["host_name"] = hostObj["host_name"]
            hostDic["address"] = hostObj["address"]
            hostDic["contact_email"] = hostObj["_NEO4j_contactEmail"]
            hostDic["application"] = hostObj["_NEO4j_application"]
            hostDic["automation"] = "No" if hostObj["_NEO4j_Automation"] == '' else hostObj["_NEO4j_Automation"]
            hostDic["layer"] = hostObj["_NEO4j_layer"]
            tempList.append(hostDic)
        response['status'] = 200
        response['data'] = tempList
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        return HttpResponse(json.dumps(response))

def edithostdetails(request):
    response = {}
    try:
        temp_list = []
        ipaddress = request.GET['ipaddress']
        editData = OnboardingModel.objects.filter(ipaddress=ipaddress)
        for p in editData:
            json_obj = {}
            json_obj["json"] = p.json
            json_obj["hostname"] = p.hostname
            json_obj["ipaddress"] = p.ipaddress
            json_obj["servicename"] = p.servicename
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        return HttpResponse(json.dumps(response))
