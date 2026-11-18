import http.client
import json
import queue
import threading
import unittest
import xml.etree.ElementTree as ET

from server import AppHandler, AppServer, ClimateGateway, SerialMonitor, extract_numeric_values
from taphome_gateway import TapHomeGateway


SAMPLE_DEVICE = {
    "ip": "192.168.1.73",
    "port": 7000,
    "mac": "aabbcc112233",
    "name": "Gree klíma",
    "brand": "gree",
    "model": "gree",
    "version": "1.0",
    "mid": None,
}


class FakeClimateGateway:
    def __init__(self):
        self.devices = []
        self.changes = []
        self.query_count = 0
        self.state = {
            "power": True,
            "mode": {"value": 1, "name": "cool"},
            "currentTemperature": 23.5,
            "targetTemperature": 21,
            "temperatureUnit": "°C",
            "fanSpeed": {"value": 3, "name": "medium"},
            "horizontalSwing": {"value": 1, "name": "fullswing"},
            "verticalSwing": {"value": 1, "name": "fullswing"},
            "freshAir": False,
            "quiet": False,
            "turbo": False,
            "sleep": False,
            "light": True,
            "xfan": False,
            "anion": False,
            "currentHumidity": 48,
            "targetHumidity": None,
            "cleanFilter": False,
        }

    def overview(self):
        return {"available": True, "configuredTarget": None, "lastScanAt": None, "devices": self.devices}

    def scan(self, target=None):
        self.devices = [SAMPLE_DEVICE]
        return {"available": True, "target": target, "lastScanAt": "2026-11-18T10:00:00Z", "devices": self.devices}

    def query(self, identifier=None):
        if identifier != SAMPLE_DEVICE["mac"]:
            raise ValueError("Ismeretlen klíma")
        self.query_count += 1
        return {
            "device": SAMPLE_DEVICE,
            "updatedAt": "2026-11-18T10:00:01Z",
            "firmware": "3.31",
            "state": self.state,
            "raw": {"Pow": 1},
        }

    def control(self, changes, identifier=None):
        if identifier != SAMPLE_DEVICE["mac"]:
            raise ValueError("Ismeretlen klíma")
        self.changes.append(changes)
        for key, value in changes.items():
            if key == "mode":
                self.state["mode"] = {"value": value, "name": "changed"}
            elif key == "fanSpeed":
                self.state["fanSpeed"] = {"value": value, "name": "changed"}
            elif key == "horizontalSwing":
                self.state["horizontalSwing"] = {"value": value, "name": "changed"}
            elif key == "verticalSwing":
                self.state["verticalSwing"] = {"value": value, "name": "changed"}
            else:
                self.state[key] = value
        return self.query(identifier)


class NumericExtractionTests(unittest.TestCase):
    def test_extracts_flat_json_numbers(self):
        self.assertEqual(
            extract_numeric_values('{"temperature":23.4,"humidity":48,"ok":true}'),
            {"temperature": 23.4, "humidity": 48.0},
        )

    def test_extracts_plain_text_numbers(self):
        self.assertEqual(
            extract_numeric_values("T=-2.5 H=41 1e2"),
            {"érték 1": -2.5, "érték 2": 41.0, "érték 3": 100.0},
        )

    def test_empty_payload_has_no_values(self):
        self.assertEqual(extract_numeric_values("\r\n"), {})


class MonitorTests(unittest.TestCase):
    def test_demo_emits_chartable_reading(self):
        monitor = SerialMonitor()
        client = monitor.subscribe()
        try:
            monitor.start_demo()
            reading = None
            for _ in range(4):
                event = client.get(timeout=1.5)
                if event["type"] == "reading":
                    reading = event
                    break
            self.assertIsNotNone(reading)
            self.assertIn("temperature", reading["values"])
            self.assertGreater(reading["byteLength"], 0)
        finally:
            monitor.disconnect()
            monitor.unsubscribe(client)


class ClimateGatewayTests(unittest.TestCase):
    def test_accepts_valid_ipv4_target(self):
        self.assertEqual(ClimateGateway._validate_target("192.168.1.73"), "192.168.1.73")

    def test_rejects_invalid_target(self):
        with self.assertRaisesRegex(ValueError, "IPv4"):
            ClimateGateway._validate_target("gree.local")


class TapHomeGatewayTests(unittest.TestCase):
    def setUp(self):
        self.climate = FakeClimateGateway()
        self.climate.devices = [SAMPLE_DEVICE]
        self.gateway = TapHomeGateway(self.climate, device_identifier=SAMPLE_DEVICE["mac"])

    def test_exposes_semantic_values_without_modbus_registers(self):
        state = self.gateway.read()
        self.assertTrue(state["power"])
        self.assertEqual(state["mode"], "cool")
        self.assertEqual(state["currentTemperature"], 23.5)
        self.assertEqual(state["fanSpeed"], "medium")
        self.assertNotIn("holdingRegisters", state)

    def test_translates_semantic_write_to_gree_control(self):
        state = self.gateway.control({"targetTemperature": 24})
        self.assertEqual(self.climate.changes[-1], {"targetTemperature": 24})
        self.assertEqual(state["targetTemperature"], 24)

    def test_normalizes_fahrenheit_for_taphome_thermostat(self):
        self.climate.state["temperatureUnit"] = "°F"
        self.climate.state["currentTemperature"] = 74.3
        self.climate.state["targetTemperature"] = 69.8
        state = self.gateway.read()
        self.assertEqual(state["temperatureUnit"], "C")
        self.assertEqual(state["currentTemperature"], 23.5)
        self.assertEqual(state["targetTemperature"], 21.0)

    def test_translates_named_packet_parser_values(self):
        state = self.gateway.control(
            {"mode": "heat", "fanSpeed": "mediumHigh", "verticalSwing": "fixedMiddle"}
        )
        self.assertEqual(
            self.climate.changes[-1],
            {"mode": 4, "fanSpeed": 4, "verticalSwing": 4},
        )
        self.assertEqual(state["targetTemperature"], 21)

    def test_rejects_raw_register_payload(self):
        with self.assertRaisesRegex(ValueError, "Nem támogatott TapHome mező"):
            self.gateway.control({"register": 20, "value": 22})

    def test_caches_repeated_packet_parser_reads(self):
        self.gateway.read()
        self.gateway.read()
        self.assertEqual(self.climate.query_count, 1)


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.monitor = SerialMonitor()
        self.climate = FakeClimateGateway()
        self.climate.devices = [SAMPLE_DEVICE]
        self.taphome = TapHomeGateway(self.climate, device_identifier=SAMPLE_DEVICE["mac"])
        self.server = AppServer(("127.0.0.1", 0), AppHandler, self.monitor, self.climate, self.taphome)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.port = self.server.server_address[1]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.monitor.disconnect()
        self.thread.join(timeout=2)

    def request(self, method, path, body=None):
        connection = http.client.HTTPConnection("127.0.0.1", self.port, timeout=2)
        headers = {"Content-Type": "application/json"} if body is not None else {}
        connection.request(method, path, body=body, headers=headers)
        response = connection.getresponse()
        payload = response.read()
        connection.close()
        return response.status, response.getheader("Content-Type"), payload

    def test_status_endpoint(self):
        status, content_type, payload = self.request("GET", "/api/status")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/json"))
        self.assertFalse(json.loads(payload)["connected"])

    def test_index_is_served(self):
        status, content_type, payload = self.request("GET", "/")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("text/html"))
        self.assertIn(b"Gree / Local Monitor", payload)

    def test_demo_endpoint(self):
        status, _, payload = self.request("POST", "/api/demo", "{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["mode"], "demo")

    def test_rejects_directory_traversal(self):
        status, _, _ = self.request("GET", "/..%2Fserver.py")
        self.assertEqual(status, 404)

    def test_climate_scan_and_status_endpoints(self):
        status, _, payload = self.request("POST", "/api/ac/scan", '{"target":"192.168.1.73"}')
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["devices"][0]["ip"], "192.168.1.73")

        status, _, payload = self.request("GET", "/api/ac/status?device=aabbcc112233")
        self.assertEqual(status, 200)
        response = json.loads(payload)
        self.assertTrue(response["state"]["power"])
        self.assertEqual(response["state"]["currentTemperature"], 23.5)

    def test_taphome_gree_ac_http_gateway(self):
        status, _, payload = self.request("GET", "/api/taphome/ac")
        self.assertEqual(status, 200)
        response = json.loads(payload)
        self.assertEqual(response["currentTemperature"], 23.5)
        self.assertEqual(response["mode"], "cool")
        self.assertNotIn("holdingRegisters", response)

        status, _, payload = self.request(
            "POST", "/api/taphome/ac", '{"power":false,"mode":"heat"}'
        )
        self.assertEqual(status, 200)
        response = json.loads(payload)
        self.assertFalse(response["power"])
        self.assertEqual(self.climate.changes[-1], {"power": False, "mode": 4})

    def test_taphome_rejects_modbus_register_payload(self):
        status, _, payload = self.request(
            "POST", "/api/taphome/ac", '{"register":20,"value":22}'
        )
        self.assertEqual(status, 400)
        self.assertIn("Nem támogatott TapHome mező", json.loads(payload)["error"])

    def test_serves_valid_packet_parser_template(self):
        status, content_type, payload = self.request("GET", "/api/taphome/template.xml")
        self.assertEqual(status, 200)
        self.assertTrue(content_type.startswith("application/xml"))
        root = ET.fromstring(payload)
        self.assertEqual(root.tag, "Templates")
        self.assertIn(b"PacketParserThermostat", payload)
        self.assertIn(b"/api/taphome/ac", payload)


if __name__ == "__main__":
    unittest.main()
