from collections import deque


class RiskEngine:
    """
    Prototype risk analysis using raw sensor thresholds and trends.

    IMPORTANT:
    These thresholds are demonstration values, not validated
    gas safety limits. This is not a trained ML model.
    """

    def __init__(self):
        # Keep the latest 10 readings for each sensor.
        self.mq2_history = deque(maxlen=10)
        self.mq7_history = deque(maxlen=10)

        # Demonstration thresholds for raw ADC readings.
        self.thresholds = {
            "mq2_warning": 400,
            "mq2_critical": 600,
            "mq7_warning": 450,
            "mq7_critical": 650,
        }

        # Used to detect rapid increases in readings.
        self.slope_threshold = 25

    def update_thresholds(self, new_thresholds):
        """Update thresholds after validating the configuration."""

        updated = self.thresholds.copy()

        for key in updated:
            if key in new_thresholds:
                value = new_thresholds[key]

                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    raise ValueError(f"{key} must be a number.")

                if not 0 <= value <= 4095:
                    raise ValueError(f"{key} must be between 0 and 4095.")

                updated[key] = value

        if updated["mq2_warning"] >= updated["mq2_critical"]:
            raise ValueError("MQ-2 warning must be below its critical threshold.")

        if updated["mq7_warning"] >= updated["mq7_critical"]:
            raise ValueError("MQ-7 warning must be below its critical threshold.")

        self.thresholds = updated

    @staticmethod
    def calculate_slope(history):
        """
        Estimate the average change in ADC counts per sample.
        This is a trend indicator, not a physical gas measurement.
        """

        if len(history) < 3:
            return 0.0

        return (history[-1] - history[0]) / (len(history) - 1)

    def analyse_sensor(self, sensor, value, history):
        """Classify a sensor reading and explain the result."""

        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{sensor} reading must be an integer.")

        if not 0 <= value <= 4095:
            raise ValueError(f"{sensor} reading must be between 0 and 4095.")

        history.append(value)
        slope = self.calculate_slope(history)

        warning_threshold = self.thresholds[f"{sensor}_warning"]
        critical_threshold = self.thresholds[f"{sensor}_critical"]

        if value >= critical_threshold:
            level = "CRITICAL"
            reason = (
                f"{sensor.upper()} raw reading ({value}) has reached "
                f"the demonstration critical threshold ({critical_threshold})."
            )

        elif value >= warning_threshold:
            level = "WARNING"
            reason = (
                f"{sensor.upper()} raw reading ({value}) has reached "
                f"the demonstration warning threshold ({warning_threshold})."
            )

        elif slope >= self.slope_threshold:
            level = "WARNING"
            reason = (
                f"{sensor.upper()} reading is rising rapidly: "
                f"{slope:.1f} ADC counts per sample."
            )

        else:
            level = "NORMAL"
            reason = "No configured threshold or rapid-rise condition detected."

        return {
            "sensor": sensor.upper(),
            "raw_reading": value,
            "slope": round(slope, 2),
            "level": level,
            "reason": reason,
            "anomaly": slope >= self.slope_threshold,
        }

    def analyse(self, mq2, mq7):
        """Analyse both sensors and return their individual and combined risks."""

        mq2_result = self.analyse_sensor(
            "mq2", mq2, self.mq2_history
        )

        mq7_result = self.analyse_sensor(
            "mq7", mq7, self.mq7_history
        )

        levels = {"NORMAL": 0, "WARNING": 1, "CRITICAL": 2}

        overall_level = max(
            [mq2_result["level"], mq7_result["level"]],
            key=lambda level: levels[level],
        )

        return {
            "overall_level": overall_level,
            "mq2": mq2_result,
            "mq7": mq7_result,
        }


if __name__ == "__main__":
    engine = RiskEngine()

    print("Testing the risk engine...\n")

    test_readings = [
        (250, 300),
        (260, 310),
        (280, 320),
        (420, 350),
        (620, 400),
    ]

    for mq2, mq7 in test_readings:
        result = engine.analyse(mq2, mq7)

        print(
            f"MQ-2: {mq2:4} | "
            f"MQ-7: {mq7:4} | "
            f"Overall risk: {result['overall_level']}"
        )

        print(f"  MQ-2: {result['mq2']['reason']}")
        print(f"  MQ-7: {result['mq7']['reason']}\n")