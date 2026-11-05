#
from scalpl import Cut
import json
import time
import ast

class K8S:
    def __init__(self,namespace):
        self.ret = {}
        self.current_milli_time = lambda: int(round(time.time() * 1000))
        self.retProxy = Cut(self.ret)
        self.kubeCommon = {
        "application" : namespace,
        "epoch" : self.current_milli_time(),
        "status" : 4,
        }
        self.nodeStruct = {
        "label" : "application",
        "title" : "raw_object.metadata.name",
        "name" : "raw_object.metadata.name",
        "kind" : "raw_object.kind",
        "type" : "raw_object.kind",
        "host" : "raw_object.metadata.name",
        "hostIp" : "raw_object.status.addresses[0].address",
        "ip" : "raw_object.status.addresses[0].address",
        "overlayIP": "",
        #"overlayIP" : "raw_object.metadata.annotations.projectcalico.org/IPv4IPIPTunnelAddr",
        "info_kernelVersion" : "raw_object.status.nodeInfo.kernelVersion",
        "info_kubeletVersion" : "raw_object.status.nodeInfo.kubeletVersion",
        "info_operatingSystem" : "raw_object.status.nodeInfo.operatingSystem",
        "info_architecture" : "raw_object.status.nodeInfo.architecture",
        "info_containerRuntimeVersion" : "raw_object.status.nodeInfo.containerRuntimeVersion",
        "info_ephemeral_storage" : "raw_object.status.capacity.ephemeral-storage",
        "info_hugepages_1Gi" : "raw_object.status.capacity.hugepages-1Gi",
        "info_hugepages_2Mi" : "raw_object.status.capacity.hugepages-2Mi",
        "dockerimage" : "raw_object.status.nodeInfo.osImage",
        "monitor_status" : "raw_object.status.conditions[4].status",
        "monitor_message" : "raw_object.status.conditions[4].message",
        "monitor_cpu" : "raw_object.status.capacity.cpu",
        "monitor_memory" : "raw_object.status.capacity.memory",
        "monitor_downtime" : "",
        #"pods" : "raw_object.status.capacity.pods",
        "parent" : "",
        "procParent" : "",
        "events" : "",
        "tech" : "",
        "ticket_assignee" : "",
        "ticket_status" : "",
        "ticket_number" : 0,
        "automation_isEnabled" : False,
        "automation_vault" : "",
        "automation_workflow" : {},
        "contact_email" : "",
        "contact_sms" : "",
        }

        self.cmapStruct = {
        "kind" : "raw_object.kind",
        "data" : "raw_object.metadata.annotations.FSChangedKey.data",
        "name" : "raw_object.metadata.name",
        "namespace" : "raw_object.metadata.namespace",
        }

        self.podStruct = {
        "label" : "application",
        "title" : "",
        "namespace" : "raw_object.metadata.namespace",
        "p_type" : "type",
        "name" : "raw_object.metadata.name",
        "kind" : "raw_object.kind",
        "host" : "raw_object.spec.nodeName",
        "hostIp" : "raw_object.status.hostIP",
        "ip" : "raw_object.status.hostIP",
        "overlayIP" : "raw_object.status.podIP",
        "info_restartPolicy" : "raw_object.spec.restartPolicy",
        "info_restartCount" : "raw_object.status.containerStatuses[0].restartCount",
        "info_started" : "raw_object.status.containerStatuses[0].started",
        "dockerimage" : "raw_object.status.containerStatuses[0].image",
        "monitor_status" : "raw_object.status.containerStatuses[0].state",
        "monitor_message" : "",
        "monitor_cpu" : "",
        "monitor_memory" : "",
        "monitor_downtime" : "",
        "parent" : "raw_object.spec.nodeName",
        "procParent" : "raw_object.status",
        "events" : "",
        "ticket_assignee" : "",
        "ticket_status" : "",
        "ticket_number" : 0,
        "automation_isEnabled" : False,
        "automation_vault" : "",
        "automation_workflow" : {},
        "contact_email" : "",
        "contact_sms" : "",
        }

        self.nagiosStruct = {
        "layer" : "_NEO4j_layer",
        "title" : "_NEO4j_title",
        "name" : "_NEO4j_title",
        "kind" : "_NEO4j_type",
        "type" : "_NEO4j_type",
        "host" : "_NEO4j_title",
        "hostIp" : "_NEO4j_address",
        "ip" : "_NEO4j_address",
        "overlayIP": "NA",
        "info_kernelVersion" : "",
        "info_kubeletVersion" : "NA",
        "info_operatingSystem" : "",
        "info_architecture" : "",
        "info_containerRuntimeVersion" : "NA",
        "info_ephemeral_storage" : "NA",
        "info_hugepages_1Gi" : "NA",
        "info_hugepages_2Mi" : "NA",
        "image" : "_NEO4j_icon",
        "application" : "_NEO4j_application",
        "monitor_status" : "",
        "monitor_message" : "",
        "monitor_cpu" : "",
        "monitor_memory" : "",
        "monitor_downtime" : "",
        "parent" : "_NEO4j_parent",
        "procParent" : "",
        "events" : "",
        "tech" : "_NEO4j_tech",
        "server_type" : "_NEO4j_sertype",
        "ticket_assignee" : "",
        "ticket_status" : "",
        "ticket_number" : 0,
        "automation_isEnabled" : "_NEO4j_Automation",
        "automation_vault" : "_NEO4j_Vault",
        "automation_workflow" : "_NEO4j_WF",
        "contact_email" : "_NEO4j_contactEmail",
        "Friendly_name" : "_NEO4j_FrdlyName",
        "device_type" : "_NEO4j_servertype",
        "Nics_list" : "_NEO4j_nics",
        "DiskVolumes_list" : "_NEO4j_volumes",
        "Phy_Physicalip" : "_NEO4j_PhysicalIp",
        "Phy_Physicalniclink" : "_NEO4j_PhysicalNICLink"
        }

    def convertor(self, kind, payload):
        if kind == 'node':
            self.kubeCommon['image'] = "kubernetes_nodes.png"
            self.kubeCommon["layer"] = "s_sw"
            _struct = self.nodeStruct.copy()
        elif kind == "pod":
            self.kubeCommon['image'] = "kubernetes_pods.png"
            self.kubeCommon["layer"] = "s_sw"
            _struct = self.podStruct.copy()
        elif kind == 'cmap':
            self.kubeCommon["type"] = "configmap"
            self.kubeCommon["layer"] = "s_sw"
            self.kubeCommon["hostIp"] = "0.0.0.0"
            self.kubeCommon["ip"] = "0.0.0.0"
            _struct = self.cmapStruct.copy()
        elif kind == "nagios":
            _struct = self.nagiosStruct.copy()
            del self.kubeCommon['application']

        _p = Cut(payload)
        for k,v in _struct.items():
            if "." in k:
                if not self.retProxy.get(k.split('.')[0]):
                    self.retProxy[k.split('.')[0]] = {}
            if k and v:
                self.retProxy[k] = _p.get(v,"")
            else:
                self.retProxy[k] = v
        self.retProxy.update(self.kubeCommon)
        #----------------------------------------------
        if self.retProxy['kind'] == 'Pod' or self.retProxy['kind'] == 'Node':
            self.retProxy['label'] = "application"
        if 'type' in self.retProxy:
            if self.retProxy['type'] == 'Host':
                self.retProxy['label'] = "hardware"
            elif self.retProxy['type'] == 'HostMS':
                self.retProxy['label'] = "hardware"
            elif self.retProxy['type'] == 'Service':
                self.retProxy['label'] = "software"
            elif self.retProxy['type'] == 'ServiceMS':
                self.retProxy['label'] = "software"
            elif self.retProxy['type'] == 'configmap':
                self.retProxy['label'] = "configmap"
        #self.retProxy['label'] = 'Service'
        #if 'type' in self.retProxy and self.retProxy['type'] == 'Host':
        #    self.retProxy['label'] = 'Host'
        #----------------------------------------------
        return json.dumps(self.retProxy.data)

