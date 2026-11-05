from django.db import models
from django.contrib.auth.models import Group, User
# Create your models here.


class SnmpModel(models.Model):
   id = models.AutoField(primary_key=True)
   protocol = models.CharField(max_length=50)
   ipaddress = models.CharField(max_length=20)
   username = models.CharField(max_length=50, default="NONE")
   model = models.CharField(max_length=50)
   communitystring = models.CharField(max_length=50, default="NONE")
   securitylevel = models.CharField(max_length=50, default="NONE")
   authenticationmethod = models.CharField(max_length=50, default="NONE")
   authenticationpassword = models.CharField(max_length=50, default="NONE")
   privacymethod = models.CharField(max_length=50, default="NONE")
   privacypassword = models.CharField(max_length=50, default="NONE")
   threshold = models.TextField(null=True)
   #onboardedOn = models.DateTimeField(auto_now_add=True, blank=True)
   
   class Meta:
       db_table= "snmp"