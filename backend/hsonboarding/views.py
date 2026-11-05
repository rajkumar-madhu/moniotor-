from django.shortcuts import render,HttpResponse
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
from lib.LinkedEyeDiscover import Discover
import json

# Create your views here.

@login_required(login_url="/")
@role_required(allowed_roles = ["Admin"])
def index(request):
    return render(request, 'hsonboard.html')

def gethsconfig(request):
    try:
        ipaddress = request.POST['ipaddress']
        path = request.POST['path']
        response = { }
        discover = Discover()
        res = discover.scanHS(ipaddress,path)
        if res['status'] == 200:
            response['status'] = res['status']
            response['data'] = res['data']
        else:
            response['status'] = res['status']
        return HttpResponse(json.dumps(response))
        #data = []
        #data.append({'COMMON_HOSTNAME': 'ecity-server3', 'COMMON_IPADDRESS': '172.16.0.13', 'CUSTOM_SERVICENAME': 'RequestProcessor', 'CUSTOM_PORT': '4469'})
        #data.append({'COMMON_HOSTNAME': 'ecity-server3', 'COMMON_IPADDRESS': '172.16.0.13', 'CSTOM_SERVICENAME': 'Websocket1', 'CSTOM_PORT': '9001'})
        #data.append({'COMMON_HOSTNAME': 'ecity-server3', 'COMMON_IPADDRESS': '172.16.0.13', 'CSTOM_SERVICENAME': 'Websocket2', 'CSTOM_PORT': '9002'})
        #data.append({'COMMON_HOSTNAME': 'ecity-server3', 'COMMON_IPADDRESS': '172.16.0.13', 'CSTOM_SERVICENAME': 'Websocket3', 'CSTOM_PORT': '9003'})
    except Exception as e:
        response['status'] = 400


