from django.db import models
from django.conf import settings

# Create your models here.
class portconnectionModel(models.Model):
   id = models.AutoField(primary_key=True)
   source_modal = models.TextField()
   source_ip = models.TextField()
   source_port = models.TextField()
   source_name = models.TextField()
   destination_modal = models.TextField()
   destination_ip = models.TextField()
   destination_port = models.TextField()
   destination_name = models.TextField()

   class Meta:
       db_table= "port_connection"