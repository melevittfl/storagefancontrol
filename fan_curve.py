"""
Fan curve controller. Maps temperature directly to fan speed
percentage through piecewise linear interpolation between
configured (temperature, percent) points.

Unlike a PID controller, the fan curve has no integral or derivative
state — fan speed is a pure function of current temperature. This
gives predictable, asymmetric behavior (cheap to be cool, expensive
to be hot) at the cost of not adapting to changing ambient
conditions.
"""


class FanCurve:
    def __init__(self, points):
        self.points = sorted(points, key=lambda p: p[0])
        self.last_output = 0
        self.last_temperature = 0

    @staticmethod
    def _parse_curve(curve_str):
        points = []
        for pair in curve_str.split(","):
            temp, percent = pair.strip().split(":")
            points.append((int(temp), int(percent)))
        return points

    def update(self, temperature):
        self.last_temperature = temperature
        points = self.points

        if temperature <= points[0][0]:
            output = points[0][1]
        elif temperature >= points[-1][0]:
            output = points[-1][1]
        else:
            output = points[0][1]
            for i in range(len(points) - 1):
                t1, p1 = points[i]
                t2, p2 = points[i + 1]
                if t1 <= temperature <= t2:
                    output = p1 + (p2 - p1) * (temperature - t1) / (t2 - t1)
                    break

        self.last_output = int(round(output))
        return self.last_output

    def reload(self, config):
        self.points = sorted(
            self._parse_curve(config.get("FanCurve", "curve")), key=lambda p: p[0]
        )

    def log_state(self):
        return "Mode: curve"
