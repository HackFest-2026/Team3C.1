import json
import os
import queue
import secrets
import socket
import sqlite3
import ssl
import threading
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

try:
    import paho.mqtt.client as mqtt
except ImportError:
    mqtt = None

try:
    from dotenv import load_dotenv
except ImportError:
    load_dotenv = None


# ==================================================
# 1. PAGE CONFIGURATION AND PATHS
# ==================================================

st.set_page_config(
    page_title="Mining Safety Monitor",
    page_icon="⛏️",
    layout="wide",
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "mining_safety.db"
CONFIG_PATH = BASE_DIR / "threshold_config.json"
ENV_PATH = BASE_DIR / ".env"

if load_dotenv is not None:
    load_dotenv(ENV_PATH)


# ==================================================
# 2. MQTT TOPICS AND DEFAULT THRESHOLDS
# ==================================================

READINGS_TOPIC = "mine/001/readings"
CONFIG_REQUEST_TOPIC = "mine/001/config_request"
CONFIG_STATUS_TOPIC = "mine/001/config_status"

DEFAULT_THRESHOLDS = {
    "mq2_warning": 400,
    "mq2_critical": 600,
    "mq7_warning": 450,
    "mq7_critical": 650,
}


def validate_thresholds(config):
    """Validate 12-bit ADC demonstration thresholds."""
    clean = {}
    for key in DEFAULT_THRESHOLDS:
        value = config.get(key)
        if isinstance(value, bool):
            raise ValueError(f"{key} must be a whole number from 0 to 4095.")
        try:
            numeric = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be a whole number from 0 to 4095.")
        if str(value).strip() not in (str(numeric), f"{numeric}.0"):
            raise ValueError(f"{key} must be a whole number from 0 to 4095.")
        if not 0 <= numeric <= 4095:
            raise ValueError(f"{key} must be between 0 and 4095.")
        clean[key] = numeric

    for sensor in ("mq2", "mq7"):
        if clean[f"{sensor}_warning"] >= clean[f"{sensor}_critical"]:
            raise ValueError(
                f"{sensor.upper()} warning threshold must be lower than its critical threshold."
            )
    return clean


def load_thresholds():
    """Read the backend's shared config file; do not write to it from the dashboard."""
    if CONFIG_PATH.exists():
        try:
            with CONFIG_PATH.open("r", encoding="utf-8") as file:
                saved = json.load(file)
            merged = DEFAULT_THRESHOLDS.copy()
            if isinstance(saved, dict):
                merged.update({k: saved[k] for k in merged if k in saved})
            return validate_thresholds(merged)
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            st.warning("The saved threshold configuration could not be read. Showing prototype defaults.")
    return DEFAULT_THRESHOLDS.copy()


thresholds = load_thresholds()


# ==================================================
# 3. MQTT STATUS CLIENT
# ==================================================

@st.cache_resource(show_spinner=False)
def get_mqtt_resources():
    """Create one MQTT status subscriber shared across Streamlit reruns."""
    event_queue = queue.Queue()
    state = {"connected": False, "error": "Connecting to MQTT broker...", "last_message_time": None}
    lock = threading.Lock()

    if mqtt is None:
        state["error"] = "The paho-mqtt package is not installed."
        return {"client": None, "queue": event_queue, "state": state, "lock": lock}

    host = os.getenv("MQTT_HOST", "").strip()
    port_text = os.getenv("MQTT_PORT", "8883").strip()
    username = os.getenv("MQTT_USERNAME", "").strip()
    password = os.getenv("MQTT_PASSWORD", "")

    if not host or not username or not password:
        state["error"] = (
            "MQTT settings are missing. Check MQTT_HOST, MQTT_PORT, "
            "MQTT_USERNAME, and MQTT_PASSWORD in your .env file."
        )
        return {"client": None, "queue": event_queue, "state": state, "lock": lock}

    try:
        port = int(port_text)
        client_id = "mine-safety-dashboard-" + secrets.token_hex(4)
        try:
            client = mqtt.Client(
                callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
                client_id=client_id,
                protocol=mqtt.MQTTv311,
            )
            callback_api_v2 = True
        except (AttributeError, TypeError):
            client = mqtt.Client(client_id=client_id, protocol=mqtt.MQTTv311)
            callback_api_v2 = False

        client.username_pw_set(username, password)
        client.tls_set(cert_reqs=ssl.CERT_REQUIRED, tls_version=ssl.PROTOCOL_TLS_CLIENT)

        def on_connect_v2(mqtt_client, userdata, flags, reason_code, properties):
            connected = getattr(reason_code, "is_failure", None)
            is_ok = not connected if connected is not None else str(reason_code) in ("Success", "0")
            with lock:
                state["connected"] = bool(is_ok)
                state["error"] = None if is_ok else f"MQTT connection failed: {reason_code}"
            if is_ok:
                mqtt_client.subscribe(CONFIG_STATUS_TOPIC, qos=1)

        def on_connect_v1(mqtt_client, userdata, flags, rc):
            with lock:
                state["connected"] = (rc == 0)
                state["error"] = None if rc == 0 else f"MQTT connection failed (code {rc})."
            if rc == 0:
                mqtt_client.subscribe(CONFIG_STATUS_TOPIC, qos=1)

        def on_connect_fail(mqtt_client, userdata):
            with lock:
                state["connected"] = False
                state["error"] = (
                    "Could not reach the MQTT broker. Check MQTT_HOST, internet access, "
                    "port 8883, and that the host matches the working backend."
                )

        def on_disconnect_v2(mqtt_client, userdata, disconnect_flags, reason_code, properties):
            with lock:
                state["connected"] = False
                if getattr(reason_code, "is_failure", False):
                    state["error"] = f"MQTT disconnected: {reason_code}"
                elif state.get("error") is None:
                    state["error"] = "MQTT disconnected. Retrying connection..."

        def on_disconnect_v1(mqtt_client, userdata, rc):
            with lock:
                state["connected"] = False
                if rc != 0:
                    state["error"] = f"MQTT disconnected unexpectedly (code {rc}). Retrying connection..."
                elif state.get("error") is None:
                    state["error"] = "MQTT disconnected. Retrying connection..."

        def on_message(mqtt_client, userdata, message):
            try:
                payload = json.loads(message.payload.decode("utf-8"))
                if isinstance(payload, dict):
                    event_queue.put({"topic": message.topic, "payload": payload, "received_at": time.time()})
                    with lock:
                        state["last_message_time"] = time.time()
            except (UnicodeDecodeError, json.JSONDecodeError):
                event_queue.put({"topic": message.topic, "payload": {"status": "error", "message": "Received invalid JSON status."}, "received_at": time.time()})

        client.on_connect = on_connect_v2 if callback_api_v2 else on_connect_v1
        client.on_connect_fail = on_connect_fail
        client.on_disconnect = on_disconnect_v2 if callback_api_v2 else on_disconnect_v1
        client.on_message = on_message
        client.connect_async(host, port, keepalive=30)
        client.loop_start()
        return {"client": client, "queue": event_queue, "state": state, "lock": lock}

    except (ValueError, OSError, ssl.SSLError, Exception) as error:
        with lock:
            state["error"] = f"Could not start MQTT connection: {error}"
        return {"client": None, "queue": event_queue, "state": state, "lock": lock}


mqtt_resources = get_mqtt_resources()

if "latest_config_status" not in st.session_state:
    st.session_state.latest_config_status = None
if "latest_config_request_id" not in st.session_state:
    st.session_state.latest_config_request_id = None

# Consume status messages delivered by the background MQTT client.
while True:
    try:
        status_event = mqtt_resources["queue"].get_nowait()
    except queue.Empty:
        break
    payload = status_event.get("payload", {})
    st.session_state.latest_config_status = payload


def send_threshold_configuration(config):
    """Request that the backend forward a validated configuration to the ESP32."""
    clean = validate_thresholds(config)
    client = mqtt_resources.get("client")
    state = mqtt_resources.get("state", {})
    lock = mqtt_resources.get("lock")

    if client is None:
        raise RuntimeError(state.get("error") or "MQTT client is unavailable.")

    with lock:
        connected = state.get("connected", False)
    if not connected:
        raise RuntimeError("The dashboard is not connected to MQTT yet. Wait a few seconds and try again.")

    request_id = "dashboard-" + secrets.token_hex(8)
    payload = {"request_id": request_id, **clean}
    result = client.publish(CONFIG_REQUEST_TOPIC, json.dumps(payload), qos=1, retain=False)
    if result.rc != mqtt.MQTT_ERR_SUCCESS:
        raise RuntimeError(f"MQTT publish failed with code {result.rc}.")

    st.session_state.latest_config_request_id = request_id
    st.session_state.latest_config_status = {
        "status": "request_sent",
        "request_id": request_id,
        "message": "Threshold configuration request sent to the backend. Waiting for device acknowledgement.",
    }


# ==================================================
# 4. DATABASE FUNCTIONS
# ==================================================

def get_readings():
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        return pd.read_sql_query(
            "SELECT * FROM readings ORDER BY id DESC LIMIT 500", conn
        ).sort_values("id")


def get_alerts():
    if not DB_PATH.exists():
        return pd.DataFrame()
    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        return pd.read_sql_query(
            "SELECT * FROM alerts ORDER BY id DESC LIMIT 100", conn
        )


# ==================================================
# 5. RISK CLASSIFICATION
# ==================================================

def get_risk_level(value, slope, warning, critical):
    """Classify raw ADC readings using prototype thresholds and a trend heuristic."""
    if value >= critical:
        return "CRITICAL"
    if slope >= 25:
        return "HIGH RISK"
    if value >= warning:
        return "WARNING"
    return "NORMAL"


RISK_COLORS = {
    "NORMAL": "#16A34A",
    "WARNING": "#EAB308",
    "HIGH RISK": "#F97316",
    "CRITICAL": "#DC2626",
}
RISK_EMOJI = {"NORMAL": "🟢", "WARNING": "🟡", "HIGH RISK": "🟠", "CRITICAL": "🔴"}
RISK_PRIORITY = {"NORMAL": 0, "WARNING": 1, "HIGH RISK": 2, "CRITICAL": 3}


def highest_risk(*levels):
    return max(levels, key=lambda level: RISK_PRIORITY[level])


# ==================================================
# 6. DASHBOARD HEADER AND REFRESH
# ==================================================

st.title("⛏️ Mining Worker Gas Safety Monitor")
st.caption("ESP32 + MQ-2 + MQ-7 + EMQX MQTT + Python")
st.warning(
    "PROTOTYPE ONLY: Sensor values are raw ADC readings. Thresholds are demonstration settings, "
    "not certified gas-safety limits. This system must not be used as the sole means of protecting workers."
)

try:
    from streamlit_autorefresh import st_autorefresh
    st_autorefresh(interval=3000, key="dashboard_refresh")
except ImportError:
    st.info("Automatic refresh is unavailable. Install streamlit-autorefresh or use Streamlit's Rerun button.")


# ==================================================
# 7. SIDEBAR: THRESHOLD CONFIGURATION OVER MQTT
# ==================================================

st.sidebar.title("⚙️ Threshold Settings")
st.sidebar.caption(
    "Submit values to the Python backend, which forwards them to the ESP32 over MQTT. "
    "The device must acknowledge the configuration before it is reported as applied."
)

with mqtt_resources["lock"]:
    mqtt_connected = mqtt_resources["state"].get("connected", False)
    mqtt_error = mqtt_resources["state"].get("error")
if mqtt_connected:
    st.sidebar.success("MQTT status: connected")
else:
    st.sidebar.warning("MQTT status: not connected")
    if mqtt_error:
        st.sidebar.caption(mqtt_error)

with st.sidebar.form("threshold_settings_form"):
    st.subheader("MQ-2")
    mq2_warning_input = st.number_input("MQ-2 Warning Threshold", min_value=0, max_value=4095, value=thresholds["mq2_warning"], step=10)
    mq2_critical_input = st.number_input("MQ-2 Critical Threshold", min_value=0, max_value=4095, value=thresholds["mq2_critical"], step=10)
    st.subheader("MQ-7")
    mq7_warning_input = st.number_input("MQ-7 Warning Threshold", min_value=0, max_value=4095, value=thresholds["mq7_warning"], step=10)
    mq7_critical_input = st.number_input("MQ-7 Critical Threshold", min_value=0, max_value=4095, value=thresholds["mq7_critical"], step=10)
    submit_button = st.form_submit_button("📡 Send Thresholds to ESP32", use_container_width=True)

if submit_button:
    new_thresholds = {
        "mq2_warning": int(mq2_warning_input),
        "mq2_critical": int(mq2_critical_input),
        "mq7_warning": int(mq7_warning_input),
        "mq7_critical": int(mq7_critical_input),
    }
    try:
        send_threshold_configuration(new_thresholds)
        st.sidebar.success("Request sent. Waiting for the backend and ESP32 acknowledgement.")
    except (ValueError, RuntimeError) as error:
        st.sidebar.error(str(error))

status = st.session_state.latest_config_status
if status:
    status_name = str(status.get("status", "unknown")).lower()
    status_message = status.get("message") or status.get("detail") or ""
    if status_name == "applied":
        st.sidebar.success("✅ ESP32 acknowledged: thresholds applied.")
    elif status_name in ("error", "failed", "rejected"):
        st.sidebar.error(f"❌ Configuration error. {status_message}".strip())
    elif status_name == "sent":
        st.sidebar.info("📤 Backend sent configuration to the ESP32. Waiting for device acknowledgement.")
    elif status_name == "request_sent":
        st.sidebar.info("📨 Request sent to backend. Waiting for configuration status.")
    else:
        st.sidebar.info(f"Configuration status: {status_name}. {status_message}".strip())
    if status.get("config_id"):
        st.sidebar.caption(f"Configuration ID: {status['config_id']}")
    elif status.get("request_id"):
        st.sidebar.caption(f"Request ID: {status['request_id']}")


# ==================================================
# 8. LOAD AND VALIDATE SENSOR DATA
# ==================================================

readings = get_readings()
alerts = get_alerts()

if readings.empty:
    st.info("Waiting for sensor data. Start the ESP32 and Python backend, then wait for readings to appear in the database.")
    st.stop()

required_columns = {"id", "timestamp", "device_id", "mq2_raw", "mq7_raw", "mq2_slope", "mq7_slope"}
missing_columns = required_columns.difference(readings.columns)
if missing_columns:
    st.error("The readings database is missing required columns: " + ", ".join(sorted(missing_columns)))
    st.stop()

readings["timestamp"] = pd.to_datetime(readings["timestamp"], errors="coerce", utc=True)
for column in ("mq2_raw", "mq7_raw", "mq2_slope", "mq7_slope"):
    readings[column] = pd.to_numeric(readings[column], errors="coerce")
readings["mq2_slope"] = readings["mq2_slope"].fillna(0)
readings["mq7_slope"] = readings["mq7_slope"].fillna(0)
readings = readings.dropna(subset=["timestamp", "mq2_raw", "mq7_raw"])
if readings.empty:
    st.error("No valid sensor readings are available.")
    st.stop()

latest = readings.iloc[-1]


# ==================================================
# 9. LATEST SENSOR READINGS
# ==================================================

st.subheader("📡 Live Sensor Readings")
col1, col2, col3 = st.columns(3)
with col1:
    st.metric("MQ-2 Raw Reading", int(latest["mq2_raw"]))
    st.caption(f"Warning: {thresholds['mq2_warning']} | Critical: {thresholds['mq2_critical']}")
with col2:
    st.metric("MQ-7 Raw Reading", int(latest["mq7_raw"]))
    st.caption(f"Warning: {thresholds['mq7_warning']} | Critical: {thresholds['mq7_critical']}")
with col3:
    st.metric("Device", str(latest["device_id"]))
    st.caption("Latest device recorded in the database")

if pd.notna(latest["timestamp"]):
    st.caption("Last recorded reading: " + latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC"))


# ==================================================
# 10. SENSOR RISK ASSESSMENT
# ==================================================

mq2_level = get_risk_level(int(latest["mq2_raw"]), float(latest["mq2_slope"]), thresholds["mq2_warning"], thresholds["mq2_critical"])
mq7_level = get_risk_level(int(latest["mq7_raw"]), float(latest["mq7_slope"]), thresholds["mq7_warning"], thresholds["mq7_critical"])
overall_level = highest_risk(mq2_level, mq7_level)

st.subheader("🚦 Current Risk Assessment")
risk_col1, risk_col2, risk_col3 = st.columns(3)
with risk_col1:
    st.markdown(f"""<div style="background-color:{RISK_COLORS[mq2_level]};padding:20px;border-radius:12px;color:white;text-align:center;"><h3>MQ-2</h3><h2>{RISK_EMOJI[mq2_level]} {mq2_level}</h2><p>Reading: {int(latest['mq2_raw'])}</p></div>""", unsafe_allow_html=True)
with risk_col2:
    st.markdown(f"""<div style="background-color:{RISK_COLORS[mq7_level]};padding:20px;border-radius:12px;color:white;text-align:center;"><h3>MQ-7</h3><h2>{RISK_EMOJI[mq7_level]} {mq7_level}</h2><p>Reading: {int(latest['mq7_raw'])}</p></div>""", unsafe_allow_html=True)
with risk_col3:
    st.markdown(f"""<div style="background-color:{RISK_COLORS[overall_level]};padding:20px;border-radius:12px;color:white;text-align:center;"><h3>Overall Status</h3><h2>{RISK_EMOJI[overall_level]} {overall_level}</h2><p>Highest detected prototype risk level</p></div>""", unsafe_allow_html=True)

if overall_level == "CRITICAL":
    st.error("CRITICAL: At least one configured critical ADC threshold has been reached.")
elif overall_level == "HIGH RISK":
    st.error("HIGH RISK: A rapid-rise trend heuristic has been triggered.")
elif overall_level == "WARNING":
    st.warning("WARNING: At least one configured warning threshold has been reached.")
else:
    st.success("NORMAL: No configured warning or critical condition was detected in the latest database reading.")


# ==================================================
# 11. SENSOR TREND GRAPHS
# ==================================================

st.subheader("📈 Sensor Trends")
tab1, tab2 = st.tabs(["MQ-2", "MQ-7"])
with tab1:
    fig_mq2 = px.line(readings, x="timestamp", y="mq2_raw", title="MQ-2 Raw Sensor Readings Over Time", labels={"timestamp": "Time", "mq2_raw": "Raw ADC Reading"})
    fig_mq2.add_hline(y=thresholds["mq2_warning"], line_dash="dash", line_color="#EAB308", annotation_text="Warning threshold")
    fig_mq2.add_hline(y=thresholds["mq2_critical"], line_dash="dash", line_color="#DC2626", annotation_text="Critical threshold")
    st.plotly_chart(fig_mq2, use_container_width=True)
with tab2:
    fig_mq7 = px.line(readings, x="timestamp", y="mq7_raw", title="MQ-7 Raw Sensor Readings Over Time", labels={"timestamp": "Time", "mq7_raw": "Raw ADC Reading"})
    fig_mq7.add_hline(y=thresholds["mq7_warning"], line_dash="dash", line_color="#EAB308", annotation_text="Warning threshold")
    fig_mq7.add_hline(y=thresholds["mq7_critical"], line_dash="dash", line_color="#DC2626", annotation_text="Critical threshold")
    st.plotly_chart(fig_mq7, use_container_width=True)


# ==================================================
# 12. TREND ANALYSIS
# ==================================================

st.subheader("🧠 Trend Analysis")
analysis_col1, analysis_col2 = st.columns(2)
with analysis_col1:
    mq2_slope = float(latest["mq2_slope"])
    st.metric("MQ-2 Trend", f"{mq2_slope:.2f} ADC counts/sample")
    st.warning("MQ-2: Rapid increase detected by the trend heuristic.") if mq2_slope >= 25 else st.info("MQ-2: No rapid increase detected by the trend heuristic.")
with analysis_col2:
    mq7_slope = float(latest["mq7_slope"])
    st.metric("MQ-7 Trend", f"{mq7_slope:.2f} ADC counts/sample")
    st.warning("MQ-7: Rapid increase detected by the trend heuristic.") if mq7_slope >= 25 else st.info("MQ-7: No rapid increase detected by the trend heuristic.")


# ==================================================
# 13. ALERT HISTORY
# ==================================================

st.subheader("🔔 Alert History")
if alerts.empty:
    st.info("No alerts have been recorded yet.")
else:
    if "timestamp" in alerts.columns:
        alerts["timestamp"] = pd.to_datetime(alerts["timestamp"], errors="coerce", utc=True)
    display_columns = [column for column in ["timestamp", "device_id", "sensor", "level", "reason", "raw_reading"] if column in alerts.columns]
    st.dataframe(alerts[display_columns], use_container_width=True, hide_index=True)


# ==================================================
# 14. RECENT SENSOR DATA
# ==================================================

with st.expander("View Recent Sensor Data"):
    st.dataframe(readings.sort_values("id", ascending=False), use_container_width=True, hide_index=True)


# ==================================================
# 15. FOOTER
# ==================================================

st.caption(
    "Mining Safety Prototype | Raw ADC readings and heuristic risk assessment only. "
    "Configuration is requested over MQTT; device application is shown only after an acknowledgement."
)
