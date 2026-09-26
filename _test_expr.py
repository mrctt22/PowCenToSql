import sys
sys.path.insert(0, r"C:\Users\mrctt\Documents\pc2sql")
from pc2sql import expressions as ex

tests = [
    "NOT IN(NOTST_ID_STATO,10,62)",
    "RIS_ABBINATE_ABB_NONABB=1 AND NOT IN(NOTST_ID_STATO,10,62) AND RIS_FLAG_ANN='0'",
    "(NOT IN(NOTST_ID_STATO,10,61,62))",
    "IIF(isnull(RR_ID_RAPPORTO_ABBIN),0,1)",
    "IIF(NOTST_ID_STATO=10 and not isnull(RIS_DATA_ABBIN1),1,0)",
    "REPLACESTR ( 1, PWC_TIPO_RISORSA, '.', '' )",
    "COUNT(PRO_IMPORTO, PRO_TIPO_PROVVEDIMENTO= 'C' or PRO_TIPO_PROVVEDIMENTO='C3')",
    # newly translated conversions
    "TO_CHAR(TIPO_RAPPORTO)",
    "TO_CHAR(TIPO_RAPPORTO)||'L'",
    "TO_DATE(DATA_DW, 'YYYY-MM-DD')",
    "TO_DATE(REPLACESTR(0, $$DATADW, CHR(39), null), 'YYYY-MM-DD')",
    "CHR(39)",
    "ROUND((IMP_AE * TASSO),2)",
    "IIF(abs(IMP_S_L)> IMP_AE, -IMP_AE, -IMP_S_L)",
]
for t in tests:
    print(repr(t))
    print("  =>", ex.translate(t))
    print()
