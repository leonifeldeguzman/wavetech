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
        # --- Windy Point Forecast integration ---
    WINDY_API_URL = os.environ.get("WINDY_API_URL", "https://api.windy.com/api/point-forecast/v2")
    WINDY_API_KEY = os.environ.get("WINDY_API_KEY", "")
    WINDY_API_TIMEOUT_SECONDS = float(os.environ.get("WINDY_API_TIMEOUT_SECONDS", "10"))
    WINDY_LAT = float(os.environ.get("WINDY_LAT", "14.4"))   # Talim Island / Laguna de Bay
    WINDY_LON = float(os.environ.get("WINDY_LON", "121.2"))