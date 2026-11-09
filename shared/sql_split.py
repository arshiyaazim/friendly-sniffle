"""SQL statement splitting shared by the migration runner and the tests.

Both `tests/conftest.py` and `scripts/apply_schema.py` need to run a migration
file statement by statement (the tests to tolerate pre-existing objects, the
installer to degrade gracefully when an optional extension is missing). Keeping
one implementation means a file that splits correctly in one place cannot split
wrongly in the other.

Handles: dollar-quoted blocks (`$$`/`$tag$`, which hold whole function bodies),
single-quoted strings with `''` escapes, line comments (pg_dump writes
"-- Name: x; Type: TABLE" — the semicolons are not statement ends), and
parenthesis depth so semicolons inside CREATE TABLE never split a statement.
"""
from __future__ import annotations


def split_sql(sql_text: str) -> list[str]:
    """Split SQL into individual statements, properly handling:
    - Dollar-quoted strings ($$ ... $$ or $tag$ ... $tag$)
    - Single-quoted strings ('...;...')
    - Parenthesis depth (semicolons inside CREATE TABLE)
    - Line comments (-- ...)
    """
    statements: list[str] = []
    current: list[str] = []
    paren_depth = 0
    i = 0
    n = len(sql_text)
    in_single_quote = False
    in_dollar_quote = False
    dollar_tag = ""
    
    while i < n:
        ch = sql_text[i]

        # Check for line comment (-- ...) — consume verbatim through end of
        # line so semicolons inside human-readable comment text (extremely
        # common in pg_dump output, e.g. "-- Name: x; Type: TABLE; ...") never
        # get treated as statement boundaries.
        if not in_single_quote and not in_dollar_quote and ch == "-" and i + 1 < n and sql_text[i + 1] == "-":
            eol = sql_text.find("\n", i)
            eol = n if eol == -1 else eol + 1
            current.append(sql_text[i:eol])
            i = eol
            continue

        # Check for dollar-quoted string start/end
        if not in_single_quote and ch == "$":
            # Try to match a dollar quote tag
            j = i + 1
            while j < n and (sql_text[j].isalnum() or sql_text[j] == "_"):
                j += 1
            if j < n and sql_text[j] == "$":
                tag = sql_text[i:j+1]  # $tag$ or $$
                if in_dollar_quote and tag == dollar_tag:
                    in_dollar_quote = False
                    dollar_tag = ""
                    current.append(sql_text[i:j+1])
                    i = j + 1
                    continue
                elif not in_dollar_quote:
                    in_dollar_quote = True
                    dollar_tag = tag
                    current.append(sql_text[i:j+1])
                    i = j + 1
                    continue
        
        if in_dollar_quote:
            current.append(ch)
            i += 1
            continue
        
        # Check for single-quoted string
        if ch == "'" and not in_dollar_quote:
            if in_single_quote:
                # Check for escaped quote ('')
                if i + 1 < n and sql_text[i+1] == "'":
                    current.append("''")
                    i += 2
                    continue
                else:
                    in_single_quote = False
            else:
                in_single_quote = True
            current.append(ch)
            i += 1
            continue
        
        if in_single_quote:
            current.append(ch)
            i += 1
            continue
        
        # Track parenthesis depth
        if ch == "(":
            paren_depth += 1
        elif ch == ")":
            paren_depth = max(0, paren_depth - 1)
        
        # Check for statement end (semicolon at paren_depth 0)
        if ch == ";" and paren_depth == 0:
            current.append(ch)
            statements.append("".join(current))
            current = []
            i += 1
            continue
        
        current.append(ch)
        i += 1
    
    if current:
        statements.append("".join(current))
    return statements
