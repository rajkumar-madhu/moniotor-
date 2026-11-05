from django.db import models
from django.contrib.auth.models import Group, User

# Create your models here.

class OnboardingModel(models.Model):
   id = models.AutoField(primary_key=True)
   ipaddress = models.TextField()
   hostname = models.TextField()
   servicename = models.TextField()
   json = models.TextField()

   class Meta:
       db_table= "onboard"