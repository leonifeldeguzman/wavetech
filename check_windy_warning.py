from app import create_app
from app.services import windy_service as ws

app = create_app()
with app.app_context():
    data, _ = ws._request_forecast()
    print("warning:", data.get("warning"))