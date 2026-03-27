"""
SQLite database setup for CFIP.

Uses Python's built-in sqlite3 module — no ORM, no extra dependency.
C# analogy: a lightweight DbContext without Entity Framework.

Database file: cfip.db in the project root.
Dev: SQLite. Prod: PostgreSQL (same schema, different connection string).

Tables:
  payer_rules     — PA criteria and thresholds per payer + drug class
  denial_patterns — Known denial reasons with frequency and recommendations
"""

import sqlite3
from contextlib import contextmanager
from pathlib import Path

# Database file sits at the project root alongside .env and pyproject.toml.
# Path(__file__) = app/data/db.py → .parent = app/data → .parent = app → .parent = project root
DB_PATH = Path(__file__).parent.parent.parent / "cfip.db"

# SQL to create tables — executed once on first startup.
# IF NOT EXISTS makes this idempotent (safe to run multiple times).
CREATE_PAYER_RULES = """
CREATE TABLE IF NOT EXISTS payer_rules (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    payer_name  TEXT    NOT NULL,
    drug_class  TEXT    NOT NULL,
    rule_type   TEXT    NOT NULL,   -- 'step_therapy' | 'clinical_criteria' | 'documentation' | 'baseline'
    rule_key    TEXT    NOT NULL,   -- e.g. 'min_metformin_days', 'min_a1c', 'min_bmi'
    rule_value  TEXT    NOT NULL,   -- stored as text; cast to float/int at query time
    description TEXT    NOT NULL
);
"""

CREATE_DENIAL_PATTERNS = """
CREATE TABLE IF NOT EXISTS denial_patterns (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    payer_name      TEXT    NOT NULL,
    drug_class      TEXT    NOT NULL,
    denial_reason   TEXT    NOT NULL,   -- e.g. 'step_therapy_not_met'
    frequency       REAL    NOT NULL,   -- 0.0-1.0 fraction of denials with this reason
    recommendation  TEXT    NOT NULL    -- what to do to avoid this denial
);
"""

CREATE_INDEX_PAYER_RULES = """
CREATE INDEX IF NOT EXISTS idx_payer_rules_lookup
    ON payer_rules (payer_name, drug_class);
"""

CREATE_INDEX_DENIAL_PATTERNS = """
CREATE INDEX IF NOT EXISTS idx_denial_patterns_lookup
    ON denial_patterns (payer_name, drug_class);
"""

CREATE_CPIC_RULES = """
CREATE TABLE IF NOT EXISTS cpic_rules (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    drug_name           TEXT    NOT NULL,   -- e.g. "clopidogrel", "warfarin"
    gene                TEXT    NOT NULL,   -- e.g. "CYP2C19", "CYP2C9"
    diplotype_pattern   TEXT    NOT NULL,   -- e.g. "*2/*2", "*1/*2"
    metabolizer_status  TEXT    NOT NULL,   -- "poor_metabolizer" | "intermediate" | "normal" | "rapid" | "ultrarapid"
    recommendation      TEXT    NOT NULL,   -- CPIC guideline text
    alternative_drug    TEXT,               -- nullable — e.g. "prasugrel, ticagrelor"
    severity            TEXT    NOT NULL,   -- "high" | "moderate" | "low" | "none"
    evidence_level      TEXT    NOT NULL    -- CPIC evidence grading: "1A" | "1B" | "2A" | "2B"
);
"""

CREATE_INDEX_CPIC_RULES = """
CREATE INDEX IF NOT EXISTS idx_cpic_rules_lookup
    ON cpic_rules (drug_name, gene);
"""


def init_db() -> None:
    """
    Create tables and indexes if they don't exist.
    Called once at application startup from app/main.py lifespan.
    C# analogy: DbContext.Database.EnsureCreated() or a migration runner.

    Note: executescript() is intentionally avoided here — it auto-commits
    any open transaction, which breaks our context manager's commit/rollback
    flow. Individual execute() calls stay within the same transaction.
    """
    with get_connection() as conn:
        conn.execute(CREATE_PAYER_RULES)
        conn.execute(CREATE_DENIAL_PATTERNS)
        conn.execute(CREATE_INDEX_PAYER_RULES)
        conn.execute(CREATE_INDEX_DENIAL_PATTERNS)
        conn.execute(CREATE_CPIC_RULES)
        conn.execute(CREATE_INDEX_CPIC_RULES)
    # The context manager commits automatically on exit (see get_connection below)


@contextmanager
def get_connection():
    """
    Context manager that yields an open SQLite connection and commits/rolls back.

    @contextmanager turns a generator function into a context manager — the code
    before `yield` is setup, code after `yield` is teardown.
    C# analogy: a using block around SqlConnection with try/catch for rollback.

    Usage:
        with get_connection() as conn:
            rows = conn.execute("SELECT * FROM payer_rules").fetchall()
        # connection is closed automatically here
    """
    # isolation_level=None disables SQLite's implicit transaction handling so we
    # control commits explicitly. detect_types parses dates/timestamps automatically.
    conn = sqlite3.connect(
        DB_PATH,
        isolation_level=None,
        detect_types=sqlite3.PARSE_DECLTYPES,
    )
    # row_factory makes rows behave like dicts: row["payer_name"] instead of row[0]
    # C# analogy: like having named columns on a DataRow instead of ordinal access
    conn.row_factory = sqlite3.Row

    try:
        conn.execute("BEGIN")   # start explicit transaction
        yield conn
        conn.execute("COMMIT")  # commit if no exception
    except Exception:
        conn.execute("ROLLBACK")
        raise  # re-raise so the caller knows something went wrong
    finally:
        conn.close()            # always close, even on exception


def get_db():
    """
    Simple non-context-manager access for read-only queries.

    Returns a connection with row_factory set.
    Caller is responsible for closing it.
    Use get_connection() for writes (ensures commit/rollback).

    Usage:
        conn = get_db()
        try:
            rows = conn.execute("SELECT ...").fetchall()
        finally:
            conn.close()
    """
    conn = sqlite3.connect(DB_PATH, detect_types=sqlite3.PARSE_DECLTYPES)
    conn.row_factory = sqlite3.Row
    return conn
