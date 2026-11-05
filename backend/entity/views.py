from django.shortcuts import render
from django.http import HttpResponse
from lib.LinkedEyeEntity.Node import Node
from lib.LinkedEyeStruct.LinkedEyeStruct import K8S
from lib.LinkedEyeRedis import Redis
from lesites.models import SiteModel
from django.contrib.auth.decorators import login_required
from login.decorators import role_required
import json
import logging

logger = logging.getLogger('linkedeye')
import yaml
from json import dumps as jdumps
from django.conf import settings
from django.http import JsonResponse
from .models import portconnectionModel
from lesites.models import SiteModel
from django.db.models import Q

@login_required(login_url="/")
def index(request):
    temp_json = {
                    "websocketurl": settings.WEBSOCKET_URL
                }
    return render(request, 'app/entityview.html', temp_json)

@login_required(login_url="/")
# @role_required(allowed_roles = ["Admin"])
def addrelationships(request):
    return render(request, 'app/relationshipview.html')

def setneo4jrelationships(request):
    response = {}
    try:
        parsed_json = json.loads(request.POST['alldata'])
        source = parsed_json["source"]
        target = parsed_json["target"]
        client = Node(host=parsed_json["entity_host"], port=parsed_json["entity_port"], secure=parsed_json["is_URLSecured"])
        res = client.addRel(source,target)
        if(res['status'] == 200):
            response['status'] = res['status']
            response['data'] = res['data']
        else:
            response['status'] = res['status']
        return HttpResponse(json.dumps(response))
    except Exception as e:
       # print('==Exception=====setneo4jrelationships=====')
       # print(str(e))
        response['status'] = 400
        return HttpResponse(json.dumps(response))

def delete_relationship(request):
    response = {}
    try:
        parsed_json = json.loads(request.POST['alldata'])
        source = parsed_json["source"]
        target = parsed_json["target"]
        client = Node(host=parsed_json["entity_host"], port=parsed_json["entity_port"], secure=parsed_json["is_URLSecured"])
        res = client.delRel(source,target)
        if(res['status'] == 200):
            response['status'] = res['status']
            response['msg'] = 'Relationship deleted sucessfully'
        else:
            response['status'] = res['status']
            response['msg'] = 'Not able to delete relationship'
        return HttpResponse(json.dumps(response))
    except Exception as e:
       # print('==Exception=====delete_relationship=====')
       # print(str(e))
        response['status'] = 400
        response['msg'] = 'Not able to delete relationship'
        return HttpResponse(json.dumps(response))

def getneo4jspecificelement(request):
    response = {}
    try:
        title = request.GET['title']
        required = request.GET['required']	
        site = request.GET["sitename"]
        site_objs = SiteModel.objects.filter(sitename = site)
        res={}
        for site_obj in site_objs:
            client = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            response['data'] = client.getSpecificElement(title=title,reqEle=required)
        return HttpResponse(json.dumps(response))
    except Exception as e:
        response['status'] = 400
        return HttpResponse(json.dumps(response))

def portconnection(request):   #ADD port connection
    response = {}
    try:
        #print('DOMAIN_NAME - {}'.format(request.POST.get('domain_name')))
        site_objs = SiteModel.objects.filter(sitename = request.POST.get('domain_name'))
        for site_obj in site_objs:
            #print('site_obj.entity_host - {}'.format(site_obj.entity_host))
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
        
            data = json.loads(request.POST.get('conndata'))
            
            # Check if the port connection already exists
            existing_connection = portconnectionModel.objects.filter(
                source_modal=data['source_modal'],
                source_ip=data['source_ip'],
                source_port=data['source_port'],
                source_name=data['source_name'],
                destination_modal=data['destination_modal'],
                destination_ip=data['destination_ip'],
                destination_port=data['destination_port'],
                destination_name=data['destination_name']
            ).first()
            
            if existing_connection:
                response['msg'] = 'Port Connection already exists'
                response['status'] = 200
            else:
                port_connection = portconnectionModel.objects.create(
                    source_modal=data['source_modal'],
                    source_ip=data['source_ip'],
                    source_port=data['source_port'],
                    source_name=data['source_name'],
                    destination_modal=data['destination_modal'],
                    destination_ip=data['destination_ip'],
                    destination_port=data['destination_port'],
                    destination_name=data['destination_name']
                )
                
                dest_port = ':' + data['destination_port']
                if data['destination_port'] == 'NIC':
                    dest_port = ''
                if '/' in dest_port:
                    dest_port = dest_port.replace('/', '_')
                conn_data = {
                    'source_ip': data['source_ip'],
                    'source_port': ':' + data['source_port'],
                    'dest_ip': data['destination_ip'],
                    'dest_port': dest_port,
                    'source_name': data['source_name'],
                    'dest_name': data['destination_name'],
                }
                node_res = nodeObj.set_link(mode='update', data=conn_data)
                response['msg'] = 'Port Connection Added Successfully'
                response['status'] = 200
    except Exception as e:
        response['status'] = 'error'
        response['message'] = str(e)
    return JsonResponse(response)

def connporttable(request):
    response = {}
    try:
        temp_list = []
        app_obj = portconnectionModel.objects.all()
        for temp in app_obj:
            json_obj = {}
            json_obj["id"] = temp.id
            json_obj["source_modal"] = temp.source_modal
            json_obj["source_ip"] = temp.source_ip
            json_obj["source_port"] = temp.source_port
            json_obj["source_name"] = temp.source_name
            json_obj["destination_modal"] = temp.destination_modal
            json_obj["destination_ip"] = temp.destination_ip
            json_obj["destination_port"] = temp.destination_port
            json_obj["destination_name"] = temp.destination_name
            temp_list.append(json_obj)
        response['status'] = 200
        response['data'] = temp_list
        return JsonResponse(response)
    except Exception as e:
        response['status'] = 400
        response['msg'] = 'Something went wrong'+str(e)
        return JsonResponse(response)

def deleteconnection(request): #DELETE port connection
    response = {}
    try:
        # Extract domain name and src_data from the request
        domain_name = request.POST.get('domain_name')
        src_data = json.loads(request.POST.get('src_data', '{}'))
        delconndata = json.loads(request.POST.get('delconndata', '{}'))
        
        # Fetch site objects based on the domain name
        site_objs = SiteModel.objects.filter(sitename=domain_name)
        for site_obj in site_objs:
            nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)
            src_data['source_port'] = ':' + src_data.get('source_port', '')
            src_data['destination_port'] = ':' + src_data.get('destination_port', '')
            node_res = nodeObj.set_link(mode='delete', data=src_data)
        
        # Handle single or multiple deletions
        if isinstance(delconndata, dict) and 'data' in delconndata and 'id' in delconndata['data']:
            # Single deletion
            port_connection = portconnectionModel.objects.get(id=delconndata['data']['id'])
            port_connection.delete()
            response['msg'] = 'Port Connection Deleted Successfully'
            response['status'] = 200
        elif isinstance(delconndata, list):
            # Multiple deletions
            ids_to_delete = [item['id'] for item in delconndata if 'id' in item]
            portconnectionModel.objects.filter(id__in=ids_to_delete).delete()
            response['msg'] = 'Port Connections Deleted Successfully'
            response['status'] = 200
        else:
            response['status'] = 'error'
            response['message'] = 'No valid id found in request data'
    except Exception as e:
        response['status'] = 'error'
        response['message'] = str(e)
    
    return JsonResponse(response)

def editportconnection(request):       #EDIT port connection
    response = {}
    try:
        site_objs = SiteModel.objects.filter(sitename = request.POST.get('domain_name'))
        for site_obj in site_objs:
            #print('site_obj.entity_host - {}'.format(site_obj.entity_host))
            nodeObj = Node(host=site_obj.entity_host,port=site_obj.entity_port,secure=site_obj.is_URLSecured)
            data = json.loads(request.POST.get('conneditdata'))
            port_connection = portconnectionModel.objects.get(id=data['id'])
            port_connection.source_modal = data['source_modal']
            port_connection.source_ip = data['source_ip']
            port_connection.source_port = data['source_port']
            port_connection.source_name = data['source_name']
            port_connection.destination_modal = data['destination_modal']
            port_connection.destination_ip = data['destination_ip']
            port_connection.destination_port = data['destination_port']
            port_connection.destination_name = data['destination_name']
            port_connection.save()
            dest_port= ':' + data['destination_port']
            if(data['destination_port']=='NIC'):
                dest_port=''
            if '/' in dest_port:
                dest_port = dest_port.replace('/', '_')
            conn_data={
                       'prev_src_ip' : request.POST.get('prev_src_ip'),
                       'prev_src_port' : ':' + request.POST.get('prev_src_port'),
                       'prev_dest_ip' : request.POST.get('prev_dest_ip'),
                       'prev_dest_port' : ':' + request.POST.get('prev_dest_port'),
                       'source_ip' : data['source_ip'],
                       'source_port' : ':' + data['source_port'],
                       'dest_ip' : data['destination_ip'],
                       'dest_port' : dest_port,
                       'source_name' : data['source_name'],
                       'dest_name' : data['destination_name']
                }
            node_res = nodeObj.set_link(mode='update',data=conn_data)
        response['msg'] = 'Port Connection Modified Successfully'
        response['status'] = 200
    except Exception as e:
        response = {'status': 'error', 'message': str(e)}
    return JsonResponse(response)

def download_port_file(request):
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

def uploadportconn(request):
    response = {}
    try:
        if request.method == 'POST':
            data = json.loads(request.body)
            conndata = data.get('conndata', [])
            domain_name = data.get('domain_name')

            # Check if domain_name is None, null, or empty string
            if domain_name in ['None', '', 'null']:
                response['status'] = 'error'
                response['message'] = 'Domain name is None, null, or empty.'
                return JsonResponse(response)

            messages = []

            for item in conndata:
                site_objs = SiteModel.objects.filter(sitename=domain_name)
                if not site_objs.exists():
                    continue

                site_obj = site_objs.first()
                nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)

                # Check if the port connection already exists
                existing_port_connection = portconnectionModel.objects.filter(
                    Q(source_modal=item['source_modal']) &
                    Q(source_ip=item['source_ip']) &
                    Q(source_port=item['source_port']) &
                    Q(source_name=item['source_name']) &
                    Q(destination_modal=item['destination_modal']) &
                    Q(destination_ip=item['destination_ip']) &
                    Q(destination_port=item['destination_port']) &
                    Q(destination_name=item['destination_name'])
                ).exists()

                if not existing_port_connection:
                    port_connection = portconnectionModel.objects.create(
                        source_modal=item['source_modal'],
                        source_ip=item['source_ip'],
                        source_port=item['source_port'],
                        source_name=item['source_name'],
                        destination_modal=item['destination_modal'],
                        destination_ip=item['destination_ip'],
                        destination_port=item['destination_port'],
                        destination_name=item['destination_name']
                    )

                    dest_port = ':' + item['destination_port']
                    if item['destination_port'] == 'NIC':
                        dest_port = ''
                    if '/' in dest_port:
                        dest_port = dest_port.replace('/', '_')

                    conn_data = {
                        'source_ip': item['source_ip'],
                        'source_port': ':' + item['source_port'],
                        'dest_ip': item['destination_ip'],
                        'dest_port': dest_port,
                        'source_name': item['source_name'],
                        'dest_name': item['destination_name'],
                    }

                    node_res = nodeObj.set_link(mode='update', data=conn_data)

                    messages.append(f"Port connection for {item['source_ip']}:{item['source_port']} to {item['destination_ip']}:{item['destination_port']} created successfully.")
                else:
                    messages.append(f"Port connection for {item['source_ip']}:{item['source_port']} to {item['destination_ip']}:{item['destination_port']} already present.")

            response['msg'] = 'Port Connection uploaded Successfully'
            response['status'] = 200
            response['details'] = messages
    except Exception as e:
        response['status'] = 'error'
        response['message'] = str(e)
    return JsonResponse(response)

def postentprocessdata(request):
    try:
        if request.method == 'POST':
            raw_data = request.body  # Raw bytes from the body of the request
            # Convert the raw data into a Python dictionary (JSON format)
            data = json.loads(raw_data)

            # Call createprocessnodes and check its response
            result = createprocessnodes(data)

            if result["status"] != 200:
                return JsonResponse({"msg": "error", "status": result["status"], "details": result["data"]}, status=result["status"])

            return JsonResponse({"msg": "success", "status": 200, "details": result["data"]}, status=200)

        else:
            # Handle invalid HTTP methods (only POST is allowed)
            return JsonResponse({"msg": "error", "status": 400, "details": "Invalid request method, only POST is allowed"}, status=400)

    except json.JSONDecodeError:
        # Catch JSON parsing errors and return a message
        return JsonResponse({"msg": "error", "status": 400, "details": "Invalid JSON format in request body"}, status=400)

    except Exception as e:
        # Catch any other unexpected errors and log the details
        print("Error:", str(e))  # Log the error to the console or error log
        return JsonResponse({"msg": "error", "status": 500, "details": f"An error occurred: {str(e)}"}, status=500)

def createprocessnodes(json_output):
    client = Node()
    response = {}
    try:
        if "data" not in json_output or len(json_output["data"]) == 0:
            raise ValueError("No 'data' key found in the input or 'data' is empty")

        ipaddress_value = None  # Store IP address for Hosts and Pods
        created_nodes = {}  # Dictionary to store created nodes
        relationships_to_create = []  # List to store relationships to process later
        process_data_grouped = {}  # To group process data by IP address

        # Phase 1: Create Nodes (Hosts and Pods)
        for obj in json_output["data"]:
            if not isinstance(obj, dict):
                continue

            for key in ["_NEO4j_parent", "_NEO4j_title"]:
                if key in obj:
                    obj[key] = obj[key].strip().lower()

            # Determine the status based on "cross_conn"
            k8s_status = 1 if obj.get("cross_conn", "").strip().lower() == "yes" else 2

            # Process Hosts
            if "ipaddress" in obj:
                ipaddress_value = obj["ipaddress"].strip().lower()
                obj["_NEO4j_address"] = ipaddress_value
                obj["_NEO4j_title"] = ipaddress_value
                obj["_NEO4j_parent"] = ipaddress_value
                obj["_NEO4j_type"] = "Node"
                obj["_NEO4j_layer"] = "s_hw"
                obj["_NEO4j_icon"] = "kubernetes_node.png"

                combinedDic = {key: obj[key] for key in obj.keys() if key.startswith("_NEO4j_")}
                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = k8s_status

                _struct = k8s_instance.convertor('nagios', combinedDic)
                _struct_dict = json.loads(_struct)

                # Set "monitor_status" to "running"
                _struct_dict["monitor_status"] = "running"

                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[ipaddress_value] = _struct_dict

            # Process Pods
            if "processname" in obj:
                processname_value = obj["processname"].strip().lower()
                obj["_NEO4j_processname"] = processname_value
                obj["_NEO4j_address"] = ipaddress_value
                obj["_NEO4j_title"] = f"{ipaddress_value}:{processname_value}".lower()
                obj["_NEO4j_parent"] = ipaddress_value
                obj["_NEO4j_layer"] = "s_sw"
                obj["_NEO4j_icon"] = "IIS.png"
                obj["_NEO4j_type"] = "Pod"

                # Add _NEO4j_WF to obj
                wf_values = []
                if "critical" in obj:
                    wf_values.append(f"critical:{obj['critical']}")
                if "warning" in obj:
                    wf_values.append(f"warning:{obj['warning']}")
                obj["_NEO4j_WF"] = ",".join(wf_values)

                # Prepare combinedDic for Neo4j storage
                combinedDic = {key: obj[key] for key in obj.keys() if key.startswith("_NEO4j_")}

                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = k8s_status

                _struct = k8s_instance.convertor('nagios', combinedDic)
                _struct_dict = json.loads(_struct)

                # Set "monitor_status" to "running"
                _struct_dict["monitor_status"] = "running"

                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[obj["_NEO4j_title"]] = _struct_dict

                # Handle dynamic connections
                for key in obj:
                    if key.startswith("connection"):
                        connection_value = obj[key].strip().lower()
                        if connection_value in ["na","NA", ""]:
                            continue  # Skip if connection is "NA" or empty
                        relationships_to_create.append({
                            "parent": obj["_NEO4j_title"],
                            "child": f"{ipaddress_value}:{connection_value}",
                        })

                # Group processes by IP address for YAML output
                if ipaddress_value not in process_data_grouped:
                    process_data_grouped[ipaddress_value] = [{"ipaddress": ipaddress_value}]

                thresholds = []
                if "critical" in obj:
                    thresholds.append({f"{processname_value}_c": obj["critical"]})
                if "warning" in obj:
                    thresholds.append({f"{processname_value}_w": obj["warning"]})

                process_entry = {
                    "processname": processname_value,
                    "threshold": thresholds
                }
                process_data_grouped[ipaddress_value].append(process_entry)

        # Phase 2: Create Relationships
        created_relationships = set()

        for rel in relationships_to_create:
            parent = rel["parent"]
            child = rel["child"]

            # Skip relationships where parent and child are the same
            if parent == child:
                continue

            # Check if the child exists; if not, create it as a pod dynamically
            if child not in created_nodes:
                ipaddress_value, processname_value = child.split(":")
                new_pod_node = {
                    "_NEO4j_title": child,
                    "_NEO4j_address": ipaddress_value,
                    "_NEO4j_parent": ipaddress_value,
                    "_NEO4j_layer": "s_sw",
                    "_NEO4j_icon": "IIS.png",
                    "_NEO4j_type": "Pod",
                    "_NEO4j_processname": processname_value,
                }
                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = 2  # Default status if not specified

                # Convert and create the pod node
                _struct = k8s_instance.convertor('nagios', new_pod_node)
                _struct_dict = json.loads(_struct)
                _struct_dict["monitor_status"] = "running"

                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[child] = _struct_dict

            # Add the relationship
            if parent in created_nodes and child in created_nodes:
                rel_key = (parent, child)
                if rel_key not in created_relationships:
                    result = client.addprcRel(parent=parent, child=child)
                    if result:
                        created_relationships.add(rel_key)

        # Phase 3: Prepare YAML data
        process_yaml_data = {
            "process": [
                {ip: entries} for ip, entries in process_data_grouped.items()
            ]
        }

        # Save YAML data to file
        with open("./prom-conf/process.yaml", "w+") as f:
            yaml.dump(process_yaml_data, f, default_flow_style=False)

        return {"status": 200, "data": "Nodes, relationships, and YAML file processed successfully"}

    except Exception as e:
        return {"status": 400, "data": f"Error: {str(e)}"}

"""def getentprocesskeys(request):
    response = { }
    responseObj = []
    if request.GET["sitename"] == ' ':
        user_id = User.objects.get(username=request.user).id
        siteid_list = Usersite.objects.filter(user_id=user_id).values_list('site_id', flat=True)
        site_ids = [x for x in siteid_list]
        site_objs = SiteModel.objects.filter(id__in=site_ids, is_enable=True)
        response["refreshedsite"] = ' '
    else:
        site = request.GET["sitename"]
        mode = request.GET.get("mode","ALL")
        site_objs = SiteModel.objects.filter(sitename = site)
        response["refreshedsite"] = site
    for site_obj in site_objs:
        temp_obj = {}
        temp_obj['site'] = site_obj.sitename
        try:
            redisObj = Redis(host=site_obj.redis_host, port=site_obj.redis_port)
            if mode!="VER":
                redisKeys = redisObj.getBodEodkeys(site=site_obj.sitename,mode=mode)
            else:
                redisKeys = redisObj.getBodEodkeys(site=site_obj.sitename,mode=mode,ip=request.GET.get("ip",""))
            keys = []
            for key in redisKeys:
                tempObj = {}
                # Check if the key contains "ProcessStatus"
                if "ProcessStatus" in key:
                    # Retain the full key
                    tempObj["key"] = key
                    # Extract "ProcessStatus" part for the key
                    #tempObj["extracted_key_part"] = "ProcessStatus"
                    try:
                        # Get the value associated with the key from Redis
                        key_data = redisObj.get(key)
                        # FIXED: json.loads instead of ast.literal_eval
                        tempObj["key_data"] = json.loads(key_data) if key_data else None
                    except Exception as e:
                        # Handle any exceptions during Redis data fetch or evaluation
                        tempObj["key_data"] = f"Error: {str(e)}"
                    # Append to the keys list
                    keys.append(tempObj)
            temp_obj["site_data"] = keys
            temp_obj["code"] = 200
        except Exception as e:
            print('=======Exception=====')
            print(str(e)) 
            temp_obj["site_data"] = []
            temp_obj["code"] = 500
        responseObj.append(temp_obj)
    response["status"] = 200
    response["responseData"] = responseObj
    return HttpResponse(jdumps(response), content_type="json")"""