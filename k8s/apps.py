from django.apps import AppConfig


class K8sConfig(AppConfig):
    name = "k8s"
    label = "k8s"
    verbose_name = "Kubernetes inventory"
    default_auto_field = "django.db.models.BigAutoField"

    def ready(self):
        # Importing the sheet package registers every workbook sheet with k8s.importer, so the
        # management command, the upload page and the tests all see the same registry.
        from . import importer
        importer.load_sheets()
