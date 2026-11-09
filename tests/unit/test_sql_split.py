"""`shared/sql_split.py` — the statement splitter both runners depend on.

`tests/conftest.py` and `scripts/apply_schema.py` execute migration files
statement by statement; if the splitter gets a boundary wrong, one of them
silently runs half a file (or a `CREATE FUNCTION … $$ … ; … $$` body in pieces).
These cases are the ones that actually occur in this repository's SQL.
"""
from __future__ import annotations

from shared.sql_split import split_sql


def _statements(text: str) -> list[str]:
    return [s.strip() for s in split_sql(text) if s.strip()]


def test_plain_statements_split_on_semicolons():
    assert _statements("SELECT 1; SELECT 2;") == ["SELECT 1;", "SELECT 2;"]


def test_trailing_statement_without_semicolon_is_kept():
    assert _statements("SELECT 1; SELECT 2") == ["SELECT 1;", "SELECT 2"]


def test_dollar_quoted_function_body_stays_one_statement():
    sql = """
CREATE OR REPLACE FUNCTION f() RETURNS void AS $$
BEGIN
  PERFORM 1;
  PERFORM 2;
END;
$$ LANGUAGE plpgsql;
SELECT 1;
"""
    statements = _statements(sql)
    assert len(statements) == 2, statements
    assert statements[0].endswith("LANGUAGE plpgsql;")
    assert "PERFORM 2;" in statements[0]


def test_tagged_dollar_quote_is_handled():
    sql = "CREATE FUNCTION g() RETURNS int AS $body$ SELECT 1; $body$ LANGUAGE sql; SELECT 2;"
    statements = _statements(sql)
    assert len(statements) == 2, statements


def test_semicolon_inside_a_string_literal_does_not_split():
    assert _statements("INSERT INTO t VALUES ('a;b');") == ["INSERT INTO t VALUES ('a;b');"]


def test_doubled_quote_escape_does_not_break_the_literal():
    sql = "INSERT INTO t VALUES ('it''s; fine'); SELECT 1;"
    statements = _statements(sql)
    assert len(statements) == 2, statements
    assert "it''s; fine" in statements[0]


def test_line_comments_with_semicolons_are_not_boundaries():
    """pg_dump writes `-- Name: x; Type: TABLE; Schema: public; Owner: -`."""
    sql = "-- Name: t; Type: TABLE; Schema: public;\nCREATE TABLE t (a int);\n"
    statements = _statements(sql)
    assert len(statements) == 1, statements
    assert "CREATE TABLE t (a int);" in statements[0]


def test_paren_depth_protects_create_table_and_check_constraints():
    sql = """
CREATE TABLE t (
    a int,
    b text CHECK (b IN ('x', 'y')),
    CONSTRAINT c CHECK (a > 0)
);
"""
    assert len(_statements(sql)) == 1


def test_double_dash_inside_a_dollar_quote_is_content():
    sql = "SELECT $$ -- not a comment; $$; SELECT 1;"
    assert len(_statements(sql)) == 2


def test_empty_and_comment_only_input_has_no_executable_statement():
    assert _statements("") == []
    assert _statements("   \n\t\n") == []
    # a comment-only chunk may come back as text (the splitter is not a parser),
    # but it must never look like something a caller would execute
    leftovers = _statements("-- just a comment\n")
    assert all(chunk.lstrip().startswith("--") for chunk in leftovers)


def test_unbalanced_parens_do_not_crash():
    # A broken file must still produce statements so the caller can report the
    # first real SQL error instead of hanging.
    assert len(_statements("CREATE TABLE t (a int;")) == 1


def test_baseline_file_splits_into_complete_statements():
    """The real file: nothing may be truncated mid-statement."""
    from pathlib import Path

    sql = (Path(__file__).resolve().parents[2] / "db" / "migrations"
           / "000_virgin_baseline.sql").read_text(encoding="utf-8")
    statements = _statements(sql)
    assert len(statements) >= 40
    truncated = [s for s in statements if not s.endswith(";")]
    assert not truncated, [s[-80:] for s in truncated]
    # the baseline must be idempotent — no unconditional destructive statement
    joined = " ".join(statements).lower()
    assert "drop table" not in joined
    assert "delete from" not in joined
