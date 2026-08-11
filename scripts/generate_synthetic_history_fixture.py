"""Generate a RAM-only ESP32 test fixture from a V1 offline-log CSV."""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


REQUIRED_FLAGS = ("integrityOk", "windOk", "airOk", "soilOk", "solar1Ok", "solar2Ok")
INTERVAL_MS = 5 * 60 * 1000
MAX_CONTINUOUS_GAP_MS = INTERVAL_MS + 60 * 1000
FIXTURE_COUNT = 288


def truthy(value: str) -> bool:
    return value.strip().lower() == "true"


def complete(row: dict[str, str]) -> bool:
    return all(truthy(row[name]) for name in REQUIRED_FLAGS)


def newest_continuous_window(rows: list[dict[str, str]]) -> list[dict[str, str]]:
    run: list[dict[str, str]] = []
    selected: list[dict[str, str]] = []
    previous_session: str | None = None
    previous_uptime: int | None = None
    for row in rows:
        session = row["bootSessionId"]
        uptime = int(row["uptimeMs"])
        gap = uptime - previous_uptime if previous_uptime is not None else 0
        if (not complete(row) or session != previous_session or gap <= 0 or
                gap > MAX_CONTINUOUS_GAP_MS):
            run = []
        if complete(row):
            run.append(row)
        if len(run) >= FIXTURE_COUNT:
            selected = run[-FIXTURE_COUNT:]
        previous_session = session
        previous_uptime = uptime
    if len(selected) != FIXTURE_COUNT:
        raise ValueError("CSV 中没有 288 条连续、完整的五分钟记录")
    return selected


def render(rows: list[dict[str, str]]) -> str:
    lines = [
        "#ifndef AIOT_SYNTHETIC_HISTORY_FIXTURE_H",
        "#define AIOT_SYNTHETIC_HISTORY_FIXTURE_H",
        "",
        "// Generated locally for RAM-only model testing. Never commit this file.",
        "#define AIOT_TEST_HISTORY_FIXTURE_ENABLED 1",
        "",
        "struct SyntheticHistoryFixtureSample {",
        "  float airTemperatureC;",
        "  float airHumidityPercent;",
        "  float airPressureHpa;",
        "  float soilTemperatureC;",
        "  float soilMoisturePercent;",
        "  float solarIncomingWm2;",
        "  float solarReflectedWm2;",
        "  float windSpeedMs;",
        "};",
        "",
        f"static constexpr size_t AIOT_TEST_HISTORY_FIXTURE_COUNT = {len(rows)};",
        "static const SyntheticHistoryFixtureSample AIOT_TEST_HISTORY_FIXTURE[] = {",
    ]
    for row in rows:
        values = (
            row["airTemperatureC"], row["airHumidityPercent"], row["airPressureHpa"],
            row["soilTemperatureC"], row["soilMoisturePercent"], row["solar2Wm2"],
            row["solar1Wm2"], row["windSpeedMs"],
        )
        lines.append("  {" + ", ".join(f"{float(value):.3f}f" for value in values) + "},")
    lines.extend(["};", "", "#endif  // AIOT_SYNTHETIC_HISTORY_FIXTURE_H", ""])
    return "\n".join(lines)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv", type=Path)
    parser.add_argument(
        "--output",
        type=Path,
        default=Path("firmware/esp32_s3_all_sensors/generated/synthetic_history_fixture.h"),
    )
    args = parser.parse_args()
    with args.csv.open(encoding="utf-8-sig", newline="") as stream:
        rows = list(csv.DictReader(stream))
    selected = newest_continuous_window(rows)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render(selected), encoding="utf-8")
    print(f"Generated {args.output} from {len(selected)} continuous CSV rows.")


if __name__ == "__main__":
    main()
