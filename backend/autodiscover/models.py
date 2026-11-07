from django.db import models


class AutoDiscoveryModel(models.Model):
   id = models.AutoField(primary_key=True)
   ipaddress = models.CharField(max_length = 50)
   subnet = models.CharField(max_length = 50)
   ostype = models.CharField(max_length = 50)
   class Meta:
       db_table= "autodiscover"
