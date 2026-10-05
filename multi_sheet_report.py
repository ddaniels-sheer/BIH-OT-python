import os
import pandas as pd
import snowflake.connector

from pathlib import Path
QUERY_DIR = Path(__file__).parent / "queries"
QUERIES = {}
for sql_file in sorted(QUERY_DIR.glob("*.sql")):        #picks up every sql file in the queries folder and adds it to the QUERIES dictionary
    sheet_name = sql_file.stem.split("_", 1)[1].replace("_", " ")
    QUERIES[sheet_name] = sql_file.read_text()

print("Looking in:", QUERY_DIR)
print("Folder exists:", QUERY_DIR.exists())
print("Files there:", [p.name for p in QUERY_DIR.iterdir()] if QUERY_DIR.exists() else "n/a")
print("Found queries:", list(QUERIES.keys()))

conn = snowflake.connector.connect(
    user=os.environ["SNOWFLAKE_USER"],
    account=os.environ["SNOWFLAKE_ACCOUNT"],
    warehouse=os.environ["SNOWFLAKE_WAREHOUSE"],
    password=os.environ["SNOWFLAKE_PASSWORD"],
    passcode=os.environ["SNOWFLAKE_PASSCODE"],
)

path = "weekly_report.xlsx"
with pd.ExcelWriter(path, engine="xlsxwriter") as writer:
    for sheet, sql in QUERIES.items():
        cur = conn.cursor()
        cur.execute(sql)
        df = cur.fetch_pandas_all()
        df.to_excel(writer, sheet_name=sheet, index=False)
        print(f"{sheet}: {len(df)} rows")

conn.close()
print("saved", path)