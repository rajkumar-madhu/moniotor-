import socket

class letelnet:
    def __init__(self, ip=None, port=None ):
        #print("letel-ip->"+str(ip))
        self.ip = ip
        self.port= port
    def check(self):
        # telnetlib was removed in Python 3.13; this never used real telnet
        # protocol negotiation, just a raw connect + best-effort read, so a
        # plain socket reproduces the exact same behavior.
        try:
            sock = socket.create_connection((self.ip, int(self.port)), timeout=2)
            try:
                sock.sendall(b"Telnet Server\n")
                # Bound the read: HTTP-based targets (e.g. node_exporter on :9100)
                # accept the connection but never send a bare newline, so an
                # unbounded read hangs the gunicorn worker until the ingress
                # returns 502. A successful connect already proves reachability,
                # so a read timeout here is not treated as failure.
                sock.settimeout(3)
                try:
                    sock.recv(1024)
                except socket.timeout:
                    pass
            finally:
                sock.close()
            return True
        except socket.timeout:
            print("Connection timeout. IP or PORT is not reachable")
            return False
        except Exception as e:
            #print("letel-error--->")
            return False
