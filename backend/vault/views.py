from django.shortcuts import render, redirect
from lib.LinkedEyeVault import Vault
from django.http import HttpResponse
from json import dumps as jdumps
import json
import os
from vault.models import VaultModel
from django.template import loader
from login.decorators import role_required
from django.contrib.auth.decorators import login_required
import logging

logger = logging.getLogger(__name__)

# HIGH FIX #15: REMOVED global mutable Obj = {}
# Global mutable state persists across ALL requests - thread-unsafe and causes issues
# Instead, create Vault instance per request


@login_required(login_url="/")
@role_required(allowed_roles=["Admin"])
def vault(request):
    """
    Vault dashboard view.
    HIGH FIX #15: Create Vault instance per request instead of global mutable state.
    """
    temp_vault = Vault()
    template = loader.get_template('vault.html')
    response = temp_vault.Status()
    
    if response['status'] == 200:
        data = response['data']
        if data['status'] == 200:
            msg = json.loads(data['msg'])
            if msg['sealed'] == True:
                status = "Sealed"
            else:
                status = "Unsealed"
    else:
        status = "Unsealed"
    
    context = {
        'secrets': VaultModel.objects.all(),
        'status': status,
        'role': request.user.groups.all()[0].name
    }
    return HttpResponse(template.render(context, request))


@login_required(login_url="/")
@role_required(allowed_roles=["Admin"])
def vaultOperation(request):
    """
    Vault CRUD operations.
    HIGH FIX #15: Create Vault instance per request instead of using global Obj.
    """
    response = []
    if request.method == 'POST':
        clientData = request.POST['clientData']
        parsed_json = json.loads(clientData)
        try:
            # HIGH FIX #15: Create Vault instance per request
            temp_vault = Vault()
            
            for data_obj in parsed_json['data']:
                secretData = {}
                url = '/secret/' + data_obj["service"] + '/' + data_obj["ip"] + '/' + data_obj["username"]
                if data_obj["operation"] == 'add':
                    res = temp_vault.addSecret(data_obj["service"], data_obj["ip"], data_obj["username"], data_obj["password"])
                    if res['status'] == 200:
                        data = res['data']
                        if data['status'] == 204:
                            if not VaultModel.objects.filter(url=url):
                                ob_secret = VaultModel(service=data_obj["service"], server=data_obj["ip"], user=data_obj["username"], url=url)
                                ob_secret.save()
                                obj = VaultModel.objects.get(url=url)
                                secretData['rowid'] = obj.id
                                secretData['url'] = url
                                secretData['status'] = data['status']
                                response.append(secretData)
                elif data_obj["operation"] == 'delete':
                    response = {}
                    res = temp_vault.delSecret(data_obj["service"], data_obj["ip"], data_obj["username"])
                    if res['status'] == 200:
                        data = res['data']
                        if data['status'] == 204:
                            deleteobj = VaultModel.objects.get(url=url)
                            response['status'] = data['status']
                            response['rowid'] = deleteobj.id
                            deleteobj.delete()
                elif data_obj["operation"] == 'update':
                    response = {}
                    res = temp_vault.updateSecret(data_obj["service"], data_obj["ip"], data_obj["username"], data_obj["password"])
                    if res['status'] == 200:
                        data = res['data']
                        if data['status'] == 204:
                            obj = VaultModel.objects.get(id=data_obj["rowid"])
                            obj.server = data_obj["ip"]
                            obj.url = url
                            obj.save()
                            response['url'] = url
                            response['rowid'] = data_obj["rowid"]
                            response['status'] = data['status']
            return HttpResponse(json.dumps(response), content_type="json")
        except Exception as e:
            logger.error(f'vaultOperation error: {str(e)}')
            response = {}
            response['status'] = 400
            response['msg'] = 'Something went wrong'
            return HttpResponse(json.dumps(response), content_type="json")


@login_required(login_url="/")
@role_required(allowed_roles=["Admin"])
def getfilenames(request):
    response = {}
    try:
        all_services = ['Gateway']
        HostsList = os.listdir('template/DIRECT')
        all_services = HostsList
        response['status'] = 200
        response['data'] = all_services
        return HttpResponse(json.dumps(response))
    except Exception as e:
        logger.error(f'getfilenames error: {str(e)}')
        response['status'] = 400
        return HttpResponse(json.dumps(response))


@login_required(login_url="/")
@role_required(allowed_roles=["Admin"])
def changeStatus(request):
    """
    Change Vault seal status.
    HIGH FIX #15: Create Vault instance per request.
    """
    try:
        response = {}
        status = request.GET['status']
        # HIGH FIX #15: Create Vault instance per request
        temp_vault = Vault()
        
        if status == 'Sealed':
            res = temp_vault.Unseal()
            if res['status'] == 200:
                response['status'] = res['data']
            else:
                response['status'] = status
                response['code'] = res['status']
                response['msg'] = 'Not able to Unseal'
        else:
            res = temp_vault.Seal()
            if res['status'] == 200:
                response['status'] = res['data']
            else:
                response['status'] = status
                response['code'] = res['status']
                response['msg'] = 'Not able to Seal'
        return HttpResponse(json.dumps(response), content_type="json")
    except Exception as e:
        logger.error(f'changeStatus error: {str(e)}')
        response = {'status': 400, 'code': 400, 'msg': 'Something went wrong'}
        return HttpResponse(json.dumps(response), content_type="json")


@login_required(login_url="/")
@role_required(allowed_roles=["Admin"])
def getallsecrets(request):
    """
    Get all secrets from Vault.
    HIGH FIX #15: Create Vault instance per request.
    """
    response = {}
    try:
        # HIGH FIX #15: Create Vault instance per request
        temp_vault = Vault()
        
        res = temp_vault.Status()
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
        logger.error(f'getallsecrets error: {str(e)}')
        response['status'] = 400
        response['msg'] = 'Something went wrong'
        return HttpResponse(json.dumps(response))
