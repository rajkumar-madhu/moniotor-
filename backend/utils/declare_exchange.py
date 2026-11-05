from LinkedEyeMQ import MQ
import sys

user = sys.argv[1]
password = sys.argv[2]
exch_name = sys.argv[3]
if len(sys.argv) == 5:
    exchange_type = sys.argv[4]
else:
    exchange_type = "fanout"

conn = MQ(user=user, password=password)
conn.create_exchange(exch_name,exchange_type=exchange_type)
conn.close()
