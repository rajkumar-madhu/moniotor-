from django.db import models

class ApplicationModel(models.Model):
    id = models.AutoField(primary_key=True)
    applicationname = models.TextField()

    class Meta:
       db_table= "application"
