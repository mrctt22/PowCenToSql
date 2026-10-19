"""PowerCenter expression -> T-SQL (SQL Server) translator.

This is a best-effort, *extensible* translator for the skeleton.  It handles
the constructs that appear in real mappings (IIF, ISNULL, REPLACESTR,
DECODE, SUBSTR, IN / NOT IN, conditional aggregates) and leaves anything it
does not know intact, so the pipeline never silently drops business logic.

Extend by adding more cases to ``_translate_scalars`` or by registering a
function in ``_translate_call``.

Any ``NAME(`` that survives into the final translated text and is not one of
the constructs the passes above already handle, nor a recognised standard
T-SQL function (see ``_HANDLED_NAMES``), is reported as untranslated via an
inline ``-- WARNING: untranslated function: NAME(...)`` comment (see
``_warn_untranslated``), so unrecognised PowerCenter functions are never
silently dropped.

Conversions handled by the scalar passes (see each helper's docstring):
``CHR`` -> ``CHAR``, ``TO_CHAR`` -> ``CAST(... AS VARCHAR(255))``,
``TO_DATE`` -> ``TRY_CONVERT(DATE, ...)``, ``TO_INTEGER`` -> ``TRY_CAST(... AS INT)``,
``ZEROIFNULL`` -> ``COALESCE(..., 0)``, ``DATE_DIFF`` -> ``DATEDIFF(...)``,
``DECODE`` -> ``CASE ... END``, ``SUBSTR`` -> ``SUBSTRING``, and ``||`` -> ``+``.
"""

from __future__ import annotations

import re
from bisect import bisect_right
from collections import defaultdict


def _string_end(s: str, i: int) -> int:
    """Index just past the single-quoted literal starting at ``s[i]``."""
    n = len(s)
    j = i + 1
    while j < n:
        if s[j] == "'":
            if j + 1 < n and s[j + 1] == "'":
                j += 2
                continue
            return j + 1
        j += 1
    return n


# Code resumption markers used to recover where an inline ``--`` comment ends
# when the export has flattened an expression onto a single line.
_RESUME_RE = re.compile(
    r"(?:,\s*(?:'|[A-Za-z_0-9(])"                       # next argument
    r"|\)\s*(?:AND|OR|THEN|ELSE|END|,|\))"              # closing paren + code
    r"|\b(?:AND|OR|THEN|ELSE|END)\b"                    # operator keyword
    r"|\b[A-Za-z_][A-Za-z0-9_]*\s*(?:=|<>|<=|>=|<|>))",  # comparison
    re.IGNORECASE,
)


def _is_code_position(depth: int, base_depth: int) -> bool:
    return depth == base_depth


def _comment_end(s: str, start: int, base_depth: int) -> int:
    """Best-effort end of a flattened inline comment starting after ``--``.

    The export can lose the line breaks that terminated PowerCenter comments,
    leaving a single line such as ``... or --note FLAG = '1' ) AND ...``.  The
    comment ends where the expression resumes: a comma/short-circuit operator,
    a closing parenthesis followed by code, or an ``identifier =`` comparison,
    found at the same parenthesis depth at which the comment began.  If no
    resumption is found, the comment runs to the end of the expression.
    """
    depth = base_depth
    j = start
    n = len(s)
    while j < n:
        c = s[j]
        if c == "'":
            j = _string_end(s, j)
            continue
        if c == "(":
            depth += 1
            j += 1
            continue
        if c == ")":
            if depth > base_depth:
                depth -= 1
                j += 1
                continue
        if _is_code_position(depth, base_depth) and _RESUME_RE.match(s, j):
            return j
        j += 1
    return n


def _strip_inline_comment(s: str) -> str:
    """Remove ``--`` comments from a flattened (single-line) expression."""
    out: list[str] = []
    i = 0
    n = len(s)
    depth = 0
    while i < n:
        c = s[i]
        if c == "'":
            j = _string_end(s, i)
            out.append(s[i:j])
            i = j
            continue
        if s[i:i + 2] == "--":
            if depth == 0:
                break                      # trailing comment -> drop the tail
            end = _comment_end(s, i + 2, depth)
            if 0 < end < n and out:
                prev = out[-1][-1:] if out[-1] else ""
                nxt = s[end]
                if prev and (prev.isalnum() or prev == "'") and (nxt.isalnum() or nxt == "'"):
                    out.append(" ")
            i = end
            continue
        if c == "(":
            depth += 1
        elif c == ")" and depth > 0:
            depth -= 1
        out.append(c)
        i += 1
    return "".join(out)


def _strip_line_comment(line: str) -> str:
    """Remove a trailing ``--`` comment from a single physical line."""
    out: list[str] = []
    i = 0
    n = len(line)
    while i < n:
        if line[i] == "'":
            j = _string_end(line, i)
            out.append(line[i:j])
            i = j
            continue
        if line[i:i + 2] == "--":
            break
        out.append(line[i])
        i += 1
    return "".join(out)


def strip_comments(expr: str) -> str:
    """Drop PowerCenter ``--`` comments and blank lines.

    Multi-line expressions are processed line by line.  A single-line
    expression is treated specially because an export can flatten the original
    lines: the inline comments are then delimited by where the PowerCenter
    expression resumes (see ``_comment_end``).
    """
    if "\n" not in expr and "\r" not in expr:
        s = expr.strip()
        if s.startswith("--"):
            return ""
        return _strip_inline_comment(s).strip()

    kept = []
    for line in expr.splitlines():
        s = line.strip()
        if s.startswith("--"):
            continue
        s = _strip_line_comment(line)
        if s.strip():
            kept.append(s)
    return "\n".join(kept).strip()


def _find_matching(s: str, open_idx: int) -> int:
    depth = 0
    for i in range(open_idx, len(s)):
        if s[i] == "(":
            depth += 1
        elif s[i] == ")":
            depth -= 1
            if depth == 0:
                return i
    return -1


def _split_top_level(s: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    cur: list[str] = []
    for c in s:
        if c == "(":
            depth += 1
        elif c == ")":
            depth -= 1
        if c == sep and depth == 0:
            parts.append("".join(cur).strip())
            cur = []
        else:
            cur.append(c)
    parts.append("".join(cur).strip())
    return parts


def translate(expr: str) -> str:
    s = strip_comments(expr)
    if not s:
        return s
    s = _translate_aggregates(s)
    s = _translate_iif(s)
    s = _translate_scalars(s)
    s = _warn_untranslated(s)
    return s


# --- conditional aggregates --------------------------------------------------

_AGGREGATES = ("COUNT", "SUM", "AVG", "MIN", "MAX")


def _translate_aggregates(s: str) -> str:
    for fn in _AGGREGATES:
        s = _replace_agg(s, fn)
    return s


def _replace_agg(s: str, fn: str) -> str:
    """PowerCenter ``FN(value, cond)`` -> ``FN(CASE WHEN cond THEN value END)``.

    Plain ``FN(value)`` is left as-is.
    """
    out: list[str] = []
    i = 0
    L = len(fn)
    while i < len(s):
        if s[i:i + L].upper() == fn:
            j = i + L
            while j < len(s) and s[j] in " \t":
                j += 1
            if j < len(s) and s[j] == "(":
                end = _find_matching(s, j)
                if end != -1:
                    body = s[j + 1:end]
                    parts = _split_top_level(body)
                    if len(parts) == 2:
                        value, cond = parts
                        out.append(f"{fn}(CASE WHEN {cond} THEN {value} END)")
                    elif len(parts) == 1:
                        out.append(f"{fn}({parts[0]})")
                    else:
                        out.append(s[i:end + 1])
                    i = end + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


# --- IIF ----------------------------------------------------------------------

def _translate_iif(s: str) -> str:
    """Nested ``IIF(cond, then [, else])`` -> ``CASE WHEN ... THEN ... END``.

    Recursive, so nested IIFs (which PowerCenter writes in the ELSE branch)
    are handled naturally.  PowerCenter also accepts the two-argument form
    ``IIF(cond, then)`` (used for conditional aggregates such as
    ``IIF(flag = '0', SUM(x))``); it returns NULL when the condition is false,
    which is exactly what a CASE without ELSE does in T-SQL.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        if s[i:i + 3].upper() == "IIF":
            j = i + 3
            while j < len(s) and s[j] in " \t":
                j += 1
            if j < len(s) and s[j] == "(":
                end = _find_matching(s, j)
                if end != -1:
                    parts = _split_top_level(s[j + 1:end])
                    if len(parts) == 3:
                        cond, then_, else_ = parts
                        out.append(
                            f"CASE WHEN {_translate_iif(cond)} "
                            f"THEN {_translate_iif(then_)} "
                            f"ELSE {_translate_iif(else_)} END"
                        )
                        i = end + 1
                        continue
                    if len(parts) == 2:
                        cond, then_ = parts
                        out.append(
                            f"CASE WHEN {_translate_iif(cond)} "
                            f"THEN {_translate_iif(then_)} END"
                        )
                        i = end + 1
                        continue
        out.append(s[i])
        i += 1
    return "".join(out)


# --- scalar functions ---------------------------------------------------------

def _translate_scalars(s: str) -> str:
    s = _replace_in_forms(s)
    s = _replace_isnull(s)
    s = _replace_replacestr(s)
    s = _replace_conversions(s)   # CHR / TO_CHAR / TO_DATE
    s = _replace_to_integer(s)    # TO_INTEGER -> TRY_CAST(... AS INT)
    s = _replace_zeroifnull(s)    # ZEROIFNULL -> COALESCE(..., 0)
    s = _replace_date_diff(s)     # DATE_DIFF -> DATEDIFF
    s = _replace_decode(s)        # DECODE -> CASE ... END
    s = _replace_substr(s)        # SUBSTR -> SUBSTRING
    s = _replace_concat(s)        # || -> + (PowerCenter string concatenation)
    s = _upper_logical(s)
    return s


def _replace_in_forms(s: str) -> str:
    """``IN(x, a, b)`` -> ``x IN (a, b)`` and ``NOT IN(x, a, b)`` -> ``x NOT IN (a, b)``.

    Single left-to-right pass over the ORIGINAL string, emitting the converted
    text into a separate buffer and advancing past the original token.  This
    guarantees the two forms never re-translate each other (a ``NOT IN(...)``
    that is first rewritten to ``x NOT IN (...)`` must not be picked up again
    by the plain ``IN(...)`` rule).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        m = re.match(r"NOT\s+IN\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1                 # index of '('
            end = _find_matching(s, j)
            if end != -1:
                parts = _split_top_level(s[j + 1:end])
                if parts:
                    out.append(f"{parts[0]} NOT IN ({', '.join(parts[1:])})")
                    i = end + 1
                    continue
        m = re.match(r"IN\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                parts = _split_top_level(s[j + 1:end])
                if len(parts) > 1:
                    out.append(f"{parts[0]} IN ({', '.join(parts[1:])})")
                    i = end + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_isnull(s: str) -> str:
    """``not isnull(x)`` -> ``x IS NOT NULL``;  ``isnull(x)`` -> ``(x IS NULL)``."""
    # handle the negated form first
    out: list[str] = []
    i = 0
    while i < len(s):
        m = re.match(r"not\s+isnull\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                out.append(f"({s[j + 1:end].strip()} IS NOT NULL)")
                i = end + 1
                continue
        m = re.match(r"isnull\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                out.append(f"({s[j + 1:end].strip()} IS NULL)")
                i = end + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_replacestr(s: str) -> str:
    """``REPLACESTR(start, source, search, replace)`` -> ``REPLACE(source, search, replace)``.

    Note: PowerCenter's positional start argument has no direct T-SQL
    equivalent for a plain REPLACE, so it is dropped (documented limitation).
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        m = re.match(r"REPLACESTR\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                parts = _split_top_level(s[j + 1:end])
                if len(parts) >= 4:
                    # parts[0] = start, parts[1] = source, parts[2] = search, parts[3] = replace
                    out.append(f"REPLACE({parts[1]}, {parts[2]}, {parts[3]})")
                    i = end + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _upper_logical(s: str) -> str:
    """Uppercase PowerCenter logical operators so the result is valid T-SQL."""
    return re.sub(r"\b(and|or|not)\b", lambda m: m.group(1).upper(), s)


# --- conversion / character functions ----------------------------------------

# Mapping of the Oracle-style format strings that appear in this repository's
# TO_DATE calls to the SQL Server CONVERT/TRY_CONVERT style codes.  Only the
# unambiguous date formats are mapped; anything else falls back to a bare
# TRY_CONVERT(DATE, x) with the format dropped (documented below).
_ORACLE_FMT_TO_TSTYLE = {
    "YYYY-MM-DD": "23",   # ISO8601 yyyy-mm-dd
    "YYYY/MM/DD": "111",  # yyyy/mm/dd
    "YYYYMMDD": "112",    # yyyymmdd
    "DD/MM/YYYY": "103",  # dd/mm/yyyy
    "MM/DD/YYYY": "101",  # mm/dd/yyyy
    "DD.MM.YYYY": "104",  # dd.mm.yyyy
}


def _replace_conversions(s: str) -> str:
    """Translate the PowerCenter conversion functions.

    Order matters: ``CHR`` is innermost (it can appear inside a ``TO_DATE``
    argument), then ``TO_CHAR``, then ``TO_DATE``.
    """
    s = _replace_chr(s)
    s = _replace_to_char(s)
    s = _replace_to_date(s)
    return s


def _replace_chr(s: str) -> str:
    """``CHR(n)`` -> ``CHAR(n)``.

    PowerCenter CHR returns the character whose code point is ``n``; T-SQL
    CHAR does the same for the 0-255 range (CHR(39) -> CHAR(39) == a single
    quote).  The conversion is a straight rename.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        m = re.match(r"CHR\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                out.append(f"CHAR({s[j + 1:end].strip()})")
                i = end + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_to_char(s: str) -> str:
    """``TO_CHAR(x [, fmt])`` -> ``CAST(x AS VARCHAR(255))``.

    PowerCenter TO_CHAR converts a value to a string; the Oracle-style format
    string (2nd argument) has no direct SQL Server equivalent, so it is dropped.
    The length 255 is a documented default: the original port PRECISION is not
    consulted here because the translator operates on expressions alone.
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        m = re.match(r"TO_CHAR\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                parts = _split_top_level(s[j + 1:end])
                arg = parts[0].strip() if parts else ""
                out.append(f"CAST({arg} AS VARCHAR(255))")
                i = end + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_to_date(s: str) -> str:
    """``TO_DATE(x [, fmt])`` -> ``TRY_CONVERT(DATE, x [, style])``.

    PowerCenter TO_DATE parses a string into a date.  TRY_CONVERT returns NULL
    instead of raising on an invalid value, which matches PowerCenter's
    "conversion error -> NULL / ERROR(...)" behaviour more closely than a hard
    CONVERT.  A known Oracle format string is mapped to its T-SQL style code;
    an unrecognised or missing format is dropped (documented limitation).
    """
    out: list[str] = []
    i = 0
    while i < len(s):
        m = re.match(r"TO_DATE\s*\(", s[i:], re.IGNORECASE)
        if m:
            j = i + m.end() - 1
            end = _find_matching(s, j)
            if end != -1:
                parts = _split_top_level(s[j + 1:end])
                arg = parts[0].strip() if parts else ""
                style = ""
                if len(parts) >= 2:
                    fmt = parts[1].strip().strip("'\"").upper()
                    style = _ORACLE_FMT_TO_TSTYLE.get(fmt, "")
                if style:
                    out.append(f"TRY_CONVERT(DATE, {arg}, {style})")
                else:
                    out.append(f"TRY_CONVERT(DATE, {arg})")
                i = end + 1
                continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _is_null_literal(x: str) -> bool:
    """True when ``x`` is the bare NULL literal (case-insensitive)."""
    return x.strip().upper() == "NULL"


def _replace_to_integer(s: str) -> str:
    """``TO_INTEGER(x)`` -> ``TRY_CAST(x AS INT)``.

    PowerCenter converts a string/number to an integer, truncating the
    fractional part.  T-SQL ``CAST`` also truncates a numeric value toward
    zero, and ``TRY_CAST`` additionally returns NULL instead of raising on an
    unconvertible string (closer to PowerCenter's runtime conversion-error
    handling than a hard CAST -- documented choice).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i = _skip_string_literal(s, i, out)
            continue
        if s[i] in "tT" and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"TO_INTEGER\s*\(", s[i:], re.IGNORECASE)
            if m:
                j = i + m.end() - 1
                end = _find_matching(s, j)
                if end != -1:
                    arg = _replace_to_integer(s[j + 1:end].strip())
                    out.append(f"TRY_CAST({arg} AS INT)")
                    i = end + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_zeroifnull(s: str) -> str:
    """``ZEROIFNULL(x)`` -> ``COALESCE(x, 0)``.

    PowerCenter ZEROIFNULL returns 0 when its argument is NULL.  COALESCE is
    used instead of T-SQL ISNULL because the latter is also a PowerCenter
    function with different semantics (a one-argument NULL test).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i = _skip_string_literal(s, i, out)
            continue
        if s[i] in "zZ" and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"ZEROIFNULL\s*\(", s[i:], re.IGNORECASE)
            if m:
                j = i + m.end() - 1
                end = _find_matching(s, j)
                if end != -1:
                    arg = _replace_zeroifnull(s[j + 1:end].strip())
                    out.append(f"COALESCE({arg}, 0)")
                    i = end + 1
                    continue
        out.append(s[i])
        i += 1
    return "".join(out)


# PowerCenter DATE_DIFF format -> T-SQL DATEDIFF datepart.  Only the
# documented unambiguous formats are mapped; anything else is left
# untranslated (and reported by _warn_untranslated) rather than guessed.
_DATE_DIFF_UNITS = {
    "Y": "YEAR", "YY": "YEAR", "YYY": "YEAR", "YYYY": "YEAR", "YEAR": "YEAR",
    "Q": "QUARTER", "QQ": "QUARTER", "QUARTER": "QUARTER",
    "M": "MONTH", "MM": "MONTH", "MON": "MONTH", "MONTH": "MONTH",
    "W": "WEEK", "WW": "WEEK", "WEEK": "WEEK",
    "D": "DAY", "DD": "DAY", "DAY": "DAY",
    "HH": "HOUR", "HH12": "HOUR", "HH24": "HOUR", "HOUR": "HOUR",
    "MI": "MINUTE", "MIN": "MINUTE", "MINUTE": "MINUTE",
    "SS": "SECOND", "SEC": "SECOND", "SECOND": "SECOND",
    "MS": "MILLISECOND", "MILLISECOND": "MILLISECOND",
}


def _date_diff_unit(fmt: str) -> str:
    """Map a PowerCenter DATE_DIFF format argument to a T-SQL datepart."""
    return _DATE_DIFF_UNITS.get(fmt.strip().strip("'\"").upper(), "")


def _replace_date_diff(s: str) -> str:
    """``DATE_DIFF(date1, date2, fmt)`` -> ``DATEDIFF(unit, date2, date1)``.

    PowerCenter returns ``date1 - date2``; T-SQL DATEDIFF takes the start date
    first, so the two date arguments are swapped.  DATEDIFF counts datepart
    boundaries (PowerCenter truncates the interval), a documented semantic
    difference.  The format argument must be a known literal; otherwise the
    call is left intact for the untranslated-function warning.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i = _skip_string_literal(s, i, out)
            continue
        if s[i] in "dD" and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"DATE_DIFF\s*\(", s[i:], re.IGNORECASE)
            if m:
                j = i + m.end() - 1
                end = _find_matching(s, j)
                if end != -1:
                    parts = [_replace_date_diff(p) for p in _split_top_level(s[j + 1:end])]
                    if len(parts) == 3:
                        unit = _date_diff_unit(parts[2])
                        if unit:
                            out.append(f"DATEDIFF({unit}, {parts[1]}, {parts[0]})")
                            i = end + 1
                            continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _skip_string_literal(s: str, i: int, out: list[str]) -> int:
    """Append the single-quoted literal at ``s[i]`` to ``out``; return index after it.

    PowerCenter escapes an embedded quote by doubling it (``''``).
    """
    n = len(s)
    out.append(s[i])
    i += 1
    while i < n:
        if s[i] == "'":
            if i + 1 < n and s[i + 1] == "'":
                out.append("''")
                i += 2
                continue
            out.append(s[i])
            i += 1
            break
        out.append(s[i])
        i += 1
    return i


def _replace_decode(s: str) -> str:
    """``DECODE(value, s1, r1, ..., [default])`` -> ``CASE ... END``.

    DECODE returns the ``ri`` of the first ``si`` equal to ``value``; the
    trailing ``default`` (present when the argument count after ``value`` is
    odd) is the fallback, emitted as ``ELSE NULL`` when absent.

    NULL semantics (documented choice): PowerCenter DECODE treats NULL as
    equal to NULL, i.e. its test is ``value = si OR (value IS NULL AND
    si IS NULL)``.  T-SQL's simple ``CASE value WHEN si`` is ``value = si``,
    under which ``NULL = NULL`` is UNKNOWN and never matches.  So:

    * when no search term is the NULL literal (the common case, and the only
      case where the two semantics coincide) we emit the simple
      ``CASE value WHEN si THEN ri ... END``; and
    * when a search term *is* NULL we emit the NULL-safe searched form
      ``CASE WHEN value = si OR (value IS NULL AND si IS NULL) THEN ri ...``.

    A search term that can be NULL only through a nullable column (rather than
    the literal) is not detected statically and uses the simple form
    (documented limitation).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i = _skip_string_literal(s, i, out)
            continue
        if s[i] in "dD" and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"DECODE\s*\(", s[i:], re.IGNORECASE)
            if m:
                j = i + m.end() - 1
                end = _find_matching(s, j)
                if end != -1:
                    parts = _split_top_level(s[j + 1:end])
                    if len(parts) >= 3:
                        value = _replace_decode(parts[0])
                        rest = [_replace_decode(p) for p in parts[1:]]
                        default = None
                        if len(rest) % 2 == 1:
                            default = rest[-1]
                            rest = rest[:-1]
                        pairs = list(zip(rest[0::2], rest[1::2]))
                        null_safe = any(_is_null_literal(srch) for srch, _ in pairs)
                        tail = f" ELSE {default}" if default is not None else " ELSE NULL"
                        if null_safe:
                            whens = " ".join(
                                f"WHEN {value} = {srch} OR "
                                f"({value} IS NULL AND {srch} IS NULL) THEN {res}"
                                for srch, res in pairs
                            )
                            out.append("CASE " + whens + tail + " END")
                        else:
                            whens = " ".join(f"WHEN {srch} THEN {res}" for srch, res in pairs)
                            out.append(f"CASE {value} " + whens + tail + " END")
                        i = end + 1
                        continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_substr(s: str) -> str:
    """``SUBSTR(str, start [, length])`` -> ``SUBSTRING(...)``.

    PowerCenter SUBSTR is 1-based exactly like T-SQL SUBSTRING, so ``start``
    maps straight across (no offset -- documented).  The three-argument form
    is a straight rename.  The two-argument form ``SUBSTR(str, start)`` (the
    tail from ``start``) has no T-SQL equivalent and is expanded to
    ``SUBSTRING(str, start, LEN(str) - start + 1)`` (``LEN`` excludes trailing
    spaces, a documented caveat).
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i = _skip_string_literal(s, i, out)
            continue
        if s[i] in "sS" and (i == 0 or not (s[i - 1].isalnum() or s[i - 1] == "_")):
            m = re.match(r"SUBSTR\s*\(", s[i:], re.IGNORECASE)
            if m:
                j = i + m.end() - 1
                end = _find_matching(s, j)
                if end != -1:
                    parts = [_replace_substr(p) for p in _split_top_level(s[j + 1:end])]
                    if len(parts) == 3:
                        out.append(f"SUBSTRING({parts[0]}, {parts[1]}, {parts[2]})")
                        i = end + 1
                        continue
                    if len(parts) == 2:
                        start = parts[1]
                        start_term = f"({start})" if any(c in start for c in "+-*/") else start
                        out.append(
                            f"SUBSTRING({parts[0]}, {start}, "
                            f"LEN({parts[0]}) - {start_term} + 1)"
                        )
                        i = end + 1
                        continue
        out.append(s[i])
        i += 1
    return "".join(out)


def _replace_concat(s: str) -> str:
    """PowerCenter ``||`` (string concatenation) -> T-SQL ``+``.

    Single-quoted string literals are skipped so a literal ``||`` is never
    rewritten.  PowerCenter inherits ``||`` from Oracle syntax; its logical OR
    is the keyword ``or`` (already uppercased by ``_upper_logical``), so ``||``
    unambiguously means concatenation.
    """
    out: list[str] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            out.append(s[i])
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        out.append("''")
                        i += 2
                        continue
                    out.append(s[i])
                    i += 1
                    break
                out.append(s[i])
                i += 1
            continue
        if s[i:i + 2] == "||":
            out.append("+")
            i += 2
            continue
        out.append(s[i])
        i += 1
    return "".join(out)


# --- untranslated-function signalling ---------------------------------------

# Names the existing passes already handle, PLUS standard T-SQL functions that
# are valid in SQL Server.  In the FINAL translated text a handled name either
# no longer appears as ``NAME(`` (IIF -> CASE/WHEN, ISNULL -> IS NULL,
# REPLACESTR -> REPLACE, IN(...) -> x IN (...)), or legitimately remains as a
# T-SQL call (the plain single-argument aggregate form, REPLACE emitted by the
# REPLACESTR pass, and any genuine T-SQL function).  A ``NAME(`` that survives
# and is NOT listed here was left untouched by every pass, so it is reported
# as untranslated.
#
# This is a whitelist of *handled* constructs, not an enumeration of the
# unknown ones: any remaining PowerCenter function (TO_DECIMAL, ADD_MONTHS,
# TRUNC, ...) is caught generically by simply not being in this
# set.  DECODE and SUBSTR are now translated away by their own passes (into
# ``CASE ... END`` and ``SUBSTRING``), so -- like IIF / ISNULL / REPLACESTR --
# they do not survive as ``NAME(`` in the output and are NOT whitelisted
# themselves; the names they *emit* (CASE / WHEN / THEN / ELSE / END,
# SUBSTRING, LEN) are whitelisted below.
#
# Two deliberate decisions, documented so the intent is explicit:
#
# * LTRIM / RTRIM are whitelisted even though they are also PowerCenter
#   functions.  The semantics are identical in both (strip leading/trailing
#   spaces; single argument), so the translated text is correct either way and
#   flagging them would only produce false positives.
#
# * ISNULL is deliberately NOT whitelisted as a T-SQL function.  Its semantics
#   differ: PowerCenter ``isnull(x)`` is a NULL test returning TRUE/FALSE, while
#   T-SQL ``ISNULL(a, b)`` is a two-argument COALESCE.  The ``_replace_isnull``
#   pass already handles the PowerCenter form (-> ``IS NULL``); anything else
#   keeps being flagged so a genuine two-argument ISNULL is not silently
#   accepted.
_HANDLED_NAMES = frozenset({
    # conditional-aggregate pass (`_translate_aggregates`) -- plain `FN(x)` form
    "COUNT", "SUM", "AVG", "MIN", "MAX",
    # REPLACESTR pass emits `REPLACE(...)`
    "REPLACE",
    # IN / NOT IN pass and logical operators (may precede '(' in the output)
    "IN", "NOT", "AND", "OR",
    # IIF pass emits `CASE ... WHEN ... THEN ... ELSE ... END`
    "CASE", "WHEN", "THEN", "ELSE", "END",
    # ISNULL pass emits `IS NULL` / `IS NOT NULL`
    "IS", "NULL",

    # --- standard T-SQL functions (valid in SQL Server) -----------------------
    # String functions:
    "UPPER", "LOWER", "LTRIM", "RTRIM", "TRIM", "SUBSTRING", "LEFT", "RIGHT",
    "CHARINDEX", "PATINDEX", "STUFF", "REPLICATE", "LEN", "CONCAT",
    "CONCAT_WS", "FORMAT", "STR",
    # Date/time functions:
    "GETDATE", "GETUTCDATE", "SYSDATETIME", "SYSUTCDATETIME", "DATEADD",
    "DATEDIFF", "DATEPART", "DATENAME", "YEAR", "MONTH", "DAY", "CONVERT",
    "CAST", "ISDATE", "EOMONTH",
    # Mathematical functions:
    "ROUND", "FLOOR", "CEILING", "POWER", "SQRT", "ABS", "SIGN", "EXP",
    "LOG", "LOG10", "PI", "RAND",
    # Conversion / coalesce functions (ISNULL deliberately excluded, see above):
    "COALESCE", "NULLIF", "TRY_CONVERT", "TRY_CAST",
    # Ranking (window) functions:
    "ROW_NUMBER", "RANK", "DENSE_RANK",

    # T-SQL data-type names: these legitimately appear as ``TYPE(...)`` inside
    # CAST/CONVERT (e.g. ``CONVERT(VARCHAR(10), ...)``) and are not functions,
    # but without them here a genuine CAST/CONVERT would be a false positive.
    # None collide with a PowerCenter function (those are spelled TO_DATE /
    # TO_INTEGER / TO_DECIMAL / CHR / ... instead).
    "VARCHAR", "NVARCHAR", "CHAR", "NCHAR", "TEXT", "NTEXT",
    "DECIMAL", "NUMERIC", "INT", "BIGINT", "SMALLINT", "TINYINT", "BIT",
    "MONEY", "SMALLMONEY", "FLOAT", "REAL",
    "DATE", "TIME", "DATETIME", "DATETIME2", "SMALLDATETIME", "DATETIMEOFFSET",
    "BINARY", "VARBINARY", "IMAGE", "UNIQUEIDENTIFIER", "SQL_VARIANT",
})

_FUNC_CALL_RE = re.compile(r"([A-Za-z_][A-Za-z0-9_]*)\s*\(")


def _scan_untranslated(s: str) -> list[tuple[int, str]]:
    """Return ``(start_index, name)`` for every unhandled ``NAME(`` in ``s``.

    Single-quoted string literals are skipped (PowerCenter escapes an embedded
    quote by doubling it: ``''``), so function names inside a literal are never
    flagged.  The scan is a single left-to-right pass over the *translated*
    text, so anything produced by an earlier pass (``CASE``, ``REPLACE``, the
    ``IN`` of ``x IN (...)``, ...) is either absent or in ``_HANDLED_NAMES``.
    """
    hits: list[tuple[int, str]] = []
    i = 0
    n = len(s)
    while i < n:
        if s[i] == "'":
            i += 1
            while i < n:
                if s[i] == "'":
                    if i + 1 < n and s[i + 1] == "'":
                        i += 2
                        continue
                    i += 1
                    break
                i += 1
            continue
        m = _FUNC_CALL_RE.match(s, i)
        if m:
            name = m.group(1).upper()
            if name not in _HANDLED_NAMES:
                hits.append((i, name))
            i = m.end()
            continue
        i += 1
    return hits


def _warn_untranslated(s: str) -> str:
    """Insert ``-- WARNING: untranslated function: NAME(...)`` lines.

    Each warning goes on its own comment line immediately *before* the line
    that contains the unrecognised call, so the SQL stays syntactically valid
    (the call itself is preserved verbatim as best effort).  Returns ``s``
    unchanged when nothing is untranslated.
    """
    hits = _scan_untranslated(s)
    if not hits:
        return s

    lines = s.split("\n")
    starts: list[int] = []
    offset = 0
    for ln in lines:
        starts.append(offset)
        offset += len(ln) + 1

    by_line: dict[int, list[str]] = defaultdict(list)
    for idx, name in hits:
        li = bisect_right(starts, idx) - 1
        by_line[li].append(name)

    out: list[str] = []
    for li, ln in enumerate(lines):
        for name in by_line.get(li, []):
            out.append(f"-- WARNING: untranslated function: {name}(...)")
        out.append(ln)
    return "\n".join(out)
