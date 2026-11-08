from django.apps import apps

from administration import services as admin_services
from architecture.models import Link
from core.models import Site

from .fixtures import Base, link


class SiteDecisionTests(Base):
    """Link and Placement carry no site name of their own; they follow their devices. This pins that decision."""

    def test_no_site_name_field_on_architecture_models(self):
        for model in apps.get_app_config("architecture").get_models():
            names = [f.name for f in model._meta.concrete_fields]
            self.assertNotIn("site", names, model)
            self.assertNotIn("site_key", names, model)
        listed = {(a, m) for a, m, _f, _l in admin_services.SITE_NAME_FIELDS}
        self.assertIn(("network", "Device"), listed)           # the site name the links follow
        self.assertNotIn(("architecture", "Link"), listed)

    def test_site_rename_keeps_links_visible_through_their_devices(self):
        l = link(self.spine, self.leaf, "a", "b")
        admin_services.rename_site_references("Mumbai", "Pune")
        Site.objects.filter(name="Mumbai").update(name="Pune")
        from architecture import services
        from network.models import Device
        self.assertEqual(Device.objects.get(pk=self.spine.pk).site, "Pune")
        self.assertEqual(services.links_queryset(self.admin).filter(pk=l.pk).count(), 1)

    def test_link_with_a_global_device_is_visible_to_site_users(self):
        from network.models import Device
        Device.objects.filter(pk=self.leaf.pk).update(site="All Sites")
        l = link(self.spine, self.leaf, "a", "b")
        from architecture import services
        self.assertTrue(services.links_queryset(self.mum_viewer).filter(pk=l.pk).exists())
        self.assertTrue(services.links_queryset(self.lon_op).filter(pk=l.pk).exists() is False)    # the Mumbai end is not theirs
        self.assertFalse(services.can_manage_link(self.mum_op, l))          # a global device needs a tenant-wide role
        self.assertTrue(services.can_manage_link(self.operator, l))
