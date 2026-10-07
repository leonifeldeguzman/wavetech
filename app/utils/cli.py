import click
import pandas as pd
from datetime import datetime
from flask.cli import with_appcontext

from app.extensions import db
from app.models.historical_water_level import HistoricalWaterLevel
from app.models.lake_station import LakeStation


def _to_date(label, year):
    """'Jan 01' + 2024 -> date(2024, 1, 1).

    Returns None for anything that isn't a real calendar day: blank rows,
    the Average/Maximum/Minimum rows, and Feb 29 in non-leap years.
    """
    if pd.isna(label):
        return None
    try:
        return datetime.strptime(f"{str(label).strip()} {year}", "%b %d %Y").date()
    except ValueError:
        return None


@click.command("import-historical-levels")
@click.argument("path", type=click.Path(exists=True, dir_okay=False))
@click.option("--dry-run", is_flag=True, help="Show what would happen, write nothing.")
@with_appcontext
def import_historical_levels(path, dry_run):
    """Import the LLDA daily lake-level workbook (wide format) into
    historical_water_levels. Safe to re-run: existing dates are skipped."""
    df = pd.read_excel(path, sheet_name=0)
    label_col = df.columns[0]
    year_cols = [c for c in df.columns[1:] if str(c).strip().isdigit()]

    rows = []  # (date, level)
    for _, r in df.iterrows():
        for yc in year_cols:
            value = r[yc]
            if pd.isna(value):
                continue
            d = _to_date(r[label_col], int(str(yc).strip()))
            if d is None:
                continue
            rows.append((d, float(value)))

    click.echo(f"Valid readings found in file: {len(rows)}")
    for yc in year_cols:
        n = sum(1 for d, _ in rows if d.year == int(str(yc).strip()))
        click.echo(f"  {yc}: {n} days")
    if rows:
        click.echo(f"  Level range: {min(v for _, v in rows):.3f} to {max(v for _, v in rows):.3f}")

    existing = {
        d: lvl for d, lvl in db.session.query(
            HistoricalWaterLevel.date, HistoricalWaterLevel.level_m
        )
    }
    new_rows = [(d, v) for d, v in rows if d not in existing]
    conflicts = [(d, v, existing[d]) for d, v in rows
                 if d in existing and abs(existing[d] - v) > 1e-9]

    click.echo(f"Already in database (skipped): {len(rows) - len(new_rows)}")
    click.echo(f"To insert: {len(new_rows)}")
    if conflicts:
        click.echo(f"WARNING: {len(conflicts)} existing dates have a different level "
                   f"in the file (not changed). First: {conflicts[0]}")

    if dry_run:
        click.echo("Dry run: nothing written.")
        return

    db.session.add_all(
        HistoricalWaterLevel(date=d, level_m=v, unit="m", source="LLDA historical")
        for d, v in new_rows
    )
    db.session.commit()
    click.echo(f"Inserted {len(new_rows)} rows.")



STATIONS = [
    ("I",    "West Bay",         14.41701, 121.17404),
    ("IV",   "Central Bay",      14.38580, 121.28019),
    ("VIII", "South Bay",        14.23827, 121.23266),
    ("XVII", "Sanctuary",        14.29127, 121.26970),
    ("XX",   "GEMS-Diablo Pass", 14.42220, 121.22297),
    ("XXI",  "Cardona",          14.34247, 121.28668),
    ("XXII", "Jala-jala",        14.25513, 121.27119),
]


@click.command("seed-lake-stations")
@with_appcontext
def seed_lake_stations():
    """Insert the 7 LLDA stations. Safe to re-run: existing station numbers are skipped."""
    existing = {s for (s,) in db.session.query(LakeStation.station_no)}
    added = 0
    for no, name, lat, lon in STATIONS:
        if no in existing:
            continue
        db.session.add(LakeStation(station_no=no, name=name,
                                   latitude=lat, longitude=lon))
        added += 1
    db.session.commit()
    click.echo(f"Inserted {added} stations ({len(STATIONS) - added} already present).")


def register_cli(app):
    app.cli.add_command(import_historical_levels)
    app.cli.add_command(seed_lake_stations)