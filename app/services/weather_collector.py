"""
Background weather collector.

Calls monitoring_service.get_station_weather() on a fixed interval so
per-station weather history accumulates even when nobody opens a page.
It stores only what Open-Meteo returns (weather-MODEL data); nothing is
generated or filled in. If a fetch fails, no row is written and the gap stays.

Same pattern as the announcement scheduler: one daemon thread per process,
guarded by a lock so a second call is a safe no-op.
"""

import threading

DEFAULT_COLLECTOR_INTERVAL_SECONDS = 3600  # once an hour

_collector_thread: threading.Thread | None = None
_collector_stop_event: threading.Event | None = None
_collector_lock = threading.Lock()


def start_weather_collector(app, interval_seconds: int | None = None) -> bool:
    """Start the collector thread. Returns False if one is already running."""
    global _collector_thread, _collector_stop_event

    if interval_seconds is None:
        interval_seconds = app.config.get(
            "WEATHER_COLLECTOR_INTERVAL_SECONDS",
            DEFAULT_COLLECTOR_INTERVAL_SECONDS,
        )

    with _collector_lock:
        if _collector_thread is not None and _collector_thread.is_alive():
            return False

        stop_event = threading.Event()

        def _run():
            # Collect once at startup, then once per interval.
            # wait() returns True as soon as stop_event is set.
            while True:
                try:
                    with app.app_context():
                        # Imported here to avoid a circular import at startup.
                        from app.services import monitoring_service
                        monitoring_service.get_station_weather()
                except Exception:
                    app.logger.exception("Background weather collection failed")
                if stop_event.wait(interval_seconds):
                    break

        thread = threading.Thread(
            target=_run, name="weather-collector", daemon=True
        )
        _collector_thread = thread
        _collector_stop_event = stop_event
        thread.start()
        return True


def stop_weather_collector(timeout: float = 2) -> None:
    """Stop the collector (mainly so tests don't leak a thread)."""
    global _collector_thread, _collector_stop_event

    with _collector_lock:
        if _collector_stop_event is not None:
            _collector_stop_event.set()
        thread, _collector_thread = _collector_thread, None
        _collector_stop_event = None

    if thread is not None:
        thread.join(timeout=timeout)