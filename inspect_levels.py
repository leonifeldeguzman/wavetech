import sys
import pandas as pd

path = sys.argv[1]
df = pd.read_csv(path) if path.lower().endswith(".csv") else pd.read_excel(path)

print("COLUMNS:", list(df.columns))
print("ROWS:", len(df))
print(df.dtypes, "\n")
print(df.head(5), "\n")
print(df.tail(5), "\n")

# Guess columns: first one with "date" in the name, first numeric one for level
date_col = next((c for c in df.columns if "date" in str(c).lower()), df.columns[0])
level_col = next((c for c in df.columns if c != date_col and pd.api.types.is_numeric_dtype(df[c])), None)
print("Using date column:", date_col, "| level column:", level_col, "\n")

dates = pd.to_datetime(df[date_col], errors="coerce")
print("Unparseable dates:", int(dates.isna().sum()))
print("Date range:", dates.min().date(), "to", dates.max().date())
print("Duplicate dates:", int(dates.duplicated().sum()))

full = pd.date_range(dates.min(), dates.max(), freq="D")
missing = full.difference(dates.dropna())
print("Expected days:", len(full), "| Missing days:", len(missing))
print("First missing days:", [d.date().isoformat() for d in missing[:10]], "\n")

if level_col is not None:
    lv = pd.to_numeric(df[level_col], errors="coerce")
    print("Missing/non-numeric levels:", int(lv.isna().sum()))
    print(lv.describe())