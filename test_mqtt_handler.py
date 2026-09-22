"""
Tests for mqtt_handler.

Run with: python3 -m unittest test_mqtt_handler -v

Uses only unittest and a stub paho, so it runs on TrueNAS SCALE, where
there is no pip to install a test runner or the real broker library with.

The stub reproduces the two paho behaviours that caused real bugs here:
a publish made before the connection is up is dropped rather than queued,
and connect() resolves the broker's name on the calling thread while
connect_async() does not.
"""

import configparser
import json
import logging
import sys
import types
import unittest


NO_CONN = 4  # paho's MQTT_ERR_NO_CONN


class Result:
    """paho's MQTTMessageInfo, as far as this module uses it."""

    def __init__(self, rc):
        self.rc = rc


class FakeClient:
    """
    Stand-in for paho.mqtt.client.Client.

    Starts disconnected. Publishing while disconnected fails the way paho
    fails: QoS 0 is dropped outright, QoS 1 is queued and reported as sent.
    """

    def __init__(self, *args, **kwargs):
        self.on_connect = None
        self.on_disconnect = None
        self.on_pre_connect = None
        self.callback_api_version = args[0] if args else None
        self.connected = False
        self.loop_running = False
        self.credentials = None
        self.delays = None
        self.connect_args = None
        self.published = []      # (topic, payload, qos, retain), in order
        self.connect_calls = 0
        self.fail_publish_rc = None

    # --- the paho API this module calls -------------------------------
    def username_pw_set(self, username, password):
        self.credentials = (username, password)

    def reconnect_delay_set(self, min_delay, max_delay):
        self.delays = (min_delay, max_delay)

    def connect(self, host, port, keepalive=60):
        # The synchronous call resolves DNS here, which is what failed at
        # boot. Nothing in this module may call it any more.
        raise OSError("[Errno -3] Temporary failure in name resolution")

    def connect_async(self, host, port, keepalive=60):
        if not host:
            raise ValueError("Invalid host.")
        self.connect_args = (host, port, keepalive)

    def loop_start(self):
        self.loop_running = True

    def publish(self, topic, payload, qos=0, retain=False):
        if self.fail_publish_rc is not None:
            return Result(self.fail_publish_rc)
        if not self.connected and qos == 0:
            return Result(NO_CONN)
        self.published.append((topic, payload, qos, retain))
        return Result(0)

    # --- things the network thread would do ---------------------------
    def fire_pre_connect(self):
        self.on_pre_connect(self, None)

    def fire_connack(self, reason=0):
        self.connected = True
        if self.callback_api_version == "v2":
            self.on_connect(self, None, {}, reason, None)
        else:
            self.on_connect(self, None, {}, reason)

    def fire_disconnect(self, reason=0):
        self.connected = False
        if self.callback_api_version == "v2":
            self.on_disconnect(self, None, {}, reason, None)
        else:
            self.on_disconnect(self, None, reason)

    def topics(self):
        return sorted(topic for topic, _, _, _ in self.published)


class Refused:
    """A version 2 ReasonCode for a rejected connection."""

    is_failure = True

    def __str__(self):
        return "Not authorized"


def install_fake_paho(with_api_version=True):
    """Put a stub paho in sys.modules for mqtt_handler's deferred import."""
    client_mod = types.ModuleType("paho.mqtt.client")
    client_mod.Client = FakeClient
    if with_api_version:
        client_mod.CallbackAPIVersion = types.SimpleNamespace(VERSION2="v2")
    paho = types.ModuleType("paho")
    mqtt = types.ModuleType("paho.mqtt")
    paho.mqtt = mqtt
    mqtt.client = client_mod
    sys.modules["paho"] = paho
    sys.modules["paho.mqtt"] = mqtt
    sys.modules["paho.mqtt.client"] = client_mod


def make_config(**overrides):
    values = {
        "enabled": "true",
        "broker": "homeassistant.example.com",
        "port": "1883",
        "username": "mqttuser",
        "password": "secret",
        "device_id": "truenas_temps",
        "device_name": "TrueNAS Temperature Logger",
    }
    values.update(overrides)
    config = configparser.ConfigParser()
    config.read_dict({"MQTT": values})
    return config


class MqttTestCase(unittest.TestCase):
    """Isolates each test from the stubbed paho and the module's globals."""

    def setUp(self):
        self._saved = {k: sys.modules.get(k)
                       for k in ("paho", "paho.mqtt", "paho.mqtt.client")}
        install_fake_paho()
        import mqtt_handler
        self.mqtt_handler = mqtt_handler
        mqtt_handler._readings_state = "initial"
        # Keep expected error logs off the test runner's output.
        logging.disable(logging.CRITICAL)

    def tearDown(self):
        logging.disable(logging.NOTSET)
        for name, module in self._saved.items():
            if module is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = module

    def connected_client(self, devices=("sda", "sdb")):
        """A client that has completed the handshake and published discovery."""
        config = make_config()
        client = self.mqtt_handler.setup_mqtt(config, lambda: set(devices))
        client.fire_connack()
        return client, config


class TestDiscoveryTiming(MqttTestCase):
    """
    The bug this module was fixed for: discovery published between
    connect() and the CONNACK is silently dropped, so Home Assistant never
    creates the entities while the readings appear to publish fine.
    """

    def test_nothing_is_published_before_the_connack(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        self.assertEqual(client.published, [])

    def test_discovery_is_published_when_the_broker_accepts(self):
        client, _ = self.connected_client(devices=("sda", "sdb", "sdc"))
        # One per drive, plus CPU temperature and fan speed.
        self.assertEqual(len(client.published), 5)

    def test_discovery_survives_a_publish_that_precedes_the_connection(self):
        """QoS 1 is queued by paho, so discovery is not lost to a race."""
        for _, _, qos, retain in self.connected_client()[0].published:
            self.assertEqual(qos, 1)
            self.assertTrue(retain)

    def test_a_refused_connection_publishes_nothing(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        client.fire_connack(Refused())
        self.assertEqual(client.published, [])

    def test_reconnecting_republishes_discovery(self):
        """A broker restart loses its retained set; reconnecting restores it."""
        client, _ = self.connected_client()
        client.published.clear()
        client.fire_disconnect(reason=7)
        client.fire_connack()
        self.assertEqual(len(client.published), 4)

    def test_reconnecting_picks_up_a_reloaded_drive_list(self):
        """SIGHUP rebinds block_devices, so the callable must be re-read."""
        devices = {"sda"}
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: devices)
        client.fire_connack()
        client.published.clear()
        devices.add("sdb")          # as reload_config_values would
        client.fire_connack()
        self.assertIn(
            "homeassistant/sensor/truenas_temps_sdb/config", client.topics()
        )


class TestColdDns(MqttTestCase):
    """
    The NAS starts before DNS is up often enough that a synchronous
    connect() would raise and leave MQTT dead for the whole run.
    """

    def test_setup_survives_a_name_that_does_not_resolve_yet(self):
        # FakeClient.connect raises EAI_NONAME; connect_async does not.
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        self.assertIsNotNone(client)
        self.assertTrue(client.loop_running)

    def test_retry_backoff_is_bounded(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        self.assertEqual(client.delays, (1, 120))

    def test_retries_are_logged_after_the_first_attempt(self):
        logging.disable(logging.NOTSET)
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        with self.assertLogs(level="INFO") as captured:
            client.fire_pre_connect()   # first attempt, not worth a line
            client.fire_pre_connect()   # a retry, which is
            logging.info("sentinel")
        retries = [m for m in captured.output if "connection attempt" in m]
        self.assertEqual(len(retries), 1)
        self.assertIn("attempt 2", retries[0])

    def test_an_unusable_address_is_not_retried(self):
        """Retrying a malformed address would never come good."""
        self.assertIsNone(
            self.mqtt_handler.setup_mqtt(make_config(broker=""), lambda: {"sda"})
        )

    def test_connection_is_asynchronous(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        self.assertEqual(
            client.connect_args, ("homeassistant.example.com", 1883, 60)
        )


class TestDiscoveryPayloads(MqttTestCase):
    def payloads(self, client):
        return {topic: json.loads(body) for topic, body, _, _ in client.published}

    def test_every_sensor_has_its_own_topic_and_unique_id(self):
        client, _ = self.connected_client(devices=("sda", "sdb"))
        payloads = self.payloads(client)
        self.assertEqual(
            sorted(payloads),
            [
                "homeassistant/sensor/truenas_temps_cpu_temp/config",
                "homeassistant/sensor/truenas_temps_fan_speed/config",
                "homeassistant/sensor/truenas_temps_sda/config",
                "homeassistant/sensor/truenas_temps_sdb/config",
            ],
        )
        unique_ids = [p["unique_id"] for p in payloads.values()]
        self.assertEqual(len(unique_ids), len(set(unique_ids)))

    def test_all_sensors_share_one_state_topic_and_device(self):
        client, _ = self.connected_client()
        for payload in self.payloads(client).values():
            self.assertEqual(
                payload["state_topic"], "homeassistant/sensor/truenas_temps/state"
            )
            self.assertEqual(payload["device"]["identifiers"], ["truenas_temps"])

    def test_value_templates_match_the_keys_actually_published(self):
        """A template naming a key the state payload lacks renders empty."""
        client, config = self.connected_client(devices=("sda", "sdb"))
        templates = {
            p["value_template"] for p in self.payloads(client).values()
        }
        client.published.clear()
        self.mqtt_handler.publish_readings(
            client, config, {"sda": 34, "sdb": 36}, 40, 51.25
        )
        state = json.loads(client.published[0][1])
        for template in templates:
            key = template.strip("{} ").split("value_json.")[1].strip()
            self.assertIn(key, state)

    def test_drive_names_are_valid_jinja_attributes(self):
        """value_json.sda works; a name with a hyphen would not."""
        client, _ = self.connected_client(devices=("sda", "nvme0n1"))
        for payload in self.payloads(client).values():
            key = payload["value_template"].strip("{} ").split("value_json.")[1]
            self.assertTrue(key.strip().isidentifier())

    def test_no_drives_still_advertises_cpu_and_fan(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: set())
        client.fire_connack()
        self.assertEqual(len(client.published), 2)

    def test_no_drives_is_warned_about(self):
        logging.disable(logging.NOTSET)
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: set())
        with self.assertLogs(level="WARNING") as captured:
            client.fire_connack()
        self.assertTrue(
            any("No drives to advertise" in m for m in captured.output)
        )


class TestReadings(MqttTestCase):
    def test_payload_carries_drives_cpu_and_fan(self):
        client, config = self.connected_client()
        client.published.clear()
        self.mqtt_handler.publish_readings(
            client, config, {"sda": 34, "sdb": 36}, 40, 51.25
        )
        topic, body, _, _ = client.published[0]
        self.assertEqual(topic, "homeassistant/sensor/truenas_temps/state")
        self.assertEqual(
            json.loads(body),
            {"sda": 34, "sdb": 36, "fan_speed": 40, "cpu_temp": 51.2},
        )

    def test_a_drive_that_read_nothing_is_absent_rather_than_zero(self):
        """Publishing 0C for a spun-down drive would wind the graph down."""
        client, config = self.connected_client(devices=("sda", "sdb"))
        client.published.clear()
        self.mqtt_handler.publish_readings(client, config, {"sda": 34}, 40, 51.0)
        self.assertNotIn("sdb", json.loads(client.published[0][1]))

    def test_readings_are_not_retained(self):
        """A retained reading would outlive the daemon and look current."""
        client, config = self.connected_client()
        client.published.clear()
        self.mqtt_handler.publish_readings(client, config, {"sda": 34}, 40, 51.0)
        self.assertFalse(client.published[0][3])

    def test_failure_is_logged_once_then_quietly(self):
        logging.disable(logging.NOTSET)
        client, config = self.connected_client()
        client.fail_publish_rc = NO_CONN
        with self.assertLogs(level="DEBUG") as captured:
            for _ in range(5):
                self.mqtt_handler.publish_readings(
                    client, config, {"sda": 34}, 40, 51.0
                )
        errors = [m for m in captured.output if m.startswith("ERROR")]
        self.assertEqual(len(errors), 1)
        self.assertIn("Fan control is unaffected", errors[0])

    def test_recovery_is_logged(self):
        logging.disable(logging.NOTSET)
        client, config = self.connected_client()
        client.fail_publish_rc = NO_CONN
        with self.assertLogs(level="ERROR"):
            self.mqtt_handler.publish_readings(client, config, {"sda": 34}, 40, 51.0)
        client.fail_publish_rc = None
        with self.assertLogs(level="INFO") as captured:
            self.mqtt_handler.publish_readings(client, config, {"sda": 34}, 40, 51.0)
        self.assertTrue(any("recovered" in m for m in captured.output))

    def test_the_first_successful_payload_is_logged_in_full(self):
        """So a blank entity can be checked against what was really sent."""
        logging.disable(logging.NOTSET)
        client, config = self.connected_client()
        with self.assertLogs(level="INFO") as captured:
            self.mqtt_handler.publish_readings(client, config, {"sda": 34}, 40, 51.0)
        flowing = [m for m in captured.output if "readings now flowing" in m]
        self.assertEqual(len(flowing), 1)
        self.assertIn('"sda": 34', flowing[0])


class TestReturnCodeText(MqttTestCase):
    """A bare "rc=4" in the log is a puzzle; the words are the answer."""

    def test_paho_s_own_description_is_used_when_available(self):
        sys.modules["paho.mqtt.client"].error_string = (
            lambda rc: "The client is not currently connected."
        )
        self.assertEqual(
            self.mqtt_handler._rc_text(NO_CONN),
            "rc=4 (The client is not currently connected.)",
        )

    def test_the_number_alone_is_used_when_paho_cannot_describe_it(self):
        self.assertEqual(self.mqtt_handler._rc_text(NO_CONN), "rc=4")


class TestSetupGuards(MqttTestCase):
    def test_disabled_returns_no_client(self):
        self.assertIsNone(
            self.mqtt_handler.setup_mqtt(make_config(enabled="false"), lambda: set())
        )

    def test_a_non_boolean_enabled_does_not_stop_the_daemon(self):
        self.assertIsNone(
            self.mqtt_handler.setup_mqtt(make_config(enabled="yes please"),
                                         lambda: set())
        )

    def test_missing_paho_does_not_stop_the_daemon(self):
        # A None entry in sys.modules makes the import raise ImportError.
        sys.modules["paho.mqtt.client"] = None
        self.assertIsNone(
            self.mqtt_handler.setup_mqtt(make_config(), lambda: set())
        )

    def test_credentials_are_passed_to_the_broker(self):
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: set())
        self.assertEqual(client.credentials, ("mqttuser", "secret"))

    def test_no_username_connects_anonymously(self):
        client = self.mqtt_handler.setup_mqtt(
            make_config(username="", password=""), lambda: set()
        )
        self.assertIsNone(client.credentials)

    def test_paho_1_x_callback_signatures_are_supported(self):
        """paho 1.x has no CallbackAPIVersion and one fewer callback arg."""
        install_fake_paho(with_api_version=False)
        client = self.mqtt_handler.setup_mqtt(make_config(), lambda: {"sda"})
        client.fire_connack()       # would TypeError on a signature mismatch
        self.assertEqual(len(client.published), 3)

    def test_a_failed_discovery_publish_is_reported(self):
        logging.disable(logging.NOTSET)
        config = make_config()
        client = self.mqtt_handler.setup_mqtt(config, lambda: {"sda", "sdb"})
        client.fail_publish_rc = NO_CONN
        with self.assertLogs(level="ERROR") as captured:
            client.fire_connack()
        summary = [m for m in captured.output if "Published only" in m]
        self.assertEqual(len(summary), 1)
        self.assertIn("0 of 4", summary[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)
