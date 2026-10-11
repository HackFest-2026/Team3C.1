import json
import os
import ssl
import time
import uuid
from pathlib import Path
from datetime import datetime, timezone

import paho.mqtt.client as mqtt
from dotenv import load_dotenv

from database import (
    initialize_database,
    save_reading,
    save_alert,
)
from risk_engine import RiskEngine


# --------------------------------------------------
# CONFIGURATION
# --------------------------------------------------

load_dotenv()

BASE_DIR = Path(__file__).resolve().parent
CONFIG_FILE = BASE_DIR / "threshold_config.json"

MQTT_HOST = os.getenv("MQTT_HOST")
MQTT_PORT = int(os.getenv("MQTT_PORT", "8883"))
MQTT_USERNAME = os.getenv("MQTT_USERNAME")
MQTT_PASSWORD = os.getenv("MQTT_PASSWORD")

READINGS_TOPIC = "mine/001/readings"
ALERTS_TOPIC = "mine/001/alerts"
STATUS_TOPIC = "mine/001/status"

CONFIG_REQUEST_TOPIC = "mine/001/config_request"
CONFIG_TOPIC = "mine/001/config"
CONFIG_ACK_TOPIC = "mine/001/config_ack"

DEVICE_ID = "mine-001"

DEFAULT_THRESHOLDS = {
    "mq2_warning": 400,
    "mq2_critical": 600,
    "mq7_warning": 450,
    "mq7_critical": 650,
}

if not all([MQTT_HOST, MQTT_USERNAME, MQTT_PASSWORD]):
    raise RuntimeError(
        "MQTT configuration is incomplete. "
        "Check MQTT_HOST, MQTT_USERNAME, and MQTT_PASSWORD in .env."
    )


# --------------------------------------------------
# INITIALIZE DATABASE AND RISK ENGINE
# --------------------------------------------------

initialize_database()

risk_engine = RiskEngine()

previous_levels = {
    "MQ2": "NORMAL",
    "MQ7": "NORMAL",
}

last_alert_time = {}

# Current backend configuration.
current_thresholds = DEFAULT_THRESHOLDS.copy()

# Configuration currently waiting for device acknowledgement.
pending_config_id = None
pending_config_time = None


# --------------------------------------------------
# THRESHOLD VALIDATION
# --------------------------------------------------

def validate_thresholds(data):
    """
    Validate all four raw ADC thresholds.

    Values must be integers from 0 through 4095.
    Warning must be lower than critical for each sensor.
    """

    if not isinstance(data, dict):
        raise ValueError("Configuration must be a JSON object.")

    validated = {}

    for key in DEFAULT_THRESHOLDS:
        value = data.get(key)

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(
                f"{key} must be an integer."
            )

        if not 0 <= value <= 4095:
            raise ValueError(
                f"{key} must be between 0 and 4095."
            )

        validated[key] = value

    if validated["mq2_warning"] >= validated["mq2_critical"]:
        raise ValueError(
            "MQ-2 warning threshold must be lower than its critical threshold."
        )

    if validated["mq7_warning"] >= validated["mq7_critical"]:
        raise ValueError(
            "MQ-7 warning threshold must be lower than its critical threshold."
        )

    return validated


# --------------------------------------------------
# CONFIGURATION STORAGE
# --------------------------------------------------

def load_threshold_configuration():
    """
    Load the saved configuration at startup.
    If no valid configuration exists, use the defaults.
    """

    global current_thresholds

    if not CONFIG_FILE.exists():
        current_thresholds = DEFAULT_THRESHOLDS.copy()
        save_threshold_configuration(current_thresholds)
        print("Created default threshold configuration.")
        return

    try:
        with CONFIG_FILE.open("r", encoding="utf-8") as file:
            saved = json.load(file)

        current_thresholds = validate_thresholds(saved)

        print("Loaded saved threshold configuration.")

    except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Could not load saved thresholds: {exc}")
        print("Using default threshold configuration.")

        current_thresholds = DEFAULT_THRESHOLDS.copy()


def save_threshold_configuration(thresholds):
    """
    Save a complete configuration to a temporary file first,
    then replace the previous file.
    """

    validated = validate_thresholds(thresholds)

    temporary_file = CONFIG_FILE.with_suffix(".tmp")

    with temporary_file.open("w", encoding="utf-8") as file:
        json.dump(validated, file, indent=4)
        file.flush()
        os.fsync(file.fileno())

    os.replace(temporary_file, CONFIG_FILE)


def apply_thresholds_to_risk_engine(thresholds):
    """
    Map dashboard threshold names to RiskEngine threshold names.
    """

    risk_engine.update_thresholds({
        "mq2_warning": thresholds["mq2_warning"],
        "mq2_critical": thresholds["mq2_critical"],
        "mq7_warning": thresholds["mq7_warning"],
        "mq7_critical": thresholds["mq7_critical"],
    })


# --------------------------------------------------
# CONFIGURATION STATUS
# --------------------------------------------------

def publish_config_status(
    config_id,
    status,
    message,
    thresholds=None,
):
    """
    Publish configuration progress for the dashboard.

    The dashboard must subscribe to this topic if it wants
    to display these updates.
    """

    payload = {
        "device_id": DEVICE_ID,
        "config_id": config_id,
        "status": status,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }

    if thresholds is not None:
        payload["thresholds"] = thresholds

    info = client.publish(
        "mine/001/config_status",
        json.dumps(payload),
        qos=1,
        retain=False,
    )

    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        print("Warning: Could not publish configuration status.")


# --------------------------------------------------
# SEND CONFIGURATION TO ESP32
# --------------------------------------------------

def send_configuration(thresholds):
    """
    Persist and apply the requested configuration in the backend,
    then publish it to the ESP32.

    The ESP32 acknowledgement is required before we report
    successful application on the device.
    """

    global current_thresholds
    global pending_config_id
    global pending_config_time

    validated = validate_thresholds(thresholds)

    config_id = str(uuid.uuid4())

    # Persist the configuration before publishing it.
    # This prevents the backend from claiming it accepted
    # a configuration that it could not save locally.

    save_threshold_configuration(validated)

    current_thresholds = validated.copy()

    apply_thresholds_to_risk_engine(current_thresholds)

    pending_config_id = config_id
    pending_config_time = time.time()

    payload = {
        "config_id": config_id,
        **current_thresholds,
    }

    info = client.publish(
        CONFIG_TOPIC,
        json.dumps(payload),
        qos=1,
        retain=True,
    )

    if info.rc != mqtt.MQTT_ERR_SUCCESS:
        publish_config_status(
            config_id,
            "error",
            "Could not publish configuration to MQTT.",
            current_thresholds,
        )

        print("ERROR: Configuration publish failed.")
        return

    print()
    print("Configuration saved by backend and sent to ESP32.")
    print(f"Configuration ID: {config_id}")
    print(f"Thresholds: {current_thresholds}")
    print("Waiting for ESP32 acknowledgement...")

    publish_config_status(
        config_id,
        "sent",
        "Configuration sent; waiting for ESP32 acknowledgement.",
        current_thresholds,
    )


# --------------------------------------------------
# MQTT CALLBACKS
# --------------------------------------------------

def on_connect(client, userdata, flags, reason_code, properties):
    if reason_code == 0:
        print("Connected securely to EMQX Cloud!")

        subscriptions = [
            READINGS_TOPIC,
            STATUS_TOPIC,
            CONFIG_REQUEST_TOPIC,
            CONFIG_ACK_TOPIC,
        ]

        for topic in subscriptions:
            client.subscribe(topic, qos=1)
            print(f"Subscribed to: {topic}")

        print("Waiting for ESP32 sensor readings...\n")

        # Restore the saved backend configuration to the retained
        # configuration topic after reconnecting.
        #
        # The ESP32 receives it when it reconnects or resubscribes.

        payload = {
            "config_id": str(uuid.uuid4()),
            **current_thresholds,
        }

        client.publish(
            CONFIG_TOPIC,
            json.dumps(payload),
            qos=1,
            retain=True,
        )

        print("Published current configuration to MQTT.")

    else:
        print(f"MQTT connection failed: {reason_code}")


def handle_config_request(client, data):
    """
    Process a dashboard configuration request.
    """

    request_id = data.get("request_id", "unknown")

    try:
        thresholds = validate_thresholds(data)

        send_configuration(thresholds)

        print(
            f"Accepted dashboard configuration request: {request_id}"
        )

    except (ValueError, TypeError) as exc:
        print(f"Rejected configuration request: {exc}")

        publish_config_status(
            request_id,
            "error",
            str(exc),
        )

    except OSError as exc:
        print(f"Could not save configuration: {exc}")

        publish_config_status(
            request_id,
            "error",
            "Could not persist configuration.",
        )


def handle_config_ack(data):
    """
    Accept acknowledgements from the ESP32.
    """

    global pending_config_id
    global pending_config_time

    config_id = data.get("config_id")
    status = data.get("status", "unknown")
    message = data.get("message", "")

    print()
    print("ESP32 configuration acknowledgement received.")
    print(f"Configuration ID: {config_id}")
    print(f"Status: {status}")
    print(f"Message: {message}")

    # Ignore acknowledgements that do not match the configuration
    # currently awaiting confirmation.

    if config_id != pending_config_id:
        print("Acknowledgement does not match the pending configuration.")
        return

    if status == "applied":
        print("SUCCESS: ESP32 confirmed the configuration was applied.")

        publish_config_status(
            config_id,
            "applied",
            "ESP32 confirmed that thresholds were saved and applied.",
            current_thresholds,
        )

        pending_config_id = None
        pending_config_time = None

    elif status == "error":
        print("ERROR: ESP32 rejected or could not save the configuration.")

        publish_config_status(
            config_id,
            "error",
            message or "ESP32 reported a configuration error.",
            current_thresholds,
        )

        pending_config_id = None
        pending_config_time = None


def on_message(client, userdata, msg):
    try:
        payload = msg.payload.decode("utf-8")

        # --------------------------------------------------
        # ESP32 STATUS
        # --------------------------------------------------

        if msg.topic == STATUS_TOPIC:
            print(f"ESP32 status: {payload}")
            return

        # Parse JSON for other topics.
        data = json.loads(payload)

        if not isinstance(data, dict):
            raise ValueError("MQTT JSON payload must be an object.")

        # --------------------------------------------------
        # DASHBOARD CONFIGURATION REQUEST
        # --------------------------------------------------

        if msg.topic == CONFIG_REQUEST_TOPIC:
            handle_config_request(client, data)
            return

        # --------------------------------------------------
        # ESP32 CONFIGURATION ACKNOWLEDGEMENT
        # --------------------------------------------------

        if msg.topic == CONFIG_ACK_TOPIC:
            handle_config_ack(data)
            return

        # --------------------------------------------------
        # SENSOR READINGS
        # --------------------------------------------------

        if msg.topic != READINGS_TOPIC:
            print(f"Ignoring message on unexpected topic: {msg.topic}")
            return

        mq2 = data.get("mq2")
        mq7 = data.get("mq7")
        device_id = data.get("device_id", DEVICE_ID)

        for sensor_name, value in [("mq2", mq2), ("mq7", mq7)]:
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError(
                    f"{sensor_name} must be an integer."
                )

            if not 0 <= value <= 4095:
                raise ValueError(
                    f"{sensor_name} must be between 0 and 4095."
                )

        timestamp = datetime.now(timezone.utc).isoformat()

        result = risk_engine.analyse(mq2, mq7)

        mq2_result = result["mq2"]
        mq7_result = result["mq7"]

        save_reading(
            timestamp=timestamp,
            device_id=device_id,
            mq2_raw=mq2,
            mq7_raw=mq7,
            mq2_slope=mq2_result["slope"],
            mq7_slope=mq7_result["slope"],
            mq2_anomaly=mq2_result["anomaly"],
            mq7_anomaly=mq7_result["anomaly"],
        )

        print(
            f"[{timestamp}] "
            f"MQ-2: {mq2} | "
            f"MQ-7: {mq7} | "
            f"Overall risk: {result['overall_level']}"
        )

        # Generate alerts when risk escalates.
        # Repeat reminders are rate-limited.

        for sensor_result in [mq2_result, mq7_result]:
            sensor = sensor_result["sensor"]
            level = sensor_result["level"]

            if level == "NORMAL":
                previous_levels[sensor] = "NORMAL"
                continue

            now = time.time()
            alert_key = (sensor, level)
            last_time = last_alert_time.get(alert_key, 0)

            risk_escalated = (
                level == "CRITICAL"
                and previous_levels[sensor] != "CRITICAL"
            ) or (
                level == "WARNING"
                and previous_levels[sensor] == "NORMAL"
            )

            reminder_due = now - last_time >= 60

            if risk_escalated or reminder_due:
                alert = {
                    "timestamp": timestamp,
                    "device_id": device_id,
                    "sensor": sensor,
                    "level": level,
                    "reason": sensor_result["reason"],
                    "raw_reading": sensor_result["raw_reading"],
                }

                save_alert(
                    timestamp=timestamp,
                    device_id=device_id,
                    sensor=sensor,
                    level=level,
                    reason=sensor_result["reason"],
                    raw_reading=sensor_result["raw_reading"],
                )

                client.publish(
                    ALERTS_TOPIC,
                    json.dumps(alert),
                    qos=1,
                )

                print(
                    f"  >>> {level} ALERT: "
                    f"{sensor} - {sensor_result['reason']}"
                )

                last_alert_time[alert_key] = now

            previous_levels[sensor] = level

    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        print(f"Invalid MQTT message: {exc}")

    except Exception as exc:
        print(f"Error processing MQTT message: {exc}")


# --------------------------------------------------
# START MQTT CLIENT
# --------------------------------------------------

load_threshold_configuration()
apply_thresholds_to_risk_engine(current_thresholds)

client = mqtt.Client(
    callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
    client_id="mine-safety-python-backend",
    protocol=mqtt.MQTTv311,
)

client.username_pw_set(MQTT_USERNAME, MQTT_PASSWORD)

# Require valid TLS certificates.

client.tls_set(
    cert_reqs=ssl.CERT_REQUIRED,
    tls_version=ssl.PROTOCOL_TLS_CLIENT,
)

client.on_connect = on_connect
client.on_message = on_message

print("Starting Mining Safety Backend...")
print(f"MQTT broker: {MQTT_HOST}:{MQTT_PORT}")
print("TLS certificate verification: enabled")
print(f"Initial thresholds: {current_thresholds}")

try:
    client.connect(MQTT_HOST, MQTT_PORT, keepalive=60)
    client.loop_forever()

except KeyboardInterrupt:
    print("\nStopping backend...")

finally:
    client.disconnect()