import json
from django.shortcuts import render,HttpResponse
from lib.LinkedEyeDiscover.Discover import Discover
from .models import AutoDiscoveryModel
from bulk_sync import bulk_sync
from django.db.models import Q
from django.contrib.auth.decorators import login_required
from login.decorators import role_required

@login_required(login_url="/")
# @role_required(allowed_roles = ["Admin"])
def index(request):
    return render(request, 'autodiscovery.html')

def getdiscoverydetails(request):
    discover = Discover()
    response = { }
    res = discover.scan(request.GET["from"],request.GET["to"])
    if res['status'] == 200:
        response['status'] = res['status']
        response['data'] = res['data']
    else:
        response['status'] = res['status']
    return HttpResponse(json.dumps(response))

def savedata(request):
    response = {}
    try:
        data = request.POST['clientData']
        parsed_json = json.loads(data)
        subnet = request.POST['subnet']
        AutoDiscoveryModel.objects.filter(subnet=subnet).delete()
        discover_models = []
        for jsonObj in parsed_json:
            modelObj = AutoDiscoveryModel()
            modelObj.ostype = jsonObj["os"]
            modelObj.ipaddress = jsonObj["ip"]
            modelObj.subnet = subnet
            discover_models.append(modelObj)
        filters = Q() 
        key_fields = ("ipaddress",) 
        ret = bulk_sync(new_models=discover_models, filters=filters, key_fields=key_fields)
        response['status'] = 200
        response['data'] = ret
        return HttpResponse(json.dumps(response))
    except Exception as ex:
        response['status'] = 400
        return HttpResponse(json.dumps(response))