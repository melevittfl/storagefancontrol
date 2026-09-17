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


def _log_disconnect(reason):
    if _connection_failed(reason):
        logging.warning(
            "MQTT disconnected: %s. paho will retry, and discovery is "
            "republished once it reconnects.",
            reason,
        )
    else:
        logging.info("MQTT disconnected cleanly")


def _rc_text(rc):
    """
    Describe a paho return code. "rc=4" in a log is a puzzle; "rc=4 (The
    client is not currently connected.)" is an answer.
    """
    try:
        import paho.mqtt.client as mqtt

        return "rc=%s (%s)" % (rc, mqtt.error_string(rc))
    except Exception:
        return "rc=%s" % rc


def _publish(client, topic, payload, retain=False, qos=0):
    """
    Publish and report failure.

    paho does not raise when it cannot send. A QoS 0 publish made before
    the connection is up returns MQTT_ERR_NO_CONN and is dropped without
    being queued, so an unchecked return code loses the message silently.
    """
    result = client.publish(topic, payload, qos=qos, retain=retain)
    if result.rc != 0:
        logging.error("MQTT publish to %s failed: %s", topic, _rc_text(result.rc))
        return False
    logging.debug("MQTT published %d bytes to %s", len(payload), topic)
    return True


def setup_mqtt(config, get_devices):
    """
    Connect to MQTT broker and return the client, or None if disabled or
    the connection fails.

    `get_devices` is called to get the set of drives to advertise. It is a
    callable rather than a set because a SIGHUP reload rebuilds the set,
    and discovery is republished on every reconnect, not just the first.
    """
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

    def on_connect(client, reason):
        if _connection_failed(reason):
            logging.error(
                "MQTT broker refused the connection: %s. Check the username and "
                "password in [MQTT], and that the broker allows this client.",
                reason,
            )
            return
        logging.info("MQTT broker accepted the connection")
        # Discovery is published from here, never from the caller. connect()
        # returns once the TCP connection is up but before the CONNACK, so
        # anything published before this point is thrown away. Republishing
        # on every connect also restores the entities after a broker restart
        # that lost its retained set.
        publish_discovery(client, config, get_devices())

    # paho-mqtt 2.x defaults to the version 1 callback API and warns that it
    # is deprecated. This module needs only connection results, whose shape
    # differs between the two, so both are handled and version 2 preferred.
    # paho-mqtt 1.x has no CallbackAPIVersion enum at all.
    if hasattr(mqtt, "CallbackAPIVersion"):
        client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2)
        client.on_connect = lambda c, u, flags, reason, props: on_connect(c, reason)
        client.on_disconnect = lambda c, u, flags, reason, props: _log_disconnect(reason)
    else:
        client = mqtt.Client()
        client.on_connect = lambda c, u, flags, rc: on_connect(c, rc)
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
    """
    Publish HA MQTT discovery config for each drive.

    Called from the on_connect callback, so it runs on the paho network
    thread once the broker has accepted us. Sent at QoS 1 and retained:
    Home Assistant creates an entity only when it sees these, so unlike a
    temperature reading there is no next one along to cover a loss.
    """
    device_id = config.get("MQTT", "device_id")
    device_name = config.get("MQTT", "device_name")
    state_topic = f"homeassistant/sensor/{device_id}/state"

    device_info = {
        "identifiers": [device_id],
        "name": device_name,
    }

    # Named in full because tracking a missing entity down starts with
    # knowing which topics to subscribe to on the broker.
    logging.info(
        "Publishing MQTT discovery to homeassistant/sensor/%s_*/config, "
        "state topic %s, grouped under device '%s'",
        device_id,
        state_topic,
        device_name,
    )
    if not devices:
        logging.warning(
            "No drives to advertise over MQTT. Home Assistant will show only "
            "CPU temperature and fan speed. Check [Smart] device_filter."
        )
    published = 0
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
        published += _publish(client, config_topic, json.dumps(payload), retain=True, qos=1)

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
    published += _publish(client, config_topic, json.dumps(payload), retain=True, qos=1)

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
    published += _publish(client, config_topic, json.dumps(payload), retain=True, qos=1)

    expected = len(devices) + 2
    if published == expected:
        logging.info(
            "Published MQTT discovery for %d drive sensors, CPU temp, and fan speed",
            len(devices),
        )
    else:
        logging.error(
            "Published only %d of %d MQTT discovery messages. Home Assistant "
            "will be missing entities until the next reconnect.",
            published,
            expected,
        )


# Readings are published every cycle, so a broker outage would log once
# per poll forever. The first failure is worth an error, the rest are noise
# until one succeeds again. "initial" separates the first successful
# publish, which is worth announcing, from the routine ones after it.
_readings_state = "initial"


def publish_readings(client, config, readings, fan_speed, cpu_temp):
    """Publish per-device temperatures, CPU temp, and fan speed to MQTT state topic."""
    global _readings_state

    device_id = config.get("MQTT", "device_id")
    state_topic = f"homeassistant/sensor/{device_id}/state"
    payload = dict(readings)
    payload["fan_speed"] = fan_speed
    payload["cpu_temp"] = round(cpu_temp, 1)
    body = json.dumps(payload)

    result = client.publish(state_topic, body)
    if result.rc != 0:
        if _readings_state != "failing":
            logging.error(
                "MQTT publish of readings to %s failed: %s. Fan control is "
                "unaffected, only reporting is. Further failures log at debug "
                "level until one succeeds.",
                state_topic,
                _rc_text(result.rc),
            )
            _readings_state = "failing"
        else:
            logging.debug("MQTT publish of readings failed: %s", _rc_text(result.rc))
        return

    # The first payload is logged in full so that a Home Assistant entity
    # showing nothing can be checked against what was actually sent.
    if _readings_state == "initial":
        logging.info("MQTT readings now flowing to %s: %s", state_topic, body)
    elif _readings_state == "failing":
        logging.info("MQTT publish of readings recovered")
    else:
        logging.debug("MQTT readings to %s: %s", state_topic, body)
    _readings_state = "ok"
