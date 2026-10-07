from datetime import timezone, timedelta
from app import create_app          # ADAPT if create_app lives elsewhere
from app.services import windy_service as ws

STATIONS = {
    "I West Bay":        (14.41701, 121.17404),
    "IV Central Bay":    (14.38580, 121.28019),
    "VIII South Bay":    (14.23827, 121.23266),
    "XVII Sanctuary":    (14.29127, 121.26970),
    "XX Diablo Pass":    (14.42220, 121.22297),
    "XXI Cardona":       (14.34247, 121.28668),
    "XXII Jala-jala":    (14.25513, 121.27119),
}
PH = timezone(timedelta(hours=8))
STEPS = 4  # first 4 forecast points

app = create_app()
with app.app_context():
    cfg = app.config
    original = (cfg["WINDY_LAT"], cfg["WINDY_LON"])
    results = {}
    try:
        for name, (lat, lon) in STATIONS.items():
            cfg["WINDY_LAT"], cfg["WINDY_LON"] = lat, lon
            data, retrieved_at = ws._request_forecast()
            rows = []
            for i in range(min(STEPS, len(data["ts"]))):
                c = ws._conditions_from_index(data, i, retrieved_at)
                rows.append((
                    c.recorded_at.astimezone(PH).strftime("%a %d %H:%M"),
                    c.wind_speed_kmh, c.wind_direction,
                    c.temperature_c, c.weather_condition,
                ))
            results[name] = rows
    finally:
        cfg["WINDY_LAT"], cfg["WINDY_LON"] = original

    for name, rows in results.items():
        print(f"\n{name}")
        for t, spd, d, temp, cond in rows:
            print(f"  {t}  {spd:>5} km/h {d:<3} {temp:>5}C  {cond}")

    groups = {}
    for name, rows in results.items():
        key = tuple(r[1:] for r in rows)   # values only, ignore time label
        groups.setdefault(key, []).append(name)

    print("\nGroups of stations with IDENTICAL values:")
    for members in groups.values():
        print("  -", ", ".join(members))
    print(f"\n{len(groups)} distinct forecast(s) across {len(STATIONS)} stations")