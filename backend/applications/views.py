
from django.views.decorators.clickjacking import xframe_options_exempt
from django.contrib.auth.decorators import login_required
from django.template import loader
from applications.models import ApplicationModel
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
from django.shortcuts import render,HttpResponse
#from newonb.models import newOnbModel
from dashboard.models import SnmpModel
#from newonb.models import managementModel
from django.http import JsonResponse
import csv
import json

@xframe_options_exempt
@login_required(login_url="/")
def application(request):
    isIframe = request.GET.get('isIframe', False)
    template = loader.get_template('application.html')
    context = {
        'isIframe': isIframe
    }
    return HttpResponse(template.render(context, request))
def applicationactions(request):
    if request.method == 'POST':
        response = {}
        clientData = json.loads(request.POST['clientData'])
        parsed_json = clientData['data']
        try:
            if parsed_json["operation"] == 'add':
                applicationname = parsed_json['applicationname']
                if ApplicationModel.objects.filter(applicationname = applicationname).exists():
                    response['msg'] = 'Application name already exist.'
                    response['status'] = 500
                else:
                    obj = ApplicationModel(applicationname = applicationname)
                    obj.save()
                    response['msg'] = 'Sucessfully added application'
                    response['status'] = 200
                    response['rowid'] = ApplicationModel.objects.get(applicationname = applicationname).id
            elif parsed_json["operation"] == 'delete':
                deleteobj = ApplicationModel.objects.get(id=parsed_json["rowid"])
                deleteobj.delete()
                response['status'] = 200
                response['msg'] = 'Sucessfully deleted application'
                response['rowid'] = parsed_json["rowid"]
            elif parsed_json["operation"] == 'update':
                obj = ApplicationModel.objects.get(id = parsed_json["rowid"])
                obj.applicationname = parsed_json["applicationname"]
                obj.save()
                response['status'] = 200
                response['msg'] = 'Application name updated sucessfully'
                response['rowid'] =  parsed_json["rowid"]
        except Exception as e:
            response['status'] = 400
            response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")
def getallapplicationnames(request):
    response = {}
    try:
        temp_list = []
        app_obj = ApplicationModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["applicationname"] = temp.applicationname
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

"""def exportnewonb(request):
    response = {}
    try:
        temp_list = []
        app_obj = newOnbModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["selecthost"] = temp.selecthost
            json_obj["ipaddress"] = temp.ipaddress
            json_obj["servertype"] = temp.servertype
            json_obj["emailid"] = temp.emailid
            json_obj["servicename"] = temp.servicename
            json_obj["textname"] = temp.textname
            json_obj["pathhost"] = temp.pathhost
            json_obj["hostname"] = temp.hostname
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))"""

"""def exportmgmntdata(request):
    response = {}
    try:
        temp_list = []
        temp = managementModel.objects.all()
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
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))"""

def exportsnmp(request):
    response = {}
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
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return HttpResponse(json.dumps(response))
