"""Builds the Manifest Details PDF in memory. Nothing is written to disk."""
import re
from io import BytesIO
from xml.sax.saxutils import escape

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

from app.utils.ph_clock import ph_now

NAVY = colors.HexColor("#0f1b3c")
STRIPE = colors.HexColor("#f3f6fb")
LINE = colors.HexColor("#d5dce8")
MUTED = colors.HexColor("#6b7280")

_base = getSampleStyleSheet()
TITLE = ParagraphStyle("WTTitle", parent=_base["Title"], textColor=NAVY,
                       fontSize=20, alignment=0, spaceAfter=2)
SUBTITLE = ParagraphStyle("WTSub", parent=_base["Normal"], textColor=MUTED, fontSize=10)
HEADING = ParagraphStyle("WTHeading", parent=_base["Heading2"], textColor=NAVY,
                         fontSize=12, spaceBefore=4, spaceAfter=6)
LABEL = ParagraphStyle("WTLabel", parent=_base["Normal"], textColor=MUTED, fontSize=9.5)
VALUE = ParagraphStyle("WTValue", parent=_base["Normal"], fontName="Helvetica-Bold", fontSize=9.5)
CELL = ParagraphStyle("WTCell", parent=_base["Normal"], fontSize=9, leading=11)
HEAD = ParagraphStyle("WTHead", parent=_base["Normal"], fontName="Helvetica-Bold",
                      fontSize=9, textColor=colors.white)


def _text(value):
    """Escape user data for Paragraph (it parses XML-like markup); blank -> '-'."""
    if value is None or value == "":
        return "-"
    return escape(str(value))


def _footer(canvas, doc):
    canvas.saveState()
    canvas.setFont("Helvetica", 8)
    canvas.setFillColor(MUTED)
    canvas.drawString(doc.leftMargin, 12 * mm,
                      f"Generated {ph_now():%B %d, %Y %I:%M %p} (PH time)")
    canvas.drawRightString(A4[0] - doc.rightMargin, 12 * mm, f"Page {doc.page}")
    canvas.restoreState()


def _info_table(detail):
    rows = [
        ("Date:", detail["date_label"], "Capacity:", detail["capacity"]),
        ("Schedule:", detail["time_label"], "Passengers:", detail["passengers"]),
        ("Boat Name:", detail["boat_name"], "Status:", detail["status"]),
        ("Driver's Name:", detail["driver_name"], "", ""),
    ]
    data = [
        [Paragraph(a, LABEL), Paragraph(_text(b), VALUE),
         Paragraph(c, LABEL), Paragraph(_text(d) if c else "", VALUE)]
        for a, b, c, d in rows
    ]
    table = Table(data, colWidths=[32 * mm, 53 * mm, 32 * mm, 53 * mm])
    table.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 4),
        ("TOPPADDING", (0, 0), (-1, -1), 4),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
    ]))
    return table


def _passenger_table(passenger_list):
    header = [Paragraph(h, HEAD) for h in ("No.", "Name", "Age", "Address", "Time", "Type")]
    data = [header]
    for p in passenger_list:
        data.append([
            Paragraph(_text(p["no"]), CELL),
            Paragraph(_text(p["full_name"]), CELL),
            Paragraph(_text(p["age"]), CELL),
            Paragraph(_text(p["address"]), CELL),
            Paragraph(_text(p["check_in_label"]), CELL),
            Paragraph(_text(p["passenger_type"]), CELL),
        ])

    style = [
        ("BACKGROUND", (0, 0), (-1, 0), NAVY),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 5),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
        ("LINEBELOW", (0, 1), (-1, -1), 0.5, LINE),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, STRIPE]),
    ]
    if not passenger_list:
        data.append([Paragraph("No passengers on this manifest.", CELL)] + [""] * 5)
        style.append(("SPAN", (0, 1), (-1, 1)))

    # repeatRows=1 -> the navy header repeats on every page of a long manifest
    table = Table(data, colWidths=[12 * mm, 44 * mm, 12 * mm, 50 * mm, 24 * mm, 28 * mm],
                  repeatRows=1)
    table.setStyle(TableStyle(style))
    return table


def build_manifest_pdf(detail):
    """detail = analytics_service.get_manifest_detail(...). Returns PDF bytes."""
    buffer = BytesIO()
    doc = SimpleDocTemplate(
        buffer, pagesize=A4,
        leftMargin=20 * mm, rightMargin=20 * mm, topMargin=18 * mm, bottomMargin=22 * mm,
        title=f"Passenger Manifest - {detail['date_label']} {detail['time_label']}",
        author="WaveTech",
    )
    story = [
        Paragraph("WaveTech", TITLE),
        Paragraph("Passenger Manifest", SUBTITLE),
        Spacer(1, 8 * mm),
        _info_table(detail),
        Spacer(1, 6 * mm),
        Paragraph("Passenger List", HEADING),
        _passenger_table(detail["passenger_list"]),
    ]
    doc.build(story, onFirstPage=_footer, onLaterPages=_footer)
    return buffer.getvalue()


def manifest_filename(detail):
    """e.g. WaveTech-Manifest-June-18-2026-10-00-AM.pdf"""
    raw = f"WaveTech Manifest {detail['date_label']} {detail['time_label']}"
    return re.sub(r"[^A-Za-z0-9]+", "-", raw).strip("-") + ".pdf"