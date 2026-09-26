import sys
sys.path.insert(0, r"C:\Users\mrctt\Documents\pc2sql")
from pc2sql import expressions as ex


def has_warning(sql: str) -> bool:
    return "WARNING: untranslated function" in sql


failures: list[str] = []


def check(label: str, expr: str, expect_warning: bool) -> None:
    out = ex.translate(expr)
    warn = has_warning(out)
    ok = (warn == expect_warning)
    print(f"[{'PASS' if ok else 'FAIL'}] {label}: {expr!r}")
    if not ok:
        print(f"         expected warning={expect_warning}, got warning={warn}")
        print(f"         => {out}")
        failures.append(f"{label}: {expr!r}")


# 1) no regression on already-translated constructs (must NOT warn)
print("=== 1) regression: already-translated constructs (must not warn) ===")
regression = [
    "NOT IN(NOTST_ID_STATO,10,62)",
    "RIS_ABBINATE_ABB_NONABB=1 AND NOT IN(NOTST_ID_STATO,10,62) AND RIS_FLAG_ANN='0'",
    "(NOT IN(NOTST_ID_STATO,10,61,62))",
    "IIF(isnull(RR_ID_RAPPORTO_ABBIN),0,1)",
    "IIF(NOTST_ID_STATO=10 and not isnull(RIS_DATA_ABBIN1),1,0)",
    "REPLACESTR ( 1, PWC_TIPO_RISORSA, '.', '' )",
    "COUNT(PRO_IMPORTO, PRO_TIPO_PROVVEDIMENTO= 'C' or PRO_TIPO_PROVVEDIMENTO='C3')",
]
for t in regression:
    check("regression", t, expect_warning=False)

# 2) whitelisted standard T-SQL functions (must NOT warn)
print()
print("=== 2) whitelisted T-SQL functions (must not warn) ===")
whitelisted = [
    "UPPER(PWC_TIPO_RISORSA)",
    "LOWER(PWC_TIPO_RISORSA)",
    "SUBSTRING(PWC_TIPO_RISORSA, 1, 3)",
    "CONVERT(VARCHAR(10), RIS_DATA_ABBIN1, 112)",
    "COALESCE(RIS_DATA_ABBIN1, RIS_DATA_ABBIN2)",
    "ROUND(PRO_IMPORTO, 2)",
    "DATEADD(DAY, 1, RIS_DATA_ABBIN1)",
    "GETDATE()",
    # LTRIM / RTRIM are valid both in PowerCenter and T-SQL with identical
    # semantics -> whitelisted (documented in expressions._HANDLED_NAMES).
    "LTRIM(PWC_TIPO_RISORSA)",
    "RTRIM(PWC_TIPO_RISORSA)",
    "LTRIM(RTRIM(PWC_TIPO_RISORSA))",
]

# 3) newly translated PowerCenter functions (must NOT warn anymore)
print()
print("=== 3) newly translated conversions (must not warn) ===")
converted = [
    "TO_CHAR(TIPO_RAPPORTO)",
    "TO_CHAR(TIPO_RAPPORTO)||'L'",
    "TO_DATE(DATA_DW)",
    "TO_DATE(DATA_DW, 'YYYY-MM-DD')",
    "CHR(39)",
    "TO_DATE(REPLACESTR(0, $$DATADW, CHR(39), null), 'YYYY-MM-DD')",
]
for t in converted:
    check("converted", t, expect_warning=False)

# 3b) newly translated DECODE / SUBSTR (must NOT warn anymore)
print()
print("=== 3b) newly translated DECODE / SUBSTR (must not warn) ===")
decode_substr = [
    "DECODE(PRO_TIPO_PROVVEDIMENTO, 'C', 1, 0)",
    "DECODE(x, 'A', 1, 'B', 2, 0)",
    "DECODE(x, 'A', 1, 'B', 2)",
    "DECODE(x, NULL, 'isnull', 'notnull')",
    "SUBSTR(PWC_TIPO_RISORSA, 1, 3)",
    "SUBSTR(PWC_TIPO_RISORSA, 4)",
    "PRO_IMPORTO + SUBSTR(PWC_TIPO_RISORSA, 1, 3)",
    "IIF(isnull(RR_ID_RAPPORTO_ABBIN),0,DECODE(PRO_TIPO_PROVVEDIMENTO, 'C', 1, 0))",
    "LTRIM(SUBSTR(COL_B, 1, 3))",
]
for t in decode_substr:
    check("decode/substr", t, expect_warning=False)

# 4) PowerCenter-only functions (must STILL warn)
print()
print("=== 4) PowerCenter-only functions (must still warn) ===")
pc_only = [
    "ADD_MONTHS(RIS_DATA_ABBIN1, 60)",
    "TRUNC(PRO_IMPORTO, 2)",
    "TO_INTEGER(PWC_TIPO_RISORSA)",
    "IIF(isnull(RR_ID_RAPPORTO_ABBIN), 0, ADD_MONTHS(RIS_DATA_ABBIN1, 60))",
]
for t in pc_only:
    check("pc-only", t, expect_warning=True)

# 5) function names inside string literals (must NOT warn)
print()
print("=== 5) string literals (must not warn) ===")
for t in ["'TO_DATE'", "'DECODE'", "'SUBSTR'", "'UPPER'", "'TO_CHAR'", "'CHR'"]:
    check("literal", t, expect_warning=False)

print()
if failures:
    print(f"FAILED: {len(failures)} check(s):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL CHECKS PASSED")
