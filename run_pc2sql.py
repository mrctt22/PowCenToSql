#!/usr/bin/env python3
"""CLI: generate SQL Server instructions from a PowerCenter XML export.

Usage:
    python run_pc2sql.py <mapping.xml> [-o output.sql]
"""

import sys

from pc2sql import convert


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print("usage: python run_pc2sql.py <mapping.xml> [-o output.sql]", file=sys.stderr)
        return 2

    xml_path = argv[1]
    out_path = None
    if "-o" in argv:
        out_path = argv[argv.index("-o") + 1]

    sql = convert(xml_path)

    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(sql)
        print(f"wrote {out_path}", file=sys.stderr)
    else:
        print(sql)
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
