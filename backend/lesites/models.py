from django.db import models
from django.conf import settings

class SiteModel(models.Model):
    id = models.AutoField(primary_key=True)
    sitename = models.TextField()
    location = models.TextField()
    websocket_url = models.TextField()
    entity_host = models.TextField()
    entity_port = models.CharField(max_length=5)
    is_URLSecured = models.BooleanField(default=True)
    # FIXED: TextField should default to a string, not a boolean
    environment = models.TextField(default="")
    analytics_Prefix_URL = models.TextField()
    redis_host = models.TextField()
    redis_port = models.CharField(max_length=5)
    is_enable = models.BooleanField(default=True)
    prometheus_url = models.TextField()
    le_url = models.TextField()
    lat = models.FloatField(default=0)
    lng = models.FloatField(default=0)
    status = models.IntegerField(default=0)
    grafana_api = models.TextField()
    elastic_host = models.TextField()
    elastic_port = models.CharField(max_length=5)
    incident_url = models.TextField()
    incident_api = models.TextField()
    #bod = models.IntegerField(default=0)
    #entity = models.IntegerField(default=0)
    #servers = models.IntegerField(default=0)


    class Meta:
       db_table= "lesite"

class LocationModel(models.Model):
    id = models.AutoField(primary_key=True)
    locationname = models.TextField()

    class Meta:
       db_table= "lelocation"

class CountryModel(models.Model):
    countryid = models.AutoField(primary_key=True) #1
    countryshortname = models.TextField()  #IN
    countryname = models.TextField() #INDIA

    class Meta:
       db_table= "lecountry"

class StateModel(models.Model):
    stateid = models.AutoField(primary_key=True)
    countryid = models.TextField()
    #models.ForeignKey(CountryModel, on_delete=models.CASCADE)
    stateshortname = models.TextField()
    statename = models.TextField()
    lat = models.FloatField()
    lng = models.FloatField()

    class Meta:
       db_table= "lestate"
