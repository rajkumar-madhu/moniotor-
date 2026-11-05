"""
LinkedEye Celery consumer.
FIXED: print() replaced with logging.
"""

from celery import Celery
from celery import bootsteps
from kombu import Consumer, Exchange, Queue
import os
import logging

logger = logging.getLogger('linkedeye.celery')

exchange = Exchange(name="delta_update", type="fanout", durable=False)
my_queue = Queue(name='', exchange=exchange, message_ttl=59.0, auto_delete=True, exclusive=True)
broker = str('amqp://linkedeye:linkedeye@' + os.getenv('MQ_SERVICE_HOST', '') + ':' + os.getenv('MQ_SERVICE_PORT_5672', '') + '/')
app = Celery(broker=broker)


class MyConsumerStep(bootsteps.ConsumerStep):

    def get_consumers(self, channel):
        return [Consumer(channel,
                         queues=[my_queue],
                         callbacks=[self.handle_message],
                         accept=['json'])]

    def handle_message(self, body, message):
        message.ack()
        logger.info('Received message: %r', body)


app.steps['consumer'].add(MyConsumerStep)
