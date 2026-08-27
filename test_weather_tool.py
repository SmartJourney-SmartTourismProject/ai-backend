# test_weather_tool.py
# Run from your project root: python3 test_weather_tool.py
# Makes REAL calls to OpenWeather using your settings.openweather_api_key.

import asyncio
from datetime import date, timedelta
from app.tools.weather_tool import get_weather


async def main():
    # Kandy, Sri Lanka coordinates
    lat, lon = 7.2906, 80.6337

    today = date.today()
    dates = [(today + timedelta(days=i)).isoformat() for i in range(4)]

    result = await get_weather(lat, lon, dates)

    print("Requested dates:", dates)
    print("Result:", result)

    checks = []
    checks.append(("Returns a dict (not None)", result is not None))
    if result:
        checks.append(("Has 'current' key", "current" in result))
        checks.append(("Has 'forecast' key", "forecast" in result))
        if "current" in result:
            c = result["current"]
            checks.append(("current has temp/condition/humidity", all(k in c for k in ("temp", "condition", "humidity"))))
        if "forecast" in result:
            checks.append(("forecast is non-empty", len(result["forecast"]) > 0))
            if result["forecast"]:
                f0 = result["forecast"][0]
                checks.append(("forecast entries have expected keys", all(
                    k in f0 for k in ("date", "temp_min", "temp_max", "condition", "rain_probability")
                )))

    passed = 0
    for label, ok in checks:
        if ok:
            passed += 1
        print(f"[{'PASS' if ok else 'FAIL'}] {label}")

    print(f"\n{passed}/{len(checks)} checks passed")


if __name__ == "__main__":
    asyncio.run(main())