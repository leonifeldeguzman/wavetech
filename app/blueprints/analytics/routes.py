from datetime import datetime

from flask import Response, abort, current_app, render_template, request

from app.blueprints.analytics import analytics_bp
from app.models.boat import Boat
from app.services import analytics_service, pdf_service
from app.utils.decorators import login_required


def _parse_date(value):
    """'YYYY-MM-DD' -> date, or None for blank/invalid input."""
    try:
        return datetime.strptime(value, "%Y-%m-%d").date() if value else None
    except ValueError:
        return None


@analytics_bp.route("/analytics")
@login_required
def index():
    # Read-only page. KPIs and charts always show today (per the mockup);
    # only the Manifest Records table reacts to the search/filter query string.
    summary = analytics_service.get_daily_summary()

    # "Target: 80%" on the Avg Capacity card. Falls back to 80 until it is
    # added to config.py (or, later, Admin Settings).
    capacity_target_pct = current_app.config.get("ANALYTICS_CAPACITY_TARGET_PCT", 80)

    # Per-trip rows (cancelled trips excluded) for both charts.
    breakdown = analytics_service.get_trip_breakdown()
    for row in breakdown:
        row["label"] = row["departure_ph"].strftime("%I:%M %p").lstrip("0")

    # Manifest Records: search + filter come from the query string.
    search = (request.args.get("q") or "").strip()
    date_from = _parse_date(request.args.get("date_from"))
    date_to = _parse_date(request.args.get("date_to"))
    boat_id = request.args.get("boat_id", type=int)
    status = request.args.get("status")
    if status not in analytics_service.TRIP_STATUSES:
        status = None

    records = analytics_service.get_manifest_records(
        search=search,
        date_from=date_from,
        date_to=date_to,
        boat_id=boat_id,
        status=status,
    )

    filters = {
        "q": search,
        "date_from": date_from.isoformat() if date_from else "",
        "date_to": date_to.isoformat() if date_to else "",
        "boat_id": boat_id,
        "status": status or "",
    }

    # Manifest Details: ?view=<trip_id> opens the details card under the table.
    view_id = request.args.get("view", type=int)
    detail = analytics_service.get_manifest_detail(view_id) if view_id else None

    # Search/filter values to carry along when opening or closing a detail,
    # so the table stays exactly as the operator left it.
    keep_args = {
        key: value
        for key, value in {
            "q": search,
            "date_from": filters["date_from"],
            "date_to": filters["date_to"],
            "boat_id": boat_id,
            "status": status,
        }.items()
        if value
    }

    return render_template(
        "analytics/index.html",
        summary=summary,
        capacity_target_pct=capacity_target_pct,
        breakdown=breakdown,
        chart_labels=[r["label"] for r in breakdown],
        chart_passengers=[r["passengers"] for r in breakdown],
        records=records,
        filters=filters,
        filters_active=bool(date_from or date_to or boat_id or status),
        boats=Boat.query.order_by(Boat.name).all(),
        statuses=analytics_service.TRIP_STATUSES,
        detail=detail,
        selected_trip_id=view_id,
        keep_args=keep_args,
        active_page="analytics",
    )


@analytics_bp.route("/analytics/export/<int:trip_id>")
@login_required
def export_manifest(trip_id):
    # Read-only: builds the PDF in memory and sends it as a download.
    detail = analytics_service.get_manifest_detail(trip_id)
    if detail is None:
        abort(404)

    pdf_bytes = pdf_service.build_manifest_pdf(detail)
    filename = pdf_service.manifest_filename(detail)
    return Response(
        pdf_bytes,
        mimetype="application/pdf",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )