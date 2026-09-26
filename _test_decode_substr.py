import sys
sys.path.insert(0, r"C:\Users\mrctt\Documents\pc2sql")
from pc2sql import expressions as ex


def has_warning(sql: str) -> bool:
    return "WARNING: untranslated function" in sql


# (input, expected output) -- exact-string assertions for the new translations.
cases = [
    # DECODE -> simple CASE (with default)
    ("DECODE(x, 'A', 1, 'B', 2, 0)",
     "CASE x WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE 0 END"),
    # DECODE -> simple CASE (no default -> ELSE NULL)
    ("DECODE(x, 'A', 1, 'B', 2)",
     "CASE x WHEN 'A' THEN 1 WHEN 'B' THEN 2 ELSE NULL END"),
    # single pair + default
    ("DECODE(PRO_TIPO_PROVVEDIMENTO, 'C', 1, 0)",
     "CASE PRO_TIPO_PROVVEDIMENTO WHEN 'C' THEN 1 ELSE 0 END"),
    ("DECODE(COL_C, 'A', '1', '0')",
     "CASE COL_C WHEN 'A' THEN '1' ELSE '0' END"),
    # DECODE with an explicit NULL search -> NULL-safe searched CASE
    ("DECODE(x, NULL, 'isnull', 'notnull')",
     "CASE WHEN x = NULL OR (x IS NULL AND NULL IS NULL) THEN 'isnull' ELSE 'notnull' END"),
    # SUBSTR 3-arg -> SUBSTRING (1-based, straight rename)
    ("SUBSTR(s, 1, 3)", "SUBSTRING(s, 1, 3)"),
    # SUBSTR 2-arg -> SUBSTRING with LEN tail expression
    ("SUBSTR(s, 4)", "SUBSTRING(s, 4, LEN(s) - 4 + 1)"),
    # nested / in-context cases
    ("LTRIM(SUBSTR(COL_B, 1, 3))", "LTRIM(SUBSTRING(COL_B, 1, 3))"),
    ("PRO_IMPORTO + SUBSTR(PWC_TIPO_RISORSA, 1, 3)",
     "PRO_IMPORTO + SUBSTRING(PWC_TIPO_RISORSA, 1, 3)"),
    ("IIF(isnull(RR_ID_RAPPORTO_ABBIN),0,DECODE(PRO_TIPO_PROVVEDIMENTO, 'C', 1, 0))",
     "CASE WHEN (RR_ID_RAPPORTO_ABBIN IS NULL) THEN 0 "
     "ELSE CASE PRO_TIPO_PROVVEDIMENTO WHEN 'C' THEN 1 ELSE 0 END END"),
    # same-type nesting is handled recursively
    ("DECODE(DECODE(a,'x',1,0), 1, 'y', 'n')",
     "CASE CASE a WHEN 'x' THEN 1 ELSE 0 END WHEN 1 THEN 'y' ELSE 'n' END"),
    ("SUBSTR(SUBSTR(a,1,3), 2, 1)", "SUBSTRING(SUBSTRING(a, 1, 3), 2, 1)"),
]

failures = []
for expr, expected in cases:
    got = ex.translate(expr)
    ok = (got == expected) and not has_warning(got)
    print(f"[{'PASS' if ok else 'FAIL'}] {expr!r}")
    if not ok:
        print(f"         expected: {expected!r}")
        print(f"         got:      {got!r}")
        failures.append(expr)
    else:
        print(f"         => {got}")

print()
if failures:
    print(f"FAILED: {len(failures)} case(s):")
    for f in failures:
        print("  -", f)
    sys.exit(1)
print("ALL DECODE/SUBSTR TRANSLATION CHECKS PASSED")
