"""Manifest Management "Assess Condition" button -> shared Dashboard DSS card.

The button (Trip Information card) fetches /dashboard/dss-details/<trip_id>, which runs the same
scheduling_service.get_assessment() and renders the same
partials/dss_details_modal.html as the Dashboard "View Details" card.

Run with:
    python -m pytest app/tests/test_manifest_assess_condition.py -v
"""
from unittest.mock import patch

from app.services import scheduling_service


def test_trip_information_card_has_assess_button(admin_client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    html = admin_client.get(f"/manifests/{trip_id}").get_data(as_text=True)
    assert html.count('id="assessConditionBtn"') == 1
    assert f"/dashboard/dss-details/{trip_id}" in html
    # Sits in the Trip Information header, beside the Check-In indicator.
    header = html[html.index("Trip Information"):]
    assert header.index("assessConditionBtn") < header.index('manifest-checkin-status')


def test_manifest_list_is_not_expandable(admin_client, make_boat, make_trip):
    make_trip(boat_id=make_boat())
    html = admin_client.get("/manifests").get_data(as_text=True)
    assert "manifest-assess-btn" not in html
    assert "manifest-slot-toggle" not in html
    assert "manifest-slot-details" not in html


def test_details_returns_shared_dashboard_card(admin_client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    response = admin_client.get(f"/dashboard/dss-details/{trip_id}")
    assert response.status_code == 200
    assert response.headers["Cache-Control"] == "no-store"
    html = response.get_data(as_text=True)
    assert 'id="schedulingDetailsModal"' in html
    assert f"Trip #{trip_id}" in html


def test_details_uses_the_dashboard_assessment(admin_client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    with patch.object(
        scheduling_service, "get_assessment", wraps=scheduling_service.get_assessment
    ) as spy:
        admin_client.get(f"/dashboard/dss-details/{trip_id}")
    spy.assert_called_once()


def test_details_unknown_trip_is_404_json(admin_client):
    response = admin_client.get("/dashboard/dss-details/99999")
    assert response.status_code == 404
    assert response.get_json()["error"]


def test_details_failure_returns_503_not_an_empty_card(admin_client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    with patch.object(scheduling_service, "get_assessment", side_effect=RuntimeError("x")):
        response = admin_client.get(f"/dashboard/dss-details/{trip_id}")
    assert response.status_code == 503
    assert "schedulingDetailsModal" not in response.get_data(as_text=True)


def test_details_requires_login(client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    response = client.get(f"/dashboard/dss-details/{trip_id}")
    assert response.status_code in (301, 302, 401, 403)


def test_dashboard_view_details_still_renders_modal(admin_client, make_boat, make_trip):
    trip_id = make_trip(boat_id=make_boat())
    html = admin_client.get(f"/dashboard?trip_id={trip_id}").get_data(as_text=True)
    assert 'data-bs-target="#schedulingDetailsModal"' in html
    assert 'id="schedulingDetailsModal"' in html
