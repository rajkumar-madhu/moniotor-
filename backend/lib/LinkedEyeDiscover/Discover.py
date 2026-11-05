#####################
##Author : Ponmuthu M
##Version : 15-02-2020
#####################
# https://0xbharath.github.io/art-of-packet-crafting-with-scapy/network_recon/os_detection/index.html

from scapy.all import *
import socket, struct
import re
import os
import sys
import json
from jinja2 import Template, Environment, FileSystemLoader, meta
from datetime import datetime
from subprocess import call, PIPE, Popen

class Discover(object):
    def __init__(self, debug="False"):
        self.response = {'status' : 400 , 'data' : "" , "error_msg" : ""}
        self.cur_path=os.getcwd()
        self.debug = json.loads(debug.lower())
        self._TTL = {64: "unix", 128: "windows", 254: "solaris", 255: "cisco"}
        self.HSConfig = {'ServerId' : "", "Version" : "", 'RestServer' : {'LinkedEyeJ2Template' : 'RestServer', 'parent' : "" , "data" : [] } , 'FeedServer' : {'LinkedEyeJ2Template' : 'FeedServer', 'parent' : "Websocket" , "data" : [] } , 'RequestProcessor' : {'LinkedEyeJ2Template' : 'RequestProcessor' , 'parent' : "" , "data" : [] }, 'Websocket' : {'LinkedEyeJ2Template' : 'Websocket', 'parent' : "RestServer" ,"data" : [] }}
        self.HSOrder = ['RequestProcessor' , 'FeedServer' , 'Websocket']
        #self.j2Fields = {'CUSTOM_SERVICENAME' : "" , "CUSTOM_PORT" : "" , "COMMON_HOSTNAME" : "" , "COMMON_IPADDRESS" : ""}
        self.j2Fields = {'CUSTOM_SERVICENAME' : "" , "CUSTOM_PORT" : "" ,  "COMMON_IPADDRESS" : ""}
        self.HSScanResult = []

    def _execute(self,cmd):
        process = Popen(cmd, stdout=PIPE, stderr=PIPE,shell=True)
        stdout, stderr = process.communicate()
        return stdout.decode('utf-8'), stderr.decode('utf-8')

    def _osDecider(self, ttl):
        return self._TTL.get(int(ttl), "UNKNOWN")

    def _ttl_w_size(self, disc):
        try:
            # To get TTL
            icmp = IP(dst=disc["ip"]) / ICMP()
            ping_resp = sr1(icmp, timeout=1, verbose=False)
            if ping_resp:
                disc["ttl"] = ping_resp.ttl
            else:
                disc["ttl"] = 0
            if self.debug and ping_resp: ping_resp.show()

            # To get Windows Size
            tcp = IP(dst=disc["ip"]) / TCP()
            disc["win_size"] = tcp.window

            if self.debug and tcp: tcp.show()

            disc["os"] = self._osDecider(disc["ttl"])
            return disc
        except Exception as ex:
            raise Exception(ex)


    def _scan(self, ip):
        try:
            # To get MAC
            arp = Ether(dst="ff:ff:ff:ff:ff:ff") / ARP(pdst=ip)
            answered_list = srp(arp, timeout=1,
                                verbose=self.debug)

            if len(answered_list):
                answered_list = answered_list[0]
            if self.debug: arp.show()

            if len(answered_list):
                return self._ttl_w_size({"ip": answered_list[-1][1].psrc, "mac": answered_list[-1][1].hwsrc})
            else:
                return self._ttl_w_size({"ip": ip})
        except Exception as ex:
            raise Exception(ex)

    def scan(self, start, end):
        try:
            total_list = []
            self.ips_list = [socket.inet_ntoa(struct.pack('>I', i)) for i in
                             range(struct.unpack('>I', socket.inet_aton(start))[0],
                                   struct.unpack('>I', socket.inet_aton(end))[0] + 1)]
            for ip in self.ips_list:
                res = self._scan(ip)
                if res:
                    total_list.append(res)
            self.response['data'] = total_list
            self.response['status'] = 200
            return self.response
            #return total_list
        except Exception as ex:
            #raise Exception("Discover Exception - "+str(ex))
            self.response["status"] = 400
            self.response['error_msg'] = "Discover Exception - "+str(ex)
            return self.response


    def _jinjaComposer(self, j2file, contentdict):
        try:
            variables={}
            fileH =open(j2file,"r")
            templ_str = fileH.read()
            with open(j2file) as file_:
                template = Template(file_.read())
           
            ast = Environment().parse(templ_str)
            fileH.close()
            VariableList = list(meta.find_undeclared_variables(ast))
        
            for var in VariableList:
                if var in contentdict.keys():
                    variables[var] = contentdict[var]
                else:
                    variables[var] = "{{ "+str(var)+" }}"
            return template.render(variables)
        except Exception as ex:
            raise Exception("_jinjaComposer Exception - "+str(ex))


    def _composeHS(self):
        try:
            fileH = open(self.dynamic_template, "w")
            for proc in self.HSOrder:
                j2file = os.path.join(self.j2path, str(self.HSConfig[proc]["LinkedEyeJ2Template"])+str(".j2"))
                for _data in self.HSConfig[proc]["data"]:
                    fileH.write(self._jinjaComposer(j2file, _data))
                    self.HSScanResult.append(_data)
            fileH.close()
            return self.HSScanResult
        except Exception as ex:
            raise Exception("_composeHS Exception - "+str(ex))


    def _remoteExecute(self,ip,cmd,gateway=""):
        try:
            if gateway == "":
                _cmd="sh "+str(os.path.join(self.cur_path,"../script/run_any_command.sh"))+" '/opt/nagios/libexec/check_nrpe -H "+str(ip)+" -c run_command -a "+str(cmd)+"'"
            else:
                _cmd="sh "+str(os.path.join(self.cur_path,"../script/run_any_command.sh"))+" '/opt/nagios/libexec/check_nrpe -H "+str(gateway)+" -c check_nrpe_gateway -a "+str(ip)+" "+str(cmd)+"'"
            return self._execute(_cmd)
        except Exception as ex:
            raise Exception("_remoteExecute Exception - "+str(ex))


    def scanHS(self, ipaddress, configfilepath, mode="DIRECT", GATEWAY=""):
        try:
            #user (mob/web)      <-->    [webseckt  HS-M]   	<-->  [Feeders]
            #user (mob/web)      <-->    [Tomcat  HS-I]   	<-->  [RRS]
            self.port_str = re.compile("port", re.IGNORECASE)
            self.j2path=os.path.join("../template/",str(mode),"SERVICES/Linux/Hypersync")
            self.dynamic_template=os.path.join(self.j2path,"DISCOVER-Hypersync-"+datetime.now().strftime("%d%m%Y-%H-%M-%S")+".j2")
            os.system("mkdir -p "+str(os.path.join(self.j2path,".bkp"))+" && mv "+str(os.path.join(self.j2path,"DISCOVER-Hypersync-*"))+" "+str(os.path.join(self.j2path,".bkp")))
            configfile, resp= self._remoteExecute(ipaddress,"sudo cat "+str(configfilepath), gateway=GATEWAY)
            #hostname, resp= self._remoteExecute(ipaddress,"hostname", gateway=GATEWAY)
 
            if 'NRPE' in configfile:
                raise Exception(configfile)
            for line in configfile.split('\n'):
                j2Fields = self.j2Fields.copy()
                if not line.strip().startswith("#") and line.strip() != '':
                    Configkey = line.rstrip().split('=')[0]
                    Configvalue = line.rstrip().split('=')[1] or None
                    actual_key = [ i for i in self.HSConfig.keys() if i in Configkey]
                    if actual_key == [] or not Configvalue :
                        continue
                    if Configkey in self.HSConfig and isinstance(self.HSConfig[Configkey], str):
                        self.HSConfig[Configkey] = Configvalue
                        continue
                    else:
                        #j2Fields['COMMON_HOSTNAME'] =  hostname.rstrip()
                        j2Fields['COMMON_IPADDRESS'] = ipaddress
                        j2Fields['CUSTOM_SERVICENAME'] = self.port_str.sub("",Configkey)
                        j2Fields['CUSTOM_PORT'] = Configvalue
                    self.HSConfig[actual_key[-1]]['data'].append(j2Fields)
            self.response['data'] = self._composeHS()
            self.response['status'] = 200
            #return self._composeHS() #self.HSConfig
            return self.response
        except Exception as ex:
            #raise Exception("Discover -HS Exception - "+str(ex))
            self.response["status"] = 400
            self.response['error_msg'] = "Discover - HS Exception - "+str(ex)
