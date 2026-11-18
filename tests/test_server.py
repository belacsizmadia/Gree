import http.client
import json
import queue
import socket
import threading
import time
import unittest

from server import AppHandler, AppServer, ClimateGateway, SerialMonitor, extract_numeric_values
from taphome_gateway import TapHomeGateway

try:
    from pymodbus.client import ModbusTcpClient
except ImportError:
    ModbusTcpClient = None


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
        self.state = {
            "power": True,
            "mode": {"value": 1, "name": "cool"},
            "currentTemperature": 23.5,
            "targetTemperature": 21,
            "temperatureUnit": "°C",
            "fanSpeed": {"value": 3, "name": "medium"},
            "horizontalSwing": {"value": 1, "name": "fullswing"},
            "verticalSwing": {"value": 1, "name": "fullswing"},
            "turbo": False,
            "sleep": False,
            "light": True,
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

    def test_maps_gree_state_to_official_template_registers(self):
        status = self.gateway.sync_now()
        registers = status["holdingRegisters"]
        self.assertEqual(registers["2"], 170)
        self.assertEqual(registers["4"], 235)
        self.assertEqual(registers["17"], 1)
        self.assertEqual(registers["19"], 3)
        self.assertEqual(registers["20"], 21)

    def test_translates_template_write_to_gree_control(self):
        status = self.gateway.apply_register_write(20, 24)
        self.assertEqual(self.climate.changes[-1], {"targetTemperature": 24})
        self.assertEqual(status["holdingRegisters"]["20"], 24)

    def test_normalizes_fahrenheit_state_to_template_celsius(self):
        self.climate.state["temperatureUnit"] = "°F"
        self.climate.state["currentTemperature"] = 74.3
        self.climate.state["targetTemperature"] = 69.8
        registers = self.gateway.sync_now()["holdingRegisters"]
        self.assertEqual(registers["4"], 235)
        self.assertEqual(registers["20"], 21)

    def test_rejects_read_only_register_write(self):
        with self.assertRaisesRegex(ValueError, "nem írható"):
            self.gateway.apply_register_write(4, 220)

    @unittest.skipIf(ModbusTcpClient is None, "pymodbus nincs telepítve")
    def test_serves_and_accepts_official_modbus_registers(self):
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        gateway = TapHomeGateway(
            self.climate,
            enabled=True,
            host="127.0.0.1",
            port=port,
            slave_id=1,
            device_identifier=SAMPLE_DEVICE["mac"],
            poll_interval=60,
        )
        client = ModbusTcpClient("127.0.0.1", port=port, timeout=2)
        try:
            gateway.start()
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and not (
                gateway.status()["running"] and gateway.status()["connected"]
            ):
                time.sleep(0.05)
            self.assertTrue(client.connect())
            response = client.read_holding_registers(2, count=1, device_id=1)
            self.assertFalse(response.isError())
            self.assertEqual(response.registers, [170])

            response = client.write_register(20, 24, device_id=1)
            self.assertFalse(response.isError())
            deadline = time.monotonic() + 3
            while time.monotonic() < deadline and {"targetTemperature": 24} not in self.climate.changes:
                time.sleep(0.05)
            self.assertIn({"targetTemperature": 24}, self.climate.changes)
        finally:
            client.close()
            gateway.stop()


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
        status, _, payload = self.request("POST", "/api/taphome/sync", "{}")
        self.assertEqual(status, 200)
        self.assertEqual(json.loads(payload)["holdingRegisters"]["4"], 235)

        status, _, payload = self.request(
            "POST", "/api/taphome/gree-ac", '{"register":2,"value":85}'
        )
        self.assertEqual(status, 200)
        response = json.loads(payload)
        self.assertEqual(response["holdingRegisters"]["2"], 85)
        self.assertEqual(self.climate.changes[-1], {"power": False})


if __name__ == "__main__":
    unittest.main()
