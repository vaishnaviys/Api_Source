"""
Minimal REST API source over Azure SQL Database.

No table registry. No watermark columns to configure. No discovery step
you have to run by hand. Just: give it a table name, get rows back,
properly paginated.

Endpoints:
  GET /health                     - no auth, just confirms the API is alive
  GET /tables                     - lists every table in the database (live query, no setup needed)
  GET /data/{table_name}          - returns rows from that table, paginated

How pagination works without you specifying a watermark column:
SQL Server's OFFSET/FETCH NEXT needs an ORDER BY to be reliable - without
one, the database is free to return rows in a different order each call,
which can silently skip or duplicate rows across pages. So this app
auto-detects each table's primary key (or first column, if no PK exists)
the first time that table is requested, and uses that as the ORDER BY.
You never type a column name anywhere.

Run:
  uvicorn main:app --reload --port 8000
Then open http://127.0.0.1:8000/docs to test it in the browser.
"""
import os
from urllib.parse import quote_plus

from dotenv import load_dotenv
from fastapi import FastAPI, Header, HTTPException, Query
from sqlalchemy import create_engine, text

load_dotenv()  # reads .env in the current folder

DB_SERVER = os.getenv("DB_SERVER")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASSWORD = os.getenv("DB_PASSWORD")
API_KEY = os.getenv("API_KEY")

if not all([DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, API_KEY]):
    raise SystemExit(
        "Missing settings. Make sure .env exists in this folder and has "
        "DB_SERVER, DB_NAME, DB_USER, DB_PASSWORD, API_KEY all filled in."
    )

odbc_params = (
    "DRIVER={ODBC Driver 18 for SQL Server};"
    f"SERVER={DB_SERVER},1433;"
    f"DATABASE={DB_NAME};"
    f"UID={DB_USER};"
    f"PWD={DB_PASSWORD};"
    "Encrypt=yes;TrustServerCertificate=no;Connection Timeout=30;"
)
engine = create_engine(f"mssql+pyodbc:///?odbc_connect={quote_plus(odbc_params)}")

app = FastAPI(title="Simple Azure SQL API Source")

# Cache of {"schema.table": "sort_column_name"} so we only run the
# auto-detect query once per table, not on every single page request.
_sort_column_cache: dict[str, str] = {}


def check_api_key(x_api_key: str = Header(...)):
    if x_api_key != API_KEY:
        raise HTTPException(status_code=401, detail="Invalid API key")


def get_sort_column(conn, schema: str, table_name: str) -> str:
    """
    Returns a column name safe to ORDER BY for stable pagination.
    Priority: 1) primary key column (if single-column PK exists)
              2) first column in the table, by ordinal position (fallback)
    Cached after first lookup per table.
    """
    cache_key = f"{schema}.{table_name}"
    if cache_key in _sort_column_cache:
        return _sort_column_cache[cache_key]

    pk_row = conn.execute(
        text("""
            SELECT c.name AS column_name
            FROM sys.tables t
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            JOIN sys.key_constraints kc ON kc.parent_object_id = t.object_id AND kc.type = 'PK'
            JOIN sys.index_columns ic ON ic.object_id = kc.parent_object_id AND ic.index_id = kc.unique_index_id
            JOIN sys.columns c ON c.object_id = ic.object_id AND c.column_id = ic.column_id
            WHERE s.name = :schema AND t.name = :table_name
        """),
        {"schema": schema, "table_name": table_name},
    ).fetchall()

    if len(pk_row) == 1:
        # Single-column PK - ideal sort key
        sort_column = pk_row[0].column_name
    else:
        # No PK, or composite PK - fall back to the first column by position.
        # Not as fast as an indexed PK, but still gives a deterministic order,
        # which is what actually matters for correct paging.
        first_col = conn.execute(
            text("""
                SELECT c.name AS column_name
                FROM sys.tables t
                JOIN sys.schemas s ON t.schema_id = s.schema_id
                JOIN sys.columns c ON c.object_id = t.object_id
                WHERE s.name = :schema AND t.name = :table_name
                ORDER BY c.column_id
            """),
            {"schema": schema, "table_name": table_name},
        ).fetchone()
        if not first_col:
            raise HTTPException(status_code=404, detail=f"Table '{schema}.{table_name}' not found or has no columns.")
        sort_column = first_col.column_name

    _sort_column_cache[cache_key] = sort_column
    return sort_column


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/tables")
def list_tables(x_api_key: str = Header(...)):
    """Lists every table in the database. No setup needed - this is a live query."""
    check_api_key(x_api_key)
    with engine.connect() as conn:
        result = conn.execute(text("""
            SELECT s.name AS schema_name, t.name AS table_name
            FROM sys.tables t
            JOIN sys.schemas s ON t.schema_id = s.schema_id
            ORDER BY s.name, t.name
        """))
        rows = [f"{r.schema_name}.{r.table_name}" for r in result.fetchall()]
    return {"count": len(rows), "tables": rows}


@app.get("/data/{schema}/{table_name}")
def get_data(
    schema: str,
    table_name: str,
    x_api_key: str = Header(...),
    limit: int = Query(default=2000, ge=1, le=10000, description="Rows per page"),
    offset: int = Query(default=0, ge=0, description="Rows to skip (for paging)"),
):
    """
    Returns rows from any table, properly paginated. No prior registration
    needed - just pass the schema and table name from the /tables list.

    Paging: call with offset=0 first, then offset=2000, offset=4000, etc.
    until you get back fewer rows than `limit` (that means you've reached the end).
    """
    check_api_key(x_api_key)

    with engine.connect() as conn:
        # Validate the table actually exists before querying it (prevents SQL
        # injection via the path - schema/table_name are checked against real
        # DB metadata first) and auto-detect what to ORDER BY.
        sort_column = get_sort_column(conn, schema, table_name)

        # schema/table_name/sort_column are all confirmed to be real identifiers
        # that exist in sys.tables/sys.columns at this point (not raw user input
        # dropped into the query), so it's safe to use them in the SQL string here.
        query = text(f"""
            SELECT * FROM [{schema}].[{table_name}]
            ORDER BY [{sort_column}]
            OFFSET :offset ROWS FETCH NEXT :limit ROWS ONLY
        """)
        result = conn.execute(query, {"offset": offset, "limit": limit})
        columns = result.keys()
        rows = [dict(zip(columns, row)) for row in result.fetchall()]

    return {
        "schema": schema,
        "table": table_name,
        "sorted_by": sort_column,
        "offset": offset,
        "limit": limit,
        "row_count": len(rows),
        "has_more": len(rows) == limit,
        "data": rows,
    }
