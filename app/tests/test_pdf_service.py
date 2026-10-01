from app.services.pdf_service import build_manifest_pdf, manifest_filename


def _detail(passengers=None, boat_name="MB Coral", capacity=15):
    passengers = passengers if passengers is not None else [
        {"no": 1, "full_name": "Mark Tan", "age": 19, "address": "Brgy. Tagapo",
         "passenger_type": "Student", "check_in_label": "09:47:12"},
    ]
    return {
        "trip_id": 1,
        "date_label": "June 18, 2026",
        "time_label": "10:00 AM",
        "boat_name": boat_name,
        "driver_name": "Juan Dela Cruz",
        "capacity": capacity,
        "passengers": len(passengers),
        "status": "Departed",
        "passenger_list": passengers,
    }


def _assert_pdf(data):
    assert data.startswith(b"%PDF")
    assert data.rstrip().endswith(b"%%EOF")


def test_builds_valid_pdf():
    _assert_pdf(build_manifest_pdf(_detail()))


def test_empty_manifest_and_no_boat():
    _assert_pdf(build_manifest_pdf(_detail(passengers=[], boat_name=None, capacity=None)))


def test_markup_characters_in_names_do_not_crash():
    rows = [{"no": 1, "full_name": "Ana <b>& Co", "age": None, "address": "Biñan <Laguna>",
             "passenger_type": "Regular", "check_in_label": "—"}]
    _assert_pdf(build_manifest_pdf(_detail(passengers=rows)))


def test_long_manifest_spans_pages():
    rows = [{"no": i, "full_name": f"Passenger {i}", "age": 30, "address": "Brgy. Dita",
             "passenger_type": "Regular", "check_in_label": "09:00:00"} for i in range(1, 80)]
    _assert_pdf(build_manifest_pdf(_detail(passengers=rows)))


def test_filename_is_safe():
    assert manifest_filename(_detail()) == "WaveTech-Manifest-June-18-2026-10-00-AM.pdf"