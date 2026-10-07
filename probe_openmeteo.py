import requests

STATIONS = {
    "I West Bay":        (14.41701, 121.17404),
    "IV Central Bay":    (14.38580, 121.28019),
    "VIII South Bay":    (14.23827, 121.23266),
    "XVII Sanctuary":    (14.29127, 121.26970),
    "XX Diablo Pass":    (14.42220, 121.22297),
    "XXI Cardona":       (14.34247, 121.28668),
    "XXII Jala-jala":    (14.25513, 121.27119),
}
HOURS = [11, 14, 17, 20]   # PH local hours of today
DIRS = ["N", "NE", "E", "SE", "S", "SW", "W", "NW"]

results = {}
for name, (lat, lon) in STATIONS.items():
    r = None
    for attempt in range(1, 4):
        try:
            r = requests.get(
                "https://api.open-meteo.com/v1/forecast",
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "hourly": "temperature_2m,wind_speed_10m,wind_direction_10m,cloud_cover,precipitation",
                    "timezone": "Asia/Manila",
                    "forecast_days": 1,
                },
                timeout=30,
            )
            break
        except requests.exceptions.RequestException as exc:
            print(f"  {name}: attempt {attempt} failed ({type(exc).__name__})")
    if r is None:
        raise SystemExit(f"Could not reach Open-Meteo for {name} after 3 tries.")
    r.raise_for_status()
    j = r.json()
    h = j["hourly"]
    rows = []
    for hr in HOURS:
        rows.append((
            h["time"][hr][-5:],
            h["wind_speed_10m"][hr],
            DIRS[round(h["wind_direction_10m"][hr] / 45) % 8],
            h["temperature_2m"][hr],
            h["cloud_cover"][hr],
            h["precipitation"][hr],
        ))
    results[name] = rows
    print(f"\n{name}  (grid cell used: {j['latitude']}, {j['longitude']})")
    for t, spd, d, temp, cloud, rain in rows:
        print(f"  {t}  {spd:>5} km/h {d:<3} {temp:>5}C  cloud {cloud:>3}%  rain {rain} mm")

groups = {}
for name, rows in results.items():
    groups.setdefault(tuple(r[1:] for r in rows), []).append(name)

print("\nGroups of stations with IDENTICAL values:")
for members in groups.values():
    print("  -", ", ".join(members))
print(f"\n{len(groups)} distinct forecast(s) across {len(STATIONS)} stations")