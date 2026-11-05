from django.db import models
from django.shortcuts import render, redirect
from lib.LinkedEyeVault import Vault
from django.http import HttpResponse
from json import dumps as jdumps
import json, os
from addservice.models import ServerstypesModel,ServersosModel,ServerssoftwareModel,ServersnicModel, SwitcheslayersModel,SwitchesmodelModel, FirewalltypeModel, FirewallmodelModel, RoutertypeModel, RoutermodelModel
from django.template import loader
from login.decorators import role_required
from django.contrib.auth.decorators import login_required

Obj = {}

@login_required(login_url="/")
@role_required(allowed_roles = ["Admin"])

def serviceOperation(request):
    if request.method == 'POST':
        response = {}
        clientData = request.POST['clientData']
        parsed_json = json.loads(clientData)
        serv_operation=parsed_json['data'][0]['operation']
        device=parsed_json['data'][0]['device']
        servicetype=parsed_json['data'][0]['servicetype']
        servicename=parsed_json['data'][0]['servicename']
        rowid=parsed_json['data'][0].get('rowid','')
        db=''
        if device == 'server' and servicetype == 'types':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if ServerstypesModel.objects.filter(types = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = ServerstypesModel(types = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added server type'
                        response['status'] = 200
                        response['rowid'] = ServerstypesModel.objects.get(types = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = ServerstypesModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted server type'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = ServerstypesModel.objects.get(id=rowid)
                    obj.types = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updatedserver type'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Servertype actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device == 'server' and servicetype == 'os':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if ServersosModel.objects.filter(os = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = ServersosModel(os = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added server os'
                        response['status'] = 200
                        response['rowid'] = ServersosModel.objects.get(os = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = ServersosModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted server os'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = ServersosModel.objects.get(id=rowid)
                    obj.os = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updated server software'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====ServerOS actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device == 'server' and servicetype == 'software':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if ServerssoftwareModel.objects.filter(software = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = ServerssoftwareModel(software = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added server software'
                        response['status'] = 200
                        response['rowid'] = ServerssoftwareModel.objects.get(software = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = ServerssoftwareModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted server software'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = ServerssoftwareModel.objects.get(id=rowid)
                    obj.software = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updated server software'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====ServerSoftware actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device == 'server' and servicetype == 'nic':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if ServersnicModel.objects.filter(nic = servicename).exists():
                        response['msg'] = 'Server Nic name already exist.'
                        response['status'] = 500
                    else:
                        obj = ServersnicModel(nic = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added server nic'
                        response['status'] = 200
                        response['rowid'] = ServersnicModel.objects.get(nic = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = ServersnicModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted server nic'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = ServersnicModel.objects.get(id=rowid)
                    obj.nic = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updated server nic'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====ServerNic actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device == 'switch'and servicetype == 'layer':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if SwitcheslayersModel.objects.filter(layers = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = SwitcheslayersModel(layers = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added switch layer'
                        response['status'] = 200
                        response['rowid'] = SwitcheslayersModel.objects.get(layers = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = SwitcheslayersModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted switch layer'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = SwitcheslayersModel.objects.get(id=rowid)
                    obj.layers = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updated switch layer'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Switch Software actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device == 'switch'and servicetype == 'model':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if SwitchesmodelModel.objects.filter(model = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = SwitchesmodelModel(model = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added switch model'
                        response['status'] = 200
                        response['rowid'] = SwitchesmodelModel.objects.get(model = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = SwitchesmodelModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted switch model'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = SwitchesmodelModel.objects.get(id=rowid)
                    obj.model = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully Updated switch model'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Switch model actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device=='firewall' and servicetype=='types':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if FirewalltypeModel.objects.filter(types = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = FirewalltypeModel(types = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added firewall type'
                        response['status'] = 200
                        response['rowid'] = FirewalltypeModel.objects.get(types = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = FirewalltypeModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted firewall type'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = FirewalltypeModel.objects.get(id=rowid)
                    obj.types = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully updated firewall type'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Firewalltype actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device=='firewall' and servicetype=='model':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if FirewallmodelModel.objects.filter(model = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = FirewallmodelModel(model = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added firewall model'
                        response['status'] = 200
                        response['rowid'] = FirewallmodelModel.objects.get(model = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = FirewallmodelModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted firewall model'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = FirewallmodelModel.objects.get(id=rowid)
                    obj.model = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully updated firewall model'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Firewallmodel actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device=='router' and servicetype=='types':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if RoutertypeModel.objects.filter(types = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = RoutertypeModel(types = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added router type'
                        response['status'] = 200
                        response['rowid'] = RoutertypeModel.objects.get(types = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = RoutertypeModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted router type'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = RoutertypeModel.objects.get(id=rowid)
                    obj.types = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully updated router type'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Routertype actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        elif device=='router' and servicetype=='model':
            try:
                if serv_operation == 'add':
                    servicename = parsed_json['data'][0]['servicename']
                    if RoutermodelModel.objects.filter(model = servicename).exists():
                        response['msg'] = 'Service name already exist.'
                        response['status'] = 500
                    else:
                        obj = RoutermodelModel(model = servicename)
                        obj.save()
                        response['msg'] = 'Sucessfully added router model'
                        response['status'] = 200
                        response['rowid'] = RoutermodelModel.objects.get(model = servicename).id
                elif serv_operation == 'delete':
                    deleteobj = RoutermodelModel.objects.get(id= rowid)
                    deleteobj.delete()
                    response['status'] = 200
                    response['msg'] = 'Successfully deleted router model'
                    response['rowid'] = rowid
                elif serv_operation == 'update':
                    obj = RoutermodelModel.objects.get(id=rowid)
                    obj.model = servicename
                    obj.save()
                    response['service'] = servicename
                    response['msg'] = 'Successfully updated router model'
                    response['rowid'] = rowid
                    response['status'] = 200
            except Exception as e:
                print("===Exception====Routermodel actions===")
                print(str(e))
                response['status'] = 400
                response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")



def getfilenames(request):
    response = {}
    try:
        all_services = ['Gateway']
        HostsList = os.listdir('template/DIRECT/HOSTS')
        ServicesList = [ techSub  for tech in os.listdir('template/DIRECT/SERVICES') for techSub in os.listdir('template/DIRECT/SERVICES/'+tech)]
        all_services = HostsList + ServicesList
        response['status'] = 200
        response['data'] = all_services
        return HttpResponse(json.dumps(response))
    except Exception as e:
        print('=======Exception=====')
        print(str(e))
        response['status'] = 400
        return HttpResponse(json.dumps(response))

def changeStatus(request):
    try:
        response = { }
        status = request.GET['status']
        if  status == 'Sealed':
            res = Obj.Unseal()
            if res['status'] == 200:
                response['status'] = res['data']
            else:
                response['status'] = status
                response['code'] = res['status']
                response['msg'] = 'Not able to Unseal'
        else:
            res = Obj.Seal()
            if res['status'] == 200:
                response['status'] = res['data']
            else:
                response['status'] = status
                response['code'] = res['status']
                response['msg'] = 'Not able to Seal' 
        return HttpResponse(json.dumps(response), content_type="json")
    except Exception as e:
        print('===changeStatus======Exception==')
        print(str(e))
        response['status'] = status
        response['code'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response), content_type="json")
def getallsecrets(request):
    response = {}
    try:
        global Obj
        try:
            Obj = Vault()
        except Exception as e:
            print('=====Exception====')
            print(str(e))
            response['status'] = 400
            response['msg'] = 'Linkedeye Vault system not reachable. Please contact administrator'
            return HttpResponse(json.dumps(response))
        res = Obj.Status()
        if res['status'] == 200:
            data = res['data']
            if data['status'] == 200:
                msg = json.loads(data['msg'])
                if msg['sealed'] == True:
                    status = "Sealed"
                else:
                    status = "Unsealed"
        else:
            status = "Unsealed"
        temp_list = []
        secret_objs = VaultModel.objects.all()
        print(secret_objs)
        for secret in secret_objs:
            json_obj = {}
            json_obj["id"] = secret.id
            json_obj["username"] = secret.user
            json_obj["service"] = secret.service
            json_obj["ip"] = secret.server
            json_obj["url"] = secret.url
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        response['role'] = request.user.groups.all()[0].name
        response['secretstatus'] = status
        return HttpResponse(json.dumps(response))
    except Exception as e:
        print('=======Exception===getallsecrets=====')
        print(str(e))
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))

def getalldropdowndata(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            temp_list = []
            servertype_data=getServersType(request)
            serveros_data=getServersOs(request)
            serversoftware_data=getServersSoftware(request)
            servernic_data=getServersNic(request)
            switchlayer_data=getSwitchesLayers(request)
            switchmodel_data=getSwitchesModel(request)
            firewalltype_data=getFirewallType(request)
            firewallmodel_data=getFirewallModel(request)
            routertype_data=getRouterType(request)
            routermodel_data=getRouterModel(request)
            temp_list.append(servertype_data)
            temp_list.append(serveros_data)
            temp_list.append(serversoftware_data)
            temp_list.append(servernic_data)
            temp_list.append(switchlayer_data)
            temp_list.append(switchmodel_data)
            temp_list.append(firewalltype_data)
            temp_list.append(firewallmodel_data)
            temp_list.append(routertype_data)
            temp_list.append(routermodel_data)
            response['data'] = temp_list
        response['status'] = 200
        return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getServersTypeModal====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No servertype Available'
        return HttpResponse(json.dumps(response))

def getServersType(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            st_list = []
            service_obj = ServerstypesModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["types"] = temp.types
                st_list.append(json_obj)
            response['data'] = st_list
            response['dbservice'] = 'serverstypes'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getServersTypeModal====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No servertype Available'
        return HttpResponse(json.dumps(response))

def getServersOs(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            st_list = []
            service_obj = ServersosModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["os"] = temp.os
                st_list.append(json_obj)
            response['data'] = st_list
            response['dbservice'] = 'serversos'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getServersOsModal====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No serveros Available'
        return HttpResponse(json.dumps(response))

def getServersSoftware(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            ss_list = []
            service_obj = ServerssoftwareModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["software"] = temp.software
                ss_list.append(json_obj)
            response['data'] = ss_list
            response['dbservice'] = 'serverssoftware'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getServerssoftwareModel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No ServerssoftwareModel Available'
        return HttpResponse(json.dumps(response))


def getServersNic(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            ss_list = []
            service_obj = ServersnicModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["nic"] = temp.nic
                ss_list.append(json_obj)
            response['data'] = ss_list
            response['dbservice'] = 'serversnic'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getServerssoftwareModel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No ServerssoftwareModel Available'
        return HttpResponse(json.dumps(response))

def getSwitchesLayers(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            sl_list = []
            service_obj = SwitcheslayersModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["layers"] = temp.layers
                sl_list.append(json_obj)
            response['data'] = sl_list
            response['dbservice'] = 'switcheslayers'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getSwitcheslayersModel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No SwitcheslayersModel Available'
        return HttpResponse(json.dumps(response))

def getSwitchesModel(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            sm_list = []
            service_obj = SwitchesmodelModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["model"] = temp.model
                sm_list.append(json_obj)
            response['data'] = sm_list
            response['dbservice'] = 'switchesmodel'
        response['status'] = 200
        return (json.dumps(response))
       # return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getSwitchesmodelModel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No SwitchesmodelModel Available'
        return HttpResponse(json.dumps(response))

def getFirewallType(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            fm_list = []
            service_obj = FirewalltypeModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["types"] = temp.types
                fm_list.append(json_obj)
            response['data'] = fm_list
            response['dbservice'] = 'firewalltypes'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getFirewallType====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No Firewalltype Available'
        return HttpResponse(json.dumps(response))

def getFirewallModel(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            fm_list = []
            service_obj = FirewallmodelModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["model"] = temp.model
                fm_list.append(json_obj)
            response['data'] = fm_list
            response['dbservice'] = 'firewallmodel'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getFirewallModel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No FirewallModel Available'
        return HttpResponse(json.dumps(response))

def getRouterType(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            fm_list = []
            service_obj = RoutertypeModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["types"] = temp.types
                fm_list.append(json_obj)
            response['data'] = fm_list
            response['dbservice'] = 'routertypes'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getRoutertypes====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No Routertypes Available'
        return HttpResponse(json.dumps(response))

def getRouterModel(request):
    print('this is request'+ str(request))
    response = {}
    try:
        if request.method == 'GET':
            fm_list = []
            service_obj = RoutermodelModel.objects.all()
            for temp in service_obj:
                json_obj = {}
                json_obj["rowid"] = temp.id
                json_obj["model"] = temp.model
                fm_list.append(json_obj)
            response['data'] = fm_list
            response['dbservice'] = 'routermodel'
        response['status'] = 200
        return (json.dumps(response))
        #return HttpResponse(json.dumps(response))
    except Exception as e:
        print("===Exception==getroutermodel====")
        print(str(e))
        response['status'] = 400
        response['msg'] = 'No Routermodel Available'
        return HttpResponse(json.dumps(response))