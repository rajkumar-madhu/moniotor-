from django.db import models

class VaultModel(models.Model):
    id = models.AutoField(primary_key=True)
    service=models.CharField(max_length=250)
    server=models.CharField(max_length=250)
    user=models.CharField(max_length=250)
    url=models.CharField(max_length=250)
    class Meta:
       db_table= "vault"
