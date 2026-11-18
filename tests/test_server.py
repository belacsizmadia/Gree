import http.client
import json
import queue
import threading
import unittest

from server import AppHandler, AppServer, ClimateGateway, SerialMonitor, extract_numeric_values


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
            "state": {
                "power": True,
                "mode": {"value": 1, "name": "cool"},
                "currentTemperature": 23,
                "targetTemperature": 21,
            },
            "raw": {"Pow": 1},
        }


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


class ApiTests(unittest.TestCase):
    def setUp(self):
        self.monitor = SerialMonitor()
        self.climate = FakeClimateGateway()
        self.server = AppServer(("127.0.0.1", 0), AppHandler, self.monitor, self.climate)
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
        self.assertEqual(response["state"]["currentTemperature"], 23)


if __name__ == "__main__":
    unittest.main()
