from LinkedEyeMQ import MQ
import time
import json
import sys

user = sys.argv[1]
password = sys.argv[2]
exch_name = sys.argv[3]
routing_key = sys.argv[4]
message = sys.argv[5]

if isinstance(message, dict):
    msg=message
else:
    msg={'message' : message, 'updatedOn' : int(time.time())} 

conn = MQ(user=user, password=password)
conn.publish(exchange=exch_name, routing_key=routing_key, message=json.dumps(msg))
conn.close()
