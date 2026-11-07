"""
Entity service layer (PREVENTIVE FIX #27).

Encapsulates business logic for Neo4j node/relationship CRUD and port
connection management.  Functions accept plain Python data (dicts, strings,
lists) and return dicts or model instances -- never Django request/response
objects.
"""

import ast
import json
import logging
import yaml

from lib.LinkedEyeEntity.Node import Node
from lib.LinkedEyeStruct.LinkedEyeStruct import K8S
from lesites.models import SiteModel
from entity.models import portconnectionModel
from django.db.models import Q

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Neo4j relationship helpers
# ---------------------------------------------------------------------------

def create_neo4j_relationship(source, target, entity_host, entity_port, is_url_secured):
    """Add a relationship between *source* and *target* in Neo4j.

    Args:
        source (str): Source node identifier.
        target (str): Target node identifier.
        entity_host (str): Neo4j entity API host.
        entity_port (str|int): Neo4j entity API port.
        is_url_secured (bool): Whether the connection uses HTTPS.

    Returns:
        dict: ``{'status': int, 'data': ...}``
    """
    response = {}
    try:
        client = Node(host=entity_host, port=entity_port, secure=is_url_secured)
        res = client.addRel(source, target)
        if res['status'] == 200:
            response['status'] = res['status']
            response['data'] = res['data']
        else:
            response['status'] = res['status']
    except Exception as e:
        logger.exception("create_neo4j_relationship failed for %s -> %s", source, target)
        response['status'] = 400
    return response


def delete_neo4j_relationship(source, target, entity_host, entity_port, is_url_secured):
    """Delete a relationship between *source* and *target* in Neo4j.

    Returns:
        dict: ``{'status': int, 'msg': str}``
    """
    response = {}
    try:
        client = Node(host=entity_host, port=entity_port, secure=is_url_secured)
        res = client.delRel(source, target)
        if res['status'] == 200:
            response['status'] = res['status']
            response['msg'] = 'Relationship deleted successfully'
        else:
            response['status'] = res['status']
            response['msg'] = 'Not able to delete relationship'
    except Exception as e:
        logger.exception("delete_neo4j_relationship failed for %s -> %s", source, target)
        response['status'] = 400
        response['msg'] = 'Not able to delete relationship'
    return response


# ---------------------------------------------------------------------------
# Port connection helpers
# ---------------------------------------------------------------------------

def get_port_connections():
    """Return all port connections as a list of dicts.

    Returns:
        dict: ``{'status': int, 'data': list[dict]}`` or error payload.
    """
    response = {}
    try:
        temp_list = []
        for conn in portconnectionModel.objects.all():
            temp_list.append({
                "id": conn.id,
                "source_modal": conn.source_modal,
                "source_ip": conn.source_ip,
                "source_port": conn.source_port,
                "source_name": conn.source_name,
                "destination_modal": conn.destination_modal,
                "destination_ip": conn.destination_ip,
                "destination_port": conn.destination_port,
                "destination_name": conn.destination_name,
            })
        response['status'] = 200
        response['data'] = temp_list
    except Exception as e:
        logger.exception("get_port_connections failed")
        response['status'] = 400
        response['msg'] = 'Something went wrong: ' + str(e)
    return response


def save_port_connection(data, domain_name):
    """Create a new port connection (DB + Neo4j link).

    Args:
        data (dict): Keys must include source_modal, source_ip, source_port,
            source_name, destination_modal, destination_ip, destination_port,
            destination_name.
        domain_name (str): Site name used to look up Neo4j connection info.

    Returns:
        dict: ``{'status': int|str, 'msg': str}``
    """
    response = {}
    try:
        site_obj = SiteModel.objects.filter(sitename=domain_name).first()
        if not site_obj:
            return {'status': 'error', 'message': 'Site not found'}

        nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)

        existing = portconnectionModel.objects.filter(
            source_modal=data['source_modal'],
            source_ip=data['source_ip'],
            source_port=data['source_port'],
            source_name=data['source_name'],
            destination_modal=data['destination_modal'],
            destination_ip=data['destination_ip'],
            destination_port=data['destination_port'],
            destination_name=data['destination_name'],
        ).first()

        if existing:
            response['msg'] = 'Port Connection already exists'
            response['status'] = 200
        else:
            portconnectionModel.objects.create(
                source_modal=data['source_modal'],
                source_ip=data['source_ip'],
                source_port=data['source_port'],
                source_name=data['source_name'],
                destination_modal=data['destination_modal'],
                destination_ip=data['destination_ip'],
                destination_port=data['destination_port'],
                destination_name=data['destination_name'],
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
            nodeObj.set_link(mode='update', data=conn_data)
            response['msg'] = 'Port Connection Added Successfully'
            response['status'] = 200
    except Exception as e:
        logger.exception("save_port_connection failed")
        response['status'] = 'error'
        response['message'] = str(e)
    return response


def delete_port_connections(domain_name, src_data, delconndata):
    """Delete one or more port connections (DB + Neo4j link).

    Args:
        domain_name (str): Site name for Neo4j look-up.
        src_data (dict): Source/destination info for Neo4j link deletion.
        delconndata (dict|list): Single ``{'data': {'id': ...}}`` or list of
            ``{'id': ...}`` dicts for bulk delete.

    Returns:
        dict: ``{'status': int|str, 'msg': str}``
    """
    response = {}
    try:
        site_objs = SiteModel.objects.filter(sitename=domain_name)
        for site_obj in site_objs:
            nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)
            src_data['source_port'] = ':' + src_data.get('source_port', '')
            src_data['destination_port'] = ':' + src_data.get('destination_port', '')
            nodeObj.set_link(mode='delete', data=src_data)

        if isinstance(delconndata, dict) and 'data' in delconndata and 'id' in delconndata['data']:
            portconnectionModel.objects.get(id=delconndata['data']['id']).delete()
            response['msg'] = 'Port Connection Deleted Successfully'
            response['status'] = 200
        elif isinstance(delconndata, list):
            ids_to_delete = [item['id'] for item in delconndata if 'id' in item]
            portconnectionModel.objects.filter(id__in=ids_to_delete).delete()
            response['msg'] = 'Port Connections Deleted Successfully'
            response['status'] = 200
        else:
            response['status'] = 'error'
            response['message'] = 'No valid id found in request data'
    except Exception as e:
        logger.exception("delete_port_connections failed")
        response['status'] = 'error'
        response['message'] = str(e)
    return response


def bulk_upload_port_connections(conndata, domain_name):
    """Upload multiple port connections from a bulk import.

    Args:
        conndata (list[dict]): Each dict has the standard port connection keys.
        domain_name (str): Site name for Neo4j look-up.

    Returns:
        dict: ``{'status': int|str, 'msg': str, 'details': list[str]}``
    """
    response = {}
    messages = []
    try:
        if domain_name in ['None', '', 'null']:
            return {'status': 'error', 'message': 'Domain name is None, null, or empty.'}

        for item in conndata:
            site_obj = SiteModel.objects.filter(sitename=domain_name).first()
            if not site_obj:
                continue

            nodeObj = Node(host=site_obj.entity_host, port=site_obj.entity_port, secure=site_obj.is_URLSecured)

            exists = portconnectionModel.objects.filter(
                Q(source_modal=item['source_modal']) &
                Q(source_ip=item['source_ip']) &
                Q(source_port=item['source_port']) &
                Q(source_name=item['source_name']) &
                Q(destination_modal=item['destination_modal']) &
                Q(destination_ip=item['destination_ip']) &
                Q(destination_port=item['destination_port']) &
                Q(destination_name=item['destination_name'])
            ).exists()

            if not exists:
                portconnectionModel.objects.create(
                    source_modal=item['source_modal'],
                    source_ip=item['source_ip'],
                    source_port=item['source_port'],
                    source_name=item['source_name'],
                    destination_modal=item['destination_modal'],
                    destination_ip=item['destination_ip'],
                    destination_port=item['destination_port'],
                    destination_name=item['destination_name'],
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
                nodeObj.set_link(mode='update', data=conn_data)
                messages.append(
                    f"Port connection for {item['source_ip']}:{item['source_port']} "
                    f"to {item['destination_ip']}:{item['destination_port']} created successfully."
                )
            else:
                messages.append(
                    f"Port connection for {item['source_ip']}:{item['source_port']} "
                    f"to {item['destination_ip']}:{item['destination_port']} already present."
                )

        response['msg'] = 'Port Connection uploaded Successfully'
        response['status'] = 200
        response['details'] = messages
    except Exception as e:
        logger.exception("bulk_upload_port_connections failed")
        response['status'] = 'error'
        response['message'] = str(e)
    return response


# ---------------------------------------------------------------------------
# Process node creation
# ---------------------------------------------------------------------------

def create_process_nodes(json_output):
    """Create Neo4j host/pod nodes and their relationships from process data.

    This is the pure-logic equivalent of ``createprocessnodes`` in views.py.

    Args:
        json_output (dict): Must contain a ``"data"`` key with a list of
            node descriptors.

    Returns:
        dict: ``{'status': int, 'data': str}``
    """
    client = Node()
    try:
        if "data" not in json_output or len(json_output["data"]) == 0:
            raise ValueError("No 'data' key found in the input or 'data' is empty")

        ipaddress_value = None
        created_nodes = {}
        relationships_to_create = []
        process_data_grouped = {}

        # Phase 1 -- Create nodes (hosts and pods)
        for obj in json_output["data"]:
            if not isinstance(obj, dict):
                continue

            for key in ["_NEO4j_parent", "_NEO4j_title"]:
                if key in obj:
                    obj[key] = obj[key].strip().lower()

            k8s_status = 1 if obj.get("cross_conn", "").strip().lower() == "yes" else 2

            # Host nodes
            if "ipaddress" in obj:
                ipaddress_value = obj["ipaddress"].strip().lower()
                obj["_NEO4j_address"] = ipaddress_value
                obj["_NEO4j_title"] = ipaddress_value
                obj["_NEO4j_parent"] = ipaddress_value
                obj["_NEO4j_type"] = "Node"
                obj["_NEO4j_layer"] = "s_hw"
                obj["_NEO4j_icon"] = "kubernetes_node.png"

                combinedDic = {k: obj[k] for k in obj if k.startswith("_NEO4j_")}
                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = k8s_status
                _struct = k8s_instance.convertor('nagios', combinedDic)
                _struct_dict = ast.literal_eval(_struct)
                _struct_dict["monitor_status"] = "running"

                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[ipaddress_value] = _struct_dict

            # Pod nodes
            if "processname" in obj:
                processname_value = obj["processname"].strip().lower()
                obj["_NEO4j_processname"] = processname_value
                obj["_NEO4j_address"] = ipaddress_value
                obj["_NEO4j_title"] = f"{ipaddress_value}:{processname_value}".lower()
                obj["_NEO4j_parent"] = ipaddress_value
                obj["_NEO4j_layer"] = "s_sw"
                obj["_NEO4j_icon"] = "IIS.png"
                obj["_NEO4j_type"] = "Pod"

                wf_values = []
                if "critical" in obj:
                    wf_values.append(f"critical:{obj['critical']}")
                if "warning" in obj:
                    wf_values.append(f"warning:{obj['warning']}")
                obj["_NEO4j_WF"] = ",".join(wf_values)

                combinedDic = {k: obj[k] for k in obj if k.startswith("_NEO4j_")}
                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = k8s_status
                _struct = k8s_instance.convertor('nagios', combinedDic)
                _struct_dict = ast.literal_eval(_struct)
                _struct_dict["monitor_status"] = "running"

                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[obj["_NEO4j_title"]] = _struct_dict

                for key in obj:
                    if key.startswith("connection"):
                        connection_value = obj[key].strip().lower()
                        if connection_value in ["na", "NA", ""]:
                            continue
                        relationships_to_create.append({
                            "parent": obj["_NEO4j_title"],
                            "child": f"{ipaddress_value}:{connection_value}",
                        })

                if ipaddress_value not in process_data_grouped:
                    process_data_grouped[ipaddress_value] = [{"ipaddress": ipaddress_value}]

                thresholds = []
                if "critical" in obj:
                    thresholds.append({f"{processname_value}_c": obj["critical"]})
                if "warning" in obj:
                    thresholds.append({f"{processname_value}_w": obj["warning"]})

                process_data_grouped[ipaddress_value].append({
                    "processname": processname_value,
                    "threshold": thresholds,
                })

        # Phase 2 -- Create relationships
        created_relationships = set()
        for rel in relationships_to_create:
            parent = rel["parent"]
            child = rel["child"]
            if parent == child:
                continue
            if child not in created_nodes:
                ip_val, proc_val = child.split(":")
                new_pod = {
                    "_NEO4j_title": child,
                    "_NEO4j_address": ip_val,
                    "_NEO4j_parent": ip_val,
                    "_NEO4j_layer": "s_sw",
                    "_NEO4j_icon": "IIS.png",
                    "_NEO4j_type": "Pod",
                    "_NEO4j_processname": proc_val,
                }
                k8s_instance = K8S("")
                k8s_instance.kubeCommon["status"] = 2
                _struct = k8s_instance.convertor('nagios', new_pod)
                _struct_dict = ast.literal_eval(_struct)
                _struct_dict["monitor_status"] = "running"
                createResponse = client.create(_struct_dict)
                if createResponse['status'] == 200:
                    created_nodes[child] = _struct_dict

            if parent in created_nodes and child in created_nodes:
                rel_key = (parent, child)
                if rel_key not in created_relationships:
                    result = client.addprcRel(parent=parent, child=child)
                    if result:
                        created_relationships.add(rel_key)

        # Phase 3 -- Generate YAML
        process_yaml_data = {
            "process": [{ip: entries} for ip, entries in process_data_grouped.items()]
        }
        with open("./prom-conf/process.yaml", "w+") as f:
            yaml.dump(process_yaml_data, f, default_flow_style=False)

        return {"status": 200, "data": "Nodes, relationships, and YAML file processed successfully"}

    except Exception as e:
        logger.exception("create_process_nodes failed")
        return {"status": 400, "data": f"Error: {str(e)}"}
