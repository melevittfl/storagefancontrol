import json
import logging


def _connection_failed(reason):
    """
    True if the broker rejected us. `reason` is an int on the version 1
    callback API and a ReasonCode on version 2.
    """
    if hasattr(reason, "is_failure"):
        return reason.is_failure
    return reason != 0


def _log_connect_result(reason):
    if _connection_failed(reason):
        logging.error(
            "MQTT broker refused the connection: %s. Check the username and "
            "password in [MQTT], and that the broker allows this client.",
            reason,
        )
    else:
        logging.info("MQTT broker accepted the connection")


def _log_disconnect(reason):
    if _connection_failed(reason):
        logging.warning("MQTT disconnected: %s. paho will retry.", reason)
    else:
        logging.info("MQTT disconnected cleanly")


def setup_mqtt(config):
    """Connect to MQTT broker and return client, or None if disabled or connection fails."""
    try:
        enabled = config.getboolean("MQTT", "enabled", fallback=False)
    except ValueError as e:
        logging.error(
            "[MQTT] enabled is not a boolean (%s). Continuing without MQTT.", e
        )
        return None

    if not enabled:
        logging.info("MQTT is disabled in the config")
        return None

    # Imported here rather than at module level so that fan control still
    # works without paho-mqtt installed, which matters on TrueNAS SCALE
    # where there is no pip to install it with.
    try:
        import paho.mqtt.client as mqtt
    except ImportError:
        logging.error(
            "MQTT is enabled but paho-mqtt is not installed. Run "
            "'python3 install_deps.py' to vendor it into ./lib. "
            "Continuing without MQTT."
        )
        return None

    broker = config.get("MQTT", "broker")
    port = config.getint("MQTT", "port")
    username = config.get("MQTT", "username", fallback="")
    password = config.get("MQTT", "password", fallback="")

    # paho-mqtt 2.x defaults to the version 1 callback API and warns that it
    # is deprecated. This module needs only connection results, whose shape
    # differs between the two, so both are handled and version 2 preferred.
    # paho-mqtt 1.x has no CallbackAPIVersion enum at all.
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = lambda c, u, flags, reason, props: _log_connect_result(reason)
        client.on_disconnect = lambda c, u, flags, reason, props: _log_disconnect(reason)
    else:
        client = mqtt.Client()
        client.on_connect = lambda c, u, flags, rc: _log_connect_result(rc)
        client.on_disconnect = lambda c, u, rc: _log_disconnect(rc)

    if username:
        client.username_pw_set(username, password)
    else:
        logging.info("No MQTT username set, connecting anonymously")

    try:
        # This completes the TCP connection but not the MQTT handshake: the
        # broker's accept or refuse arrives later as a CONNACK, which is why
        # the result is reported from on_connect rather than here. Without
        # that callback a rejected password looks exactly like success.
        client.connect(broker, port, keepalive=60)
        client.loop_start()
        logging.info("MQTT connecting to %s:%s as %s", broker, port, username or "anonymous")
        return client
    except Exception as e:
        logging.error("Failed to reach MQTT broker at %s:%s: %s", broker, port, e)
        return None


def publish_discovery(client, config, devices):
    """Publish HA MQTT discovery config for each drive. Call once on startup."""
    device_id = config.get("MQTT", "device_id")
    device_name = config.get("MQTT", "device_name")
    state_topic = f"homeassistant/sensor/{device_id}/state"

    device_info = {
        "identifiers": [device_id],
        "name": device_name,
    }
    for dev in sorted(devices):
        config_topic = f"homeassistant/sensor/{device_id}_{dev}/config"
        payload = {
            "name": dev,
            "unique_id": f"{device_id}_{dev}",
            "state_topic": state_topic,
            "value_template": f"{{{{ value_json.{dev} }}}}",
            "device_class": "temperature",
            "unit_of_measurement": "°C",
            "state_class": "measurement",
            "device": device_info,
        }
        client.publish(config_topic, json.dumps(payload), retain=True)

    config_topic = f"homeassistant/sensor/{device_id}_fan_speed/config"
    payload = {
        "name": "Fan Speed",
        "unique_id": f"{device_id}_fan_speed",
        "state_topic": state_topic,
        "value_template": "{{ value_json.fan_speed }}",
        "unit_of_measurement": "%",
        "state_class": "measurement",
        "device": device_info,
    }
    client.publish(config_topic, json.dumps(payload), retain=True)

    config_topic = f"homeassistant/sensor/{device_id}_cpu_temp/config"
    payload = {
        "name": "CPU Temperature",
        "unique_id": f"{device_id}_cpu_temp",
        "state_topic": state_topic,
        "value_template": "{{ value_json.cpu_temp }}",
        "device_class": "temperature",
        "unit_of_measurement": "°C",
        "state_class": "measurement",
        "device": device_info,
    }
    client.publish(config_topic, json.dumps(payload), retain=True)

    logging.info("Published MQTT discovery for %d drive sensors, CPU temp, and fan speed", len(devices))


def publish_readings(client, config, readings, fan_speed, cpu_temp):
    """Publish per-device temperatures, CPU temp, and fan speed to MQTT state topic."""
    device_id = config.get("MQTT", "device_id")
    state_topic = f"homeassistant/sensor/{device_id}/state"
    payload = dict(readings)
    payload["fan_speed"] = fan_speed
    payload["cpu_temp"] = round(cpu_temp, 1)
    client.publish(state_topic, json.dumps(payload))
