"""Product catalog → device image URL resolution."""
from django.test import TestCase, override_settings

from .catalog import resolve_product_urls
from .models import NetworkProductImage

T = "default"


@override_settings(LINKEDEYE_TENANT=T)
class ProductCatalogTests(TestCase):
    def setUp(self):
        NetworkProductImage.objects.create(
            tenant_key=T,
            vendor="Arista",
            device_type="switch",
            model_key="",
            product_image_url="https://cdn.example/arista-default.webp",
            product_page_url="https://example.com/arista",
        )
        NetworkProductImage.objects.create(
            tenant_key=T,
            vendor="Arista",
            device_type="switch",
            model_key="DCS-7280",
            product_image_url="https://cdn.example/7280.webp",
            product_page_url="https://example.com/7280",
        )
        NetworkProductImage.objects.create(
            tenant_key=T,
            vendor="Arista",
            device_type="",
            model_key="",
            product_image_url="https://cdn.example/arista-any.webp",
            product_page_url="",
        )

    def test_exact_model_wins(self):
        img, page = resolve_product_urls(
            tenant_key=T, device_type="switch", vendor="Arista", model="DCS-7280SR",
        )
        self.assertEqual(img, "https://cdn.example/7280.webp")
        self.assertEqual(page, "https://example.com/7280")

    def test_vendor_default_when_no_model_match(self):
        img, page = resolve_product_urls(
            tenant_key=T, device_type="switch", vendor="Arista", model="Unknown-9000",
        )
        self.assertEqual(img, "https://cdn.example/arista-default.webp")

    def test_any_type_row(self):
        img, _ = resolve_product_urls(
            tenant_key=T, device_type="router", vendor="Arista", model="x",
        )
        self.assertEqual(img, "https://cdn.example/arista-any.webp")
