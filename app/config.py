import os
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get("SECRET_KEY", "dev-secret-key-change-later")
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # --- LLDA environmental data integration ---
    # As of this writing, LLDA has NOT published an official machine-readable
    # (API/JSON/XML/CSV) endpoint for Laguna de Bay water-level data — see
    # docs/llda_integration.md for the research behind this. LLDA_API_URL is
    # therefore expected to be BLANK in every environment until WaveTech is
    # given a real, documented endpoint (e.g. through a data-sharing
    # agreement or a future LLDA open-data release). Leaving it blank is not
    # a bug: the service is designed to fail safely into "no live source
    # configured" and fall back to cached/manual data, per the SRS.
    LLDA_API_URL = os.environ.get("LLDA_API_URL", "")
    LLDA_API_KEY = os.environ.get("LLDA_API_KEY", "")
    LLDA_API_TIMEOUT_SECONDS = float(os.environ.get("LLDA_API_TIMEOUT_SECONDS", "10"))

    # --- Windy Point Forecast integration (wind/weather/temperature) ---
    # Point Forecast API: https://api.windy.com/point-forecast/v2
    WINDY_API_URL = os.environ.get("WINDY_API_URL", "https://api.windy.com/api/point-forecast/v2")
    WINDY_API_KEY = os.environ.get("WINDY_API_KEY", "")
    # Default point is Central Bay (Cardona, Rizal) — the same reference
    # station used as the default location_label on EnvironmentalReading.
    WINDY_LAT = float(os.environ.get("WINDY_LAT", "14.387"))
    WINDY_LON = float(os.environ.get("WINDY_LON", "121.243"))
    WINDY_API_TIMEOUT_SECONDS = float(os.environ.get("WINDY_API_TIMEOUT_SECONDS", "10"))

    # --- Lake Monitoring dashboard auto-refresh ---
    # How often the monitoring dashboard re-checks LLDA/Windy for new data.
    # Unit: SECONDS (the template converts to milliseconds for
    # setInterval()). Kept well above typical API rate limits — do not set
    # this below ~30s without checking Windy's plan rate limits.
    REFRESH_INTERVAL_SECONDS = int(os.environ.get("REFRESH_INTERVAL_SECONDS", "600"))

    # --- WaveTech Monitoring Map ---
    # Default monitoring location: Talim Island, Laguna de Bay
    # --- WaveTech Monitoring Map ---
    MONITORING_LAT = float(os.environ.get("MONITORING_LAT", "14.317527"))
    MONITORING_LON = float(os.environ.get("MONITORING_LON", "121.184594"))
    MONITORING_LOCATION_LABEL = os.environ.get(
        "MONITORING_LOCATION_LABEL",
        "Talim Island, Laguna de Bay"
    )