from django.db import models

class ServerstypesModel(models.Model):
    id = models.AutoField(primary_key=True)
    types=models.CharField(max_length=250)

    class Meta:
       db_table= "serverstypes"
       
class ServersosModel(models.Model):
    id = models.AutoField(primary_key=True)
    os=models.CharField(max_length=250)

    class Meta:
       db_table= "serversos"

class ServerssoftwareModel(models.Model):
    id = models.AutoField(primary_key=True)
    software=models.CharField(max_length=250)

    class Meta:
       db_table= "serverssoftware"
       
class ServersnicModel(models.Model):
    id = models.AutoField(primary_key=True)
    nic=models.CharField(max_length=250)

    class Meta:
       db_table= "serversnic"

class SwitcheslayersModel(models.Model):
    id = models.AutoField(primary_key=True)
    layers=models.CharField(max_length=250)

    class Meta:
       db_table= "switcheslayers"

class SwitchesmodelModel(models.Model):
    id = models.AutoField(primary_key=True)
    model=models.CharField(max_length=250)

    class Meta:
       db_table= "switchesmodel"

class FirewalltypeModel(models.Model):
    id = models.AutoField(primary_key=True)
    types=models.CharField(max_length=250)
    
    class Meta:
       db_table= "firewallstypes"

class FirewallmodelModel(models.Model):
    id = models.AutoField(primary_key=True)
    model=models.CharField(max_length=250)
    
    class Meta:
       db_table= "firewallsmodel"
       
class RoutertypeModel(models.Model):
    id = models.AutoField(primary_key=True)
    types=models.CharField(max_length=250)
    
    class Meta:
       db_table= "routerstypes"

class RoutermodelModel(models.Model):
    id = models.AutoField(primary_key=True)
    model=models.CharField(max_length=250)
    
    class Meta:
       db_table= "routersmodel"