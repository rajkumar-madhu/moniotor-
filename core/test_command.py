"""Command Centre (core/command.py): real, site-scoped figures; empty and not-connected states instead of numbers."""
from django.contrib.auth.models import User
from django.test import TestCase
from django.urls import reverse
from django.utils import timezone

from core import command
from core.events import emit
from core.models import Membership, Site
from dcim.models import CoolingUnit, PowerFeed, Rack, RackItem, Room
from servicedesk.models import Ticket
from telemetry.models import Alert


class CommandCentreTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.mum = Site.objects.create(name="Mumbai", code="mum")
        cls.lon = Site.objects.create(name="London", code="lon")
        cls.admin = User.objects.create_superuser("root", "r@example.com", "pw-Strong-12345")
        cls.mum_viewer = User.objects.create_user("mumview", password="pw-Strong-12345")
        Membership.objects.create(user=cls.mum_viewer, site=cls.mum, role="viewer")

        room_m = Room.objects.create(site="Mumbai", name="Hall M")
        room_l = Room.objects.create(site="London", name="Hall L")
        # M01: 3.6 kW of a 4 kW budget (90 %) and a 5 kW cooling budget; L01: 1 kW of 4 kW
        cls.rack_m = Rack.objects.create(room=room_m, name="M01", u_height=42, max_power_kw=4.0, max_cooling_kw=5.0)
        cls.rack_l = Rack.objects.create(room=room_l, name="L01", u_height=42, max_power_kw=4.0)
        RackItem.objects.create(rack=cls.rack_m, name="srv-m", u_position=10, u_height=2, face="front")
        PowerFeed.objects.create(rack=cls.rack_m, name="PDU-A", capacity_kw=5.0, measured_kw=3.6)
        PowerFeed.objects.create(rack=cls.rack_l, name="PDU-A", capacity_kw=5.0, measured_kw=1.0)
        CoolingUnit.objects.create(room=room_m, name="CRAH-1", capacity_kw=3.0)       # 3.6 kW load > 3 kW: short
        CoolingUnit.objects.create(room=room_l, name="CRAH-L", capacity_kw=10.0)

        Ticket.objects.create(kind="incident", title="Mumbai core switch flapping", priority="P1", site="Mumbai")
        Ticket.objects.create(kind="incident", title="London VPN slow", priority="P3", site="London")
        now = timezone.now()
        Alert.objects.create(fingerprint="a-mum", alertname="NodeDownMumbai", severity="critical", status="firing",
                             site="Mumbai", starts_at=now)
        Alert.objects.create(fingerprint="a-lon", alertname="DiskFullLondon", severity="critical", status="firing",
                             site="London", starts_at=now)
        emit(source="dcim", title="M01 above 90% of its power budget", severity="warning", site="Mumbai")
        emit(source="dcim", title="Hall L cooling restored", severity="ok", site="London")

    def get(self, user, **params):
        self.client.force_login(user)
        return self.client.get(reverse("command"), params)

    def test_page_renders_every_section(self):
        r = self.get(self.admin)
        self.assertEqual(r.status_code, 200)
        for heading in ("Command Centre", "Estate signals", "Power vs cooling", "Power / cooling / space utilisation",
                        "Active incidents", "Critical alerts", "Live telemetry", "Active monitors",
                        "Live event stream"):
            self.assertContains(r, heading)
        self.assertContains(r, 'href="/command/"')            # sidebar entry

    def test_admin_figures_come_from_the_database(self):
        ctx = command.context(self.admin)
        self.assertEqual(ctx["alerts"], {"firing": 2, "critical": 2})
        self.assertEqual(ctx["incident_counts"], {"open": 2, "p12": 1})
        dc = ctx["dc"]
        self.assertEqual(dc["rack_count"], 2)
        self.assertAlmostEqual(dc["t"]["load_kw"], 4.6)
        self.assertEqual(dc["racks"][0].name, "M01")             # worst first
        self.assertEqual(dc["racks"][0].power_tone, "crit")      # 90 % of budget
        self.assertEqual(dc["risk"], 1)
        headroom = next(t for t in ctx["tiles"] if t["key"] == "headroom")
        self.assertEqual((headroom["note"], headroom["tone"]), ("dcim · 1 room short", "warn"))   # total still positive
        monitors = {m["name"]: m for m in ctx["monitors"]}
        self.assertEqual(monitors["Rack power ≥ 90% of budget"]["value"], 1)
        self.assertEqual(monitors["Room cooling short"]["value"], 1)
        self.assertEqual(monitors["Critical alerts firing"]["value"], 2)
        self.assertEqual([e.title for e in ctx["stream"]][:2],
                         ["Hall L cooling restored", "M01 above 90% of its power budget"])

    def test_site_viewer_sees_only_their_site(self):
        r = self.get(self.mum_viewer)
        self.assertEqual(r.status_code, 200)
        html = r.content.decode()
        for visible in ("M01", "Mumbai core switch flapping", "NodeDownMumbai", "M01 above 90% of its power budget"):
            self.assertIn(visible, html)
        for hidden in ("L01", "London VPN slow", "DiskFullLondon", "Hall L cooling restored"):
            self.assertNotIn(hidden, html)
        ctx = command.context(self.mum_viewer)
        self.assertEqual(ctx["alerts"]["critical"], 1)
        self.assertEqual(ctx["incident_counts"]["open"], 1)
        self.assertEqual(ctx["dc"]["rack_count"], 1)

    def test_site_parameter_narrows_and_a_hidden_site_shows_nothing(self):
        ctx = command.context(self.admin, "London")
        self.assertEqual([r.name for r in ctx["dc"]["racks"]], ["L01"])
        self.assertEqual(ctx["incident_counts"]["open"], 1)
        hidden = command.context(self.mum_viewer, "London")
        self.assertEqual(hidden["dc"]["rack_count"], 0)
        self.assertEqual(hidden["incident_counts"]["open"], 0)
        self.assertEqual(hidden["alerts"]["firing"], 0)
        self.assertEqual(hidden["stream"], [])
        r = self.get(self.mum_viewer, site="London")
        self.assertNotContains(r, "DiskFullLondon")
        self.assertNotContains(r, "L01")

    def test_hidden_site_also_empties_the_health_chip(self):
        from urlchecker.models import UrlCheck
        UrlCheck.objects.create(name="lon-web", url="https://lon.example.com/", site="London", last_status="online")
        self.assertEqual(command.context(self.admin, "London")["health"]["total"], 1)
        self.assertEqual(command.context(self.mum_viewer, "London")["health"]["total"], 0)

    def test_client_filter_narrows_every_block_to_the_clients_sites(self):
        from core.models import Client as LeClient
        acme = LeClient.objects.create(name="Acme", code="acme")
        Site.objects.filter(name="London").update(client=acme)
        ctx = command.context(self.admin, client=str(acme.pk))
        self.assertEqual([r.name for r in ctx["dc"]["racks"]], ["L01"])
        self.assertEqual(ctx["incident_counts"]["open"], 1)
        self.assertEqual(ctx["alerts"]["firing"], 1)
        self.assertEqual([e.title for e in ctx["stream"]], ["Hall L cooling restored"])
        self.assertEqual(ctx["names"], ["London"])
        # the shell's session filter reaches the page
        self.client.force_login(self.admin)
        session = self.client.session
        session["le_client"] = str(acme.pk)
        session.save()
        r = self.client.get(reverse("command"))
        self.assertContains(r, "DiskFullLondon")
        self.assertNotContains(r, "NodeDownMumbai")

    def test_gpu_block_is_not_connected_rather_than_a_number(self):
        ctx = command.context(self.admin)
        gpu = next(t for t in ctx["tiles"] if t["key"] == "gpus")
        self.assertTrue(gpu["missing"])
        self.assertEqual(gpu["value"], "–")
        self.assertContains(self.get(self.admin), "Not connected · no DCGM source")

    def test_hardware_telemetry_uses_bmc_sensors_and_history(self):
        from datetime import timedelta

        from hardware.models import BmcEndpoint, HwComponent, HwReading
        from network.models import Device
        now = timezone.now()
        for site, reading, status in (("Mumbai", 40.0, "ok"), ("London", 80.0, "critical")):
            dev = Device.objects.create(name=f"srv-{site}", device_type="physical_server", site=site,
                                        mgmt_ip="198.51.100.10")
            ep = BmcEndpoint.objects.create(device=dev, host="192.0.2.50", port=443, verify_mode="pin",
                                            pinned_sha256="a" * 64, health="healthy" if status == "ok" else "critical",
                                            last_ok_at=now)
            comp = HwComponent.objects.create(endpoint=ep, kind="temperature", key="inlet", name="Inlet",
                                              reading=reading, unit="Cel", status=status)
            HwReading.objects.bulk_create([HwReading(tenant_key=ep.tenant_key, component=comp,
                                                     ts=now - timedelta(hours=h), value=reading - h) for h in range(4)])
        tiles = {t["label"]: t for t in command.context(self.admin)["telemetry"]["tiles"]}
        self.assertEqual(tiles["Hardware temp"]["value"], "60.0 °C")
        self.assertEqual(tiles["BMC health"]["value"], "1/2")
        self.assertEqual(tiles["BMC health"]["tone"], "crit")
        ctx = command.context(self.mum_viewer)
        mum = {t["label"]: t for t in ctx["telemetry"]["tiles"]}
        self.assertEqual(mum["Hardware temp"]["value"], "40.0 °C")       # London's 80 °C sensor is out of scope
        self.assertEqual(mum["BMC health"]["value"], "1/1")
        self.assertTrue(ctx["telemetry"]["fresh"])
        chart = ctx["telemetry"]["chart"]
        self.assertGreaterEqual(len(chart["labels"]), 2)
        self.assertLessEqual(max(chart["series"][1]["values"]), 40.0)
        sensors = next(m for m in ctx["monitors"] if m["name"] == "BMC sensors outside thresholds")
        self.assertEqual(sensors["value"], 0)
        self.assertContains(self.get(self.admin), 'data-src="cc-temp"')

    def test_servers_count_inventory_and_say_how_many_are_monitored(self):
        from network.models import Device
        Device.objects.create(name="srv-a", device_type="physical_server", site="Mumbai", enabled=True, status="up")
        Device.objects.create(name="srv-b", device_type="physical_server", site="Mumbai", enabled=True, status="unknown")
        Device.objects.create(name="srv-c", device_type="physical_server", site="Mumbai", enabled=False)
        tile = next(t for t in command.context(self.admin)["tiles"] if t["key"] == "servers")
        self.assertEqual(tile["value"], 3)
        self.assertEqual(tile["note"], "compute · 2 monitored")        # one unknown: not "all up"
        Device.objects.filter(name="srv-b").update(status="down")
        tile = next(t for t in command.context(self.admin)["tiles"] if t["key"] == "servers")
        self.assertEqual((tile["note"], tile["tone"]), ("compute · 1 of 2 monitored down", "crit"))

    def test_example_bmc_data_never_lights_the_live_badge(self):
        from hardware.models import BmcEndpoint
        from network.models import Device
        dev = Device.objects.create(name="example-srv", device_type="physical_server", site="Mumbai")
        BmcEndpoint.objects.create(device=dev, host="192.0.2.50", port=443, verify_mode="pin", pinned_sha256="a" * 64,
                                   health="healthy", last_ok_at=timezone.now(), is_example=True)
        self.assertFalse(command.context(self.admin)["telemetry"]["fresh"])

    def test_empty_estate_renders_empty_states(self):
        Rack.objects.all().delete()
        Room.objects.all().delete()
        Ticket.objects.all().delete()
        Alert.objects.all().delete()
        r = self.get(self.admin)
        self.assertEqual(r.status_code, 200)
        self.assertContains(r, "No racks in service")
        self.assertContains(r, "No open incidents.")
        self.assertContains(r, "No alerts firing.")
        self.assertNotContains(r, 'data-src="cc-power"')

    def test_anonymous_user_is_sent_to_sign_in(self):
        r = self.client.get(reverse("command"))
        self.assertEqual(r.status_code, 302)
        self.assertIn(reverse("login"), r["Location"])
