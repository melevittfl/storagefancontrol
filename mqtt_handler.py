import json
import logging
import paho.mqtt.client as mqtt


def setup_mqtt(config):
    """Connect to MQTT broker and return client, or None if disabled or connection fails."""
    if not config.getboolean("MQTT", "enabled", fallback=False):
        return None

    broker = config.get("MQTT", "broker")
    port = config.getint("MQTT", "port")
    username = config.get("MQTT", "username", fallback="")
    password = config.get("MQTT", "password", fallback="")

    client = mqtt.Client()
    if username:
        client.username_pw_set(username, password)

    try:
        client.connect(broker, port, keepalive=60)
        client.loop_start()
        logging.info("Connected to MQTT broker at %s:%s", broker, port)
        return client
    except Exception as e:
        logging.error("Failed to connect to MQTT broker: %s", e)
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
