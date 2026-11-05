#####################
## Author : Ponmuthu M
## Version : 18-01-2020
## Optimized : Context manager + No hidden Node() instantiation (April 2026)
#####################

import pika
import os
import json

# CRITICAL FIX #7 - Remove hidden Node() instantiation
# Node() was being created on every MQ() instantiation causing Neo4j connection leaks
# Now, websocketStats is called only when explicitly needed via dependency injection


class MQ(object):
    """
    RabbitMQ Manager with Context Manager support.
    CRITICAL FIX #6 & #7:
    - Context manager (__enter__/__exit__) ensures connection close
    - No hidden Node() instantiation - prevents Neo4j connection leaks
    """
    
    def __init__(self, host="", port="", virtual_host="/", user="", password=""):
        self.host = host or os.getenv('MQ_HOST', "mq")
        self.port = port or os.getenv('MQ_PORT', 5672)
        self.virtual_host = virtual_host
        self.user = user
        self.password = password
        self.connection = None
        self.channel = None
        # REMOVED: self.node = Node() - This was causing Neo4j connection leaks
        # If websocketStats is needed, pass it as a parameter to publish methods

    def __enter__(self):
        """Context manager entry - establish connection."""
        self._connect()
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        """Context manager exit - always close connection."""
        self.close()
        return False  # Don't suppress exceptions

    def _connect(self):
        """Establish RabbitMQ connection."""
        try:
            credentials = pika.PlainCredentials(self.user, self.password)
            self.connection = pika.BlockingConnection(
                pika.ConnectionParameters(
                    host=self.host,
                    port=self.port,
                    virtual_host=self.virtual_host,
                    credentials=credentials
                )
            )
            self.channel = self.connection.channel()
            return self
        except Exception as ex:
            raise Exception("MQ _connect : Ex = " + str(ex))

    def create_exchange(self, name, exchange_type="fanout", durable=False):
        """Create exchange with proper error handling."""
        try:
            self.channel.exchange_declare(exchange=name, exchange_type=exchange_type, durable=durable)
        except Exception as ex:
            raise Exception("MQ create_exchange : Ex = " + str(ex))

    def publishWithStats(self, exchange='delta_update', routing_key='', message={}, overviewstats=None):
        """
        Publish message with optional overview stats.
        CRITICAL FIX #7: overviewstats must be passed in, not fetched via hidden Node()
        """
        try:
            if overviewstats:
                message['overviewstats'] = overviewstats
            message = json.dumps(message)
            if message:
                self.channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message)
        except Exception as ex:
            # Reconnect once on failure
            self._connect()
            self.channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message)
            raise Exception("MQ publishWithStats : Ex = " + str(ex))

    def publish(self, exchange='delta_update', routing_key='', message=""):
        """Publish message with proper error handling."""
        try:
            if isinstance(message, dict):
                message = json.dumps(message)
            if message:
                self.channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message)
        except Exception as ex:
            # Reconnect once on failure
            self._connect()
            self.channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message)
            raise Exception("MQ publish : Ex = " + str(ex))

    def publish_close(self, exchange='delta_update', routing_key='', message=""):
        """Publish and close connection - use context manager instead."""
        try:
            if message:
                if isinstance(message, dict):
                    message = json.dumps(message)
                self.channel.basic_publish(exchange=exchange, routing_key=routing_key, body=message)
            self.close()
        except Exception as ex:
            raise Exception("MQ publish_close : Ex = " + str(ex))

    def close(self):
        """Close RabbitMQ connection properly."""
        try:
            if self.connection and self.connection.is_open:
                self.connection.close()
        except Exception as ex:
            # Log but don't raise - connection might already be closed
            pass
