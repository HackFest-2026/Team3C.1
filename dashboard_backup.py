import json
import sqlite3
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st


# ==================================================
# 1. PAGE CONFIGURATION
# ==================================================

st.set_page_config(
    page_title="Mining Safety Monitor",
    page_icon="⛏️",
    layout="wide",
)

BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "mining_safety.db"
CONFIG_PATH = BASE_DIR / "threshold_config.json"


# ==================================================
# 2. DEFAULT THRESHOLDS
# ==================================================

DEFAULT_THRESHOLDS = {
    "mq2_warning": 400,
    "mq2_critical": 600,
    "mq7_warning": 450,
    "mq7_critical": 650,
}


def load_thresholds():
    """Load saved thresholds or use prototype defaults."""

    if not CONFIG_PATH.exists():
        return DEFAULT_THRESHOLDS.copy()

    try:
        with CONFIG_PATH.open("r", encoding="utf-8") as file:
            saved = json.load(file)

        config = DEFAULT_THRESHOLDS.copy()

        for key in config:
            if key in saved:
                config[key] = int(saved[key])

        validate_thresholds(config)
        return config

    except (OSError, ValueError, TypeError, json.JSONDecodeError):
        st.warning(
            "Saved threshold settings could not be loaded. "
            "Default demonstration thresholds are being used."
        )
        return DEFAULT_THRESHOLDS.copy()


def save_thresholds(config):
    """Persist dashboard threshold settings locally."""

    validate_thresholds(config)

    temporary_path = CONFIG_PATH.with_suffix(".tmp")

    with temporary_path.open("w", encoding="utf-8") as file:
        json.dump(config, file, indent=4)

    temporary_path.replace(CONFIG_PATH)


def validate_thresholds(config):
    """Ensure thresholds are valid 12-bit ADC values."""

    for sensor in ("mq2", "mq7"):
        warning = config[f"{sensor}_warning"]
        critical = config[f"{sensor}_critical"]

        if not (0 <= warning <= 4095):
            raise ValueError(
                f"{sensor.upper()} warning threshold must be 0–4095."
            )

        if not (0 <= critical <= 4095):
            raise ValueError(
                f"{sensor.upper()} critical threshold must be 0–4095."
            )

        if warning >= critical:
            raise ValueError(
                f"{sensor.upper()} warning threshold must be "
                "lower than its critical threshold."
            )


thresholds = load_thresholds()


# ==================================================
# 3. DATABASE FUNCTIONS
# ==================================================

def get_readings():
    if not DB_PATH.exists():
        return pd.DataFrame()

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        return pd.read_sql_query(
            """
            SELECT *
            FROM readings
            ORDER BY id DESC
            LIMIT 500
            """,
            conn,
        ).sort_values("id")


def get_alerts():
    if not DB_PATH.exists():
        return pd.DataFrame()

    with sqlite3.connect(DB_PATH, timeout=10) as conn:
        return pd.read_sql_query(
            """
            SELECT *
            FROM alerts
            ORDER BY id DESC
            LIMIT 100
            """,
            conn,
        )


# ==================================================
# 4. RISK CLASSIFICATION
# ==================================================

def get_risk_level(value, slope, warning, critical):
    """
    Classify raw ADC readings using demonstration thresholds.

    These values are for prototype visualization only.
    """

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

RISK_EMOJI = {
    "NORMAL": "🟢",
    "WARNING": "🟡",
    "HIGH RISK": "🟠",
    "CRITICAL": "🔴",
}

RISK_PRIORITY = {
    "NORMAL": 0,
    "WARNING": 1,
    "HIGH RISK": 2,
    "CRITICAL": 3,
}


def highest_risk(*levels):
    return max(levels, key=lambda level: RISK_PRIORITY[level])


# ==================================================
# 5. DASHBOARD HEADER
# ==================================================

st.title("⛏️ Mining Worker Gas Safety Monitor")

st.caption(
    "ESP32 + MQ-2 + MQ-7 + EMQX MQTT + Python"
)

st.warning(
    "PROTOTYPE ONLY: Sensor values are raw ADC readings. "
    "Thresholds are demonstration settings, not certified gas-safety "
    "limits. This system must not be used as the sole means of protecting "
    "workers."
)


# ==================================================
# 6. AUTOMATIC REFRESH
# ==================================================

try:
    from streamlit_autorefresh import st_autorefresh

    st_autorefresh(
        interval=3000,
        key="dashboard_refresh",
    )

except ImportError:
    st.info(
        "Automatic refresh is unavailable. "
        "Install streamlit-autorefresh or use Streamlit's Rerun button."
    )


# ==================================================
# 7. SIDEBAR: EDITABLE THRESHOLDS
# ==================================================

st.sidebar.title("⚙️ Threshold Settings")

st.sidebar.caption(
    "These settings are saved locally for the dashboard. "
    "They are not yet sent to the ESP32."
)

with st.sidebar.form("threshold_settings_form"):

    st.subheader("MQ-2")

    mq2_warning_input = st.number_input(
        "MQ-2 Warning Threshold",
        min_value=0,
        max_value=4095,
        value=thresholds["mq2_warning"],
        step=10,
    )

    mq2_critical_input = st.number_input(
        "MQ-2 Critical Threshold",
        min_value=0,
        max_value=4095,
        value=thresholds["mq2_critical"],
        step=10,
    )

    st.subheader("MQ-7")

    mq7_warning_input = st.number_input(
        "MQ-7 Warning Threshold",
        min_value=0,
        max_value=4095,
        value=thresholds["mq7_warning"],
        step=10,
    )

    mq7_critical_input = st.number_input(
        "MQ-7 Critical Threshold",
        min_value=0,
        max_value=4095,
        value=thresholds["mq7_critical"],
        step=10,
    )

    save_button = st.form_submit_button(
        "💾 Save Thresholds",
        use_container_width=True,
    )


if save_button:

    new_thresholds = {
        "mq2_warning": int(mq2_warning_input),
        "mq2_critical": int(mq2_critical_input),
        "mq7_warning": int(mq7_warning_input),
        "mq7_critical": int(mq7_critical_input),
    }

    try:
        validate_thresholds(new_thresholds)
        save_thresholds(new_thresholds)

        st.sidebar.success(
            "Threshold settings saved successfully."
        )

        st.rerun()

    except ValueError as error:
        st.sidebar.error(str(error))

    except OSError as error:
        st.sidebar.error(
            f"Could not save settings: {error}"
        )


# ==================================================
# 8. LOAD SENSOR DATA
# ==================================================

readings = get_readings()
alerts = get_alerts()

if readings.empty:
    st.info(
        "Waiting for sensor data. Start the ESP32 and Python backend, "
        "then wait for readings to appear in the database."
    )

    st.stop()


required_columns = {
    "id",
    "timestamp",
    "device_id",
    "mq2_raw",
    "mq7_raw",
    "mq2_slope",
    "mq7_slope",
}

missing_columns = required_columns.difference(readings.columns)

if missing_columns:
    st.error(
        "The readings database is missing required columns: "
        + ", ".join(sorted(missing_columns))
    )

    st.stop()


readings["timestamp"] = pd.to_datetime(
    readings["timestamp"],
    errors="coerce",
    utc=True,
)

readings["mq2_raw"] = pd.to_numeric(
    readings["mq2_raw"],
    errors="coerce",
)

readings["mq7_raw"] = pd.to_numeric(
    readings["mq7_raw"],
    errors="coerce",
)

readings["mq2_slope"] = pd.to_numeric(
    readings["mq2_slope"],
    errors="coerce",
).fillna(0)

readings["mq7_slope"] = pd.to_numeric(
    readings["mq7_slope"],
    errors="coerce",
).fillna(0)

readings = readings.dropna(
    subset=["timestamp", "mq2_raw", "mq7_raw"]
)

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
    st.metric(
        "MQ-2 Raw Reading",
        int(latest["mq2_raw"]),
    )

    st.caption(
        f"Warning: {thresholds['mq2_warning']} | "
        f"Critical: {thresholds['mq2_critical']}"
    )

with col2:
    st.metric(
        "MQ-7 Raw Reading",
        int(latest["mq7_raw"]),
    )

    st.caption(
        f"Warning: {thresholds['mq7_warning']} | "
        f"Critical: {thresholds['mq7_critical']}"
    )

with col3:
    st.metric(
        "Device",
        str(latest["device_id"]),
    )

    st.caption("Latest device recorded in the database")


if pd.notna(latest["timestamp"]):
    st.caption(
        "Last recorded reading: "
        + latest["timestamp"].strftime("%Y-%m-%d %H:%M:%S UTC")
    )


# ==================================================
# 10. SENSOR RISK ASSESSMENT
# ==================================================

mq2_level = get_risk_level(
    int(latest["mq2_raw"]),
    float(latest["mq2_slope"]),
    thresholds["mq2_warning"],
    thresholds["mq2_critical"],
)

mq7_level = get_risk_level(
    int(latest["mq7_raw"]),
    float(latest["mq7_slope"]),
    thresholds["mq7_warning"],
    thresholds["mq7_critical"],
)

overall_level = highest_risk(
    mq2_level,
    mq7_level,
)

st.subheader("🚦 Current Risk Assessment")

risk_col1, risk_col2, risk_col3 = st.columns(3)

with risk_col1:
    st.markdown(
        f"""
        <div style="
            background-color:{RISK_COLORS[mq2_level]};
            padding:20px;
            border-radius:12px;
            color:white;
            text-align:center;
        ">
            <h3>MQ-2</h3>
            <h2>{RISK_EMOJI[mq2_level]} {mq2_level}</h2>
            <p>Reading: {int(latest["mq2_raw"])}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

with risk_col2:
    st.markdown(
        f"""
        <div style="
            background-color:{RISK_COLORS[mq7_level]};
            padding:20px;
            border-radius:12px;
            color:white;
            text-align:center;
        ">
            <h3>MQ-7</h3>
            <h2>{RISK_EMOJI[mq7_level]} {mq7_level}</h2>
            <p>Reading: {int(latest["mq7_raw"])}</p>
        </div>
        """,
        unsafe_allow_html=True,
    )

with risk_col3:
    st.markdown(
        f"""
        <div style="
            background-color:{RISK_COLORS[overall_level]};
            padding:20px;
            border-radius:12px;
            color:white;
            text-align:center;
        ">
            <h3>Overall Status</h3>
            <h2>{RISK_EMOJI[overall_level]} {overall_level}</h2>
            <p>Highest detected prototype risk level</p>
        </div>
        """,
        unsafe_allow_html=True,
    )


if overall_level == "CRITICAL":

    st.error(
        "CRITICAL: At least one configured critical ADC threshold "
        "has been reached."
    )

elif overall_level == "HIGH RISK":

    st.error(
        "HIGH RISK: A rapid-rise trend heuristic has been triggered."
    )

elif overall_level == "WARNING":

    st.warning(
        "WARNING: At least one configured warning threshold "
        "has been reached."
    )

else:

    st.success(
        "NORMAL: No configured warning or critical condition "
        "was detected in the latest database reading."
    )


# ==================================================
# 11. SENSOR TREND GRAPHS
# ==================================================

st.subheader("📈 Sensor Trends")

tab1, tab2 = st.tabs(["MQ-2", "MQ-7"])

with tab1:

    fig_mq2 = px.line(
        readings,
        x="timestamp",
        y="mq2_raw",
        title="MQ-2 Raw Sensor Readings Over Time",
        labels={
            "timestamp": "Time",
            "mq2_raw": "Raw ADC Reading",
        },
    )

    fig_mq2.add_hline(
        y=thresholds["mq2_warning"],
        line_dash="dash",
        line_color="#EAB308",
        annotation_text="Warning threshold",
    )

    fig_mq2.add_hline(
        y=thresholds["mq2_critical"],
        line_dash="dash",
        line_color="#DC2626",
        annotation_text="Critical threshold",
    )

    st.plotly_chart(
        fig_mq2,
        use_container_width=True,
    )

with tab2:

    fig_mq7 = px.line(
        readings,
        x="timestamp",
        y="mq7_raw",
        title="MQ-7 Raw Sensor Readings Over Time",
        labels={
            "timestamp": "Time",
            "mq7_raw": "Raw ADC Reading",
        },
    )

    fig_mq7.add_hline(
        y=thresholds["mq7_warning"],
        line_dash="dash",
        line_color="#EAB308",
        annotation_text="Warning threshold",
    )

    fig_mq7.add_hline(
        y=thresholds["mq7_critical"],
        line_dash="dash",
        line_color="#DC2626",
        annotation_text="Critical threshold",
    )

    st.plotly_chart(
        fig_mq7,
        use_container_width=True,
    )


# ==================================================
# 12. TREND ANALYSIS
# ==================================================

st.subheader("🧠 Trend Analysis")

analysis_col1, analysis_col2 = st.columns(2)

with analysis_col1:

    mq2_slope = float(latest["mq2_slope"])

    st.metric(
        "MQ-2 Trend",
        f"{mq2_slope:.2f} ADC counts/sample",
    )

    if mq2_slope >= 25:
        st.warning(
            "MQ-2: Rapid increase detected by the trend heuristic."
        )
    else:
        st.info(
            "MQ-2: No rapid increase detected by the trend heuristic."
        )

with analysis_col2:

    mq7_slope = float(latest["mq7_slope"])

    st.metric(
        "MQ-7 Trend",
        f"{mq7_slope:.2f} ADC counts/sample",
    )

    if mq7_slope >= 25:
        st.warning(
            "MQ-7: Rapid increase detected by the trend heuristic."
        )
    else:
        st.info(
            "MQ-7: No rapid increase detected by the trend heuristic."
        )


# ==================================================
# 13. ALERT HISTORY
# ==================================================

st.subheader("🔔 Alert History")

if alerts.empty:

    st.info("No alerts have been recorded yet.")

else:

    if "timestamp" in alerts.columns:

        alerts["timestamp"] = pd.to_datetime(
            alerts["timestamp"],
            errors="coerce",
            utc=True,
        )

    display_columns = [
        column
        for column in [
            "timestamp",
            "device_id",
            "sensor",
            "level",
            "reason",
            "raw_reading",
        ]
        if column in alerts.columns
    ]

    st.dataframe(
        alerts[display_columns],
        use_container_width=True,
        hide_index=True,
    )


# ==================================================
# 14. RECENT SENSOR DATA
# ==================================================

with st.expander("View Recent Sensor Data"):

    st.dataframe(
        readings.sort_values(
            "id",
            ascending=False,
        ),
        use_container_width=True,
        hide_index=True,
    )


# ==================================================
# 15. FOOTER
# ==================================================

st.caption(
    "Mining Safety Prototype | "
    "Raw ADC readings and heuristic risk assessment only. "
    "Dashboard thresholds are stored locally and are not yet "
    "synchronized with the ESP32."
)