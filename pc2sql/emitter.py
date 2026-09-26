"""Emitter: IR (ir.Mapping) -> SQL Server (T-SQL) text.

Design rule enforced here (mirrors the PowerCenter object model):

* one PowerCenter object  ->  one SQL object (a CTE / statement block),
* every object keeps its PowerCenter name as a ``-- PowerCenter ...`` comment,
* transformations are NOT fused into a single SELECT: each expression /
  aggregator / source qualifier / filter / joiner / union becomes its own
  CTE, chained in data-flow order and referenced by PowerCenter object name.

Group-based transformations are expanded into one SQL object per output group:

* Filter           -> CTE with a WHERE (the ``Filter Condition``).
* Router           -> one CTE per ``<GROUP TYPE="OUTPUT">`` (and the DEFAULT
  group as the ``NOT(...)`` of the others), each named ``<router>__<group>``.
* Joiner           -> CTE with a JOIN (master = ``.../MASTER`` ports, detail =
  the other input group, direction from ``Join Type``).
* Union            -> CTE with ``UNION ALL`` of every input group, mapping
  input fields to output fields via ``FIELDDEPENDENCY``.
* Update Strategy  -> an INSERT / UPDATE / DELETE / MERGE statement (a
  statement block, not a CTE) whose form follows ``Update Strategy Expression``.
"""

from __future__ import annotations

import re

from .expressions import translate
from .ir import Edge, Mapping, Node, Port


def _ident(name: str) -> str:
    """T-SQL bracket-quoted identifier."""
    return "[" + name.replace("]", "]]") + "]"


def _banner(node: Node) -> str:
    return (
        f"-- ==========================================================================\n"
        f"-- PowerCenter {node.type}: {node.name}"
        + (f"   (definition: {node.definition})" if node.definition != node.name else "")
        + f"\n"
        f"-- =========================================================================="
    )


def _upstreams(m: Mapping, node: Node) -> list[str]:
    """Names of the instances that feed this node, deduplicated."""
    seen: list[str] = []
    for e in m.edges:
        if e.to_obj == node.name and e.from_obj not in seen:
            seen.append(e.from_obj)
    return seen


def _input_map(m: Mapping, node: Node) -> dict[str, str]:
    """input port name -> upstream column name (from the connectors)."""
    return {e.to_field: e.from_field for e in m.edges if e.to_obj == node.name}


def _substitute(expr: str, mapping: dict[str, str]) -> str:
    """Whole-word replacement of port/local-variable names (longest first).

    Two-phase: names are first replaced by unique placeholder tokens, then the
    tokens by their target values.  This avoids re-substituting the *replacement
    text* when two names share a prefix (e.g. ``ID_RAPPORTO`` and
    ``ID_RAPPORTO1``): a later, shorter name would otherwise match inside an
    already-emitted ``[ID_RAPPORTO]`` value.
    """
    names = sorted(mapping, key=len, reverse=True)
    tokens: list[tuple[str, str]] = []
    for i, name in enumerate(names):
        tok = f"\x00pc{i}\x00"
        expr = re.sub(r"\b" + re.escape(name) + r"\b", tok, expr)
        tokens.append((tok, mapping[name]))
    for tok, val in tokens:
        expr = expr.replace(tok, val)
    return expr


# --- upstream CTE resolution --------------------------------------------------

def _router_group_for_field(node: Node, field: str) -> str:
    """Name of the Router output group that owns ``field`` (or '')."""
    for p in node.ports:
        if p.name == field:
            return p.group or ""
    return ""


def _router_group_cte(router_name: str, group_name: str) -> str:
    return _ident(f"{router_name}__{group_name}")


def _upstream_refs(m: Mapping, node: Node) -> list[str]:
    """CTE references (already bracket-quoted) that feed this node.

    A Router upstream is split into one CTE per output group, so its
    reference is resolved per connector field back to the group CTE.
    """
    refs: list[str] = []
    for e in m.edges:
        if e.to_obj != node.name:
            continue
        up = m.nodes.get(e.from_obj)
        if up is not None and up.type == "Router":
            refs.append(_router_group_cte(up.name, _router_group_for_field(up, e.from_field)))
        else:
            refs.append(_ident(e.from_obj))
    seen: set[str] = set()
    out: list[str] = []
    for r in refs:
        if r not in seen:
            seen.add(r)
            out.append(r)
    return out


# --- banner / source / target -------------------------------------------------

def _emit_source_def(node: Node) -> str:
    lines = [
        _banner(node),
        f"--   database : {node.dbdname or '(n/a)'}",
        f"--   owner    : {node.ownername or '(n/a)'}",
        f"--   columns  : {len(node.ports)}",
        "--   (physical read is performed by the Source Qualifier attached to it)",
    ]
    return "\n".join(lines)


def _emit_target_def(node: Node) -> str:
    return "\n".join([
        _banner(node),
        f"--   columns  : {len(node.ports)}",
    ])


def _emit_sq(node: Node, m: Mapping) -> str:
    """Source Qualifier -> a CTE that SELECTs the source columns from the table.

    The physical table is the *associated source definition* (the
    ASSOCIATED_SOURCE_INSTANCE on the SQ instance), not the SQ's own name.
    """
    src = m.source_node
    table_name = node.associated_source or node.definition or node.name
    owner = src.ownername if src else ""
    table = _ident(table_name)
    if owner:
        table = f"{_ident(owner)}.{table}"

    cols = [p.name for p in node.output_ports]

    filter_attr = node.attributes.get("Source Filter", "").strip()
    query_attr = node.attributes.get("Sql Query", "").strip()

    lines = [_banner(node)]
    if query_attr:
        # Custom SQL override: kept verbatim (it is not run through the
        # expression translator, so any source-dialect syntax stays as-is) but
        # wrapped in a CTE so downstream references resolve to a defined name.
        lines.append("--   custom SQL override (kept verbatim, wrapped in a CTE)")
        lines.append(f"{_ident(node.name)} AS (")
        lines.append(query_attr)
        lines.append(")")
        return "\n".join(lines)

    lines.append(f"{_ident(node.name)} AS (")
    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(_ident(c) for c in cols))
    lines.append(f"    FROM {table}")
    if filter_attr:
        lines.append(f"    -- Source Filter: {filter_attr}")
    lines.append(")")
    return "\n".join(lines)


# --- relational (CTE) transformations ----------------------------------------

def _emit_expression(node: Node, m: Mapping) -> str:
    upstream = _upstream_refs(m, node)
    from_clause = f"FROM {upstream[0]}" if len(upstream) == 1 else \
        f"FROM {upstream[0]} /* + {len(upstream)-1} more inputs */"

    in_map = _input_map(m, node)

    # local variables -> sequential CROSS APPLY, referenced by alias
    lv_alias: dict[str, str] = {}
    applies: list[str] = []
    for idx, lv in enumerate(node.local_variables):
        expr = _substitute(lv.expression, lv_alias)
        expr = translate(expr)
        alias = f"lv{idx}"
        applies.append(
            f"    CROSS APPLY (SELECT {expr} AS {_ident(lv.name)}) {alias}"
        )
        lv_alias[lv.name] = f"{alias}.{_ident(lv.name)}"

    # inner derived table: rename upstream columns to this node's input port names
    rename_cols = []
    for inp in node.input_ports:
        if inp.name in in_map:
            src_col = in_map[inp.name]
            if src_col != inp.name:
                rename_cols.append(f"{_ident(src_col)} AS {_ident(inp.name)}")
            else:
                rename_cols.append(_ident(inp.name))

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")

    select_lines = []
    for p in node.output_ports:
        if p in node.local_variables:
            continue
        is_passthrough = (p.expression or "").strip() == p.name or not (p.expression or "").strip()
        if is_passthrough:
            select_lines.append(_ident(p.name))
        else:
            expr = _substitute(p.expression, lv_alias)
            expr = translate(expr)
            select_lines.append(f"{expr} AS {_ident(p.name)}  -- {p.name}")

    # Always wrap the upstream in a rename subquery so this node's input-port
    # names (which may differ from the upstream column names) are available to
    # both the output expressions and any CROSS APPLY local variables.
    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(select_lines))
    lines.append("    FROM (")
    if rename_cols:
        lines.append("        SELECT " + ",\n               ".join(rename_cols))
    else:
        lines.append("        SELECT *")
    lines.append(f"        {from_clause}")
    lines.append("    ) src")
    for a in applies:
        lines.append(a)
    lines.append(")")
    return "\n".join(lines)


def _emit_aggregator(node: Node, m: Mapping) -> str:
    upstream = _upstream_refs(m, node)
    from_clause = f"FROM {upstream[0]}" if len(upstream) == 1 else \
        f"FROM {upstream[0]} /* + {len(upstream)-1} more inputs */"
    in_map = _input_map(m, node)

    group_ports = [p for p in node.output_ports if (p.expressiontype or "").upper() == "GROUPBY"]
    agg_ports = [p for p in node.output_ports if (p.expressiontype or "").upper() != "GROUPBY"]

    rename_cols = []
    for inp in node.input_ports:
        if inp.name in in_map:
            src_col = in_map[inp.name]
            if src_col != inp.name:
                rename_cols.append(f"{_ident(src_col)} AS {_ident(inp.name)}")
            else:
                rename_cols.append(_ident(inp.name))

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")

    select_lines = []
    for p in group_ports:
        select_lines.append(_ident(p.name))
    for p in agg_ports:
        expr = translate(p.expression)
        select_lines.append(f"{expr} AS {_ident(p.name)}  -- {p.name}")

    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(select_lines))
    lines.append("    FROM (")
    lines.append("        SELECT " + ", ".join(rename_cols))
    lines.append(f"        {from_clause}")
    lines.append("    ) src")
    if group_ports:
        lines.append("    GROUP BY " + ", ".join(_ident(p.name) for p in group_ports))
    lines.append(")")
    return "\n".join(lines)


def _emit_filter(node: Node, m: Mapping) -> str:
    """Filter -> CTE that passes the input columns through a WHERE clause."""
    in_map = _input_map(m, node)
    upstream = _upstream_refs(m, node)
    from_cte = upstream[0] if upstream else "(none)"

    cond = translate(node.attributes.get("Filter Condition", ""))
    cond = _substitute(cond, in_map)

    select_lines = []
    for p in node.ports:
        if "OUTPUT" in (p.porttype or ""):
            src_col = in_map.get(p.name, p.name)
            select_lines.append(f"{_ident(src_col)} AS {_ident(p.name)}")

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")
    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(select_lines))
    lines.append(f"    FROM {from_cte}")
    lines.append(f"    WHERE {cond}")
    lines.append(")")
    return "\n".join(lines)


def _emit_router(node: Node, m: Mapping) -> list[str]:
    """Router -> one CTE per output group (plus the DEFAULT group).

    Output group fields are passthroughs of their ``REF_FIELD`` (a Router
    input field); the group's filter ``EXPRESSION`` becomes the WHERE clause.
    The ``OUTPUT/DEFAULT`` group takes the negation of every other group.
    """
    in_map = _input_map(m, node)                 # router input field -> upstream col
    upstream = _upstream_refs(m, node)
    from_cte = upstream[0] if upstream else "(none)"

    output_groups = [g for g in node.groups if g.type in ("OUTPUT", "OUTPUT/DEFAULT")]

    non_default_conds: list[str] = []
    for g in output_groups:
        if g.type == "OUTPUT":
            non_default_conds.append(translate(_substitute(g.expression, in_map)))

    blocks: list[str] = []
    for g in output_groups:
        out_fields = [p for p in node.ports
                      if p.group == g.name and "OUTPUT" in (p.porttype or "")]
        select_lines = [
            f"{_ident(in_map.get(f.ref_field, f.ref_field))} AS {_ident(f.name)}"
            for f in out_fields
        ]

        if g.type == "OUTPUT":
            where = translate(_substitute(g.expression, in_map))
        else:
            parts = [f"NOT ({c})" for c in non_default_conds]
            where = " AND ".join(parts) if parts else "1 = 0"

        lines = [_banner(node) + f"\n--   group     : {g.name}"]
        lines.append(f"{_router_group_cte(node.name, g.name)} AS (")
        lines.append("    SELECT")
        lines.append("        " + ",\n        ".join(select_lines))
        lines.append(f"    FROM {from_cte}")
        lines.append(f"    WHERE {where}")
        lines.append(")")
        blocks.append("\n".join(lines))

    return blocks


_JOIN_TYPE_MAP = {
    "Normal Join": "INNER JOIN",
    "Master Outer Join": "LEFT OUTER JOIN",
    "Detail Outer Join": "RIGHT OUTER JOIN",
    "Full Outer Join": "FULL OUTER JOIN",
}


def _emit_joiner(node: Node, m: Mapping) -> str:
    """Joiner -> CTE with a JOIN between the master and detail inputs.

    Master ports are those whose PORTTYPE ends in ``/MASTER``; the other input
    group is the detail side.  ``Join Type`` (PowerCenter) maps to the SQL
    Server join direction; ``Join Condition`` is translated and its port names
    are qualified with ``m.`` / ``d.``.
    """
    in_map = _input_map(m, node)

    master_ports = [p for p in node.ports if "MASTER" in (p.porttype or "")]
    detail_ports = [p for p in node.ports
                    if "INPUT" in (p.porttype or "") and "MASTER" not in (p.porttype or "")]

    master_name_set = {p.name for p in master_ports}
    master_up = None
    detail_up = None
    for up_name in _upstreams(m, node):
        feeds_master = any(
            e.from_obj == up_name and e.to_obj == node.name and e.to_field in master_name_set
            for e in m.edges
        )
        if feeds_master:
            master_up = up_name
        else:
            detail_up = up_name

    join_kw = _JOIN_TYPE_MAP.get(node.attributes.get("Join Type", ""), "INNER JOIN")

    cond = translate(node.attributes.get("Join Condition", ""))
    master_subst = {p.name: f"m.{_ident(in_map.get(p.name, p.name))}" for p in master_ports}
    detail_subst = {p.name: f"d.{_ident(in_map.get(p.name, p.name))}" for p in detail_ports}
    cond = _substitute(cond, {**master_subst, **detail_subst})

    select_lines = []
    for p in node.ports:
        if "INPUT" not in (p.porttype or ""):
            continue
        side = "m" if "MASTER" in (p.porttype or "") else "d"
        col = in_map.get(p.name, p.name)
        select_lines.append(f"{side}.{_ident(col)} AS {_ident(p.name)}")

    lines = [_banner(node)]
    lines.append(f"--   join type : {node.attributes.get('Join Type', '')}")
    lines.append(f"{_ident(node.name)} AS (")
    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(select_lines))
    lines.append(f"    FROM {_ident(master_up or '(master)')} m")
    lines.append(f"    {join_kw} {_ident(detail_up or '(detail)')} d")
    lines.append(f"        ON {cond}")
    lines.append(")")
    return "\n".join(lines)


def _emit_union(node: Node, m: Mapping) -> str:
    """Union (Custom Transformation, template "Union Transformation") -> CTE.

    One ``UNION ALL`` branch per ``<GROUP TYPE="INPUT">``; each branch selects
    the group's input fields renamed to the output field names via
    ``FIELDDEPENDENCY``.
    """
    out_group_name = next(
        (g.name for g in node.groups if g.type == "OUTPUT"), "OUTPUT"
    )
    input_groups = [g for g in node.groups if g.type == "INPUT"]
    dep = dict(node.field_dependencies)          # inputfield -> outputfield

    out_fields = [p for p in node.ports if p.group == out_group_name]

    branches: list[tuple[str, list[str]]] = []
    for g in input_groups:
        # upstream instance feeding this input group
        up_name = None
        for e in m.edges:
            if e.to_obj != node.name:
                continue
            p = node.port(e.to_field)
            if p is not None and p.group == g.name:
                up_name = e.from_obj
                break

        select_lines = []
        for out_p in out_fields:
            for p in node.ports:
                if p.group == g.name and dep.get(p.name) == out_p.name:
                    select_lines.append(f"{_ident(p.name)} AS {_ident(out_p.name)}")
                    break
        branches.append((up_name, select_lines))

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")
    for idx, (up_name, select_lines) in enumerate(branches):
        if idx > 0:
            lines.append("    UNION ALL")
        lines.append("    SELECT")
        lines.append("        " + ",\n        ".join(select_lines))
        lines.append(f"    FROM {_ident(up_name or '(input)')}")
    lines.append(")")
    return "\n".join(lines)


# --- Update Strategy (statement) ---------------------------------------------

def _emit_update_strategy(node: Node, m: Mapping, target: Node) -> str:
    """Update Strategy -> INSERT / UPDATE / DELETE / MERGE against its target.

    ``Update Strategy Expression`` is either a literal strategy constant
    (DD_INSERT / DD_UPDATE / DD_DELETE / DD_REJECT) or an expression that
    returns one of them per row (emitted as a MERGE).  The UPDATE / DELETE /
    MERGE key is the target's first mapped column (documented assumption:
    the physical key is not exported in this repository version).
    """
    strategy = (node.attributes.get("Update Strategy Expression", "") or "").strip()

    src_refs = _upstream_refs(m, node)
    src_cte = src_refs[0] if src_refs else "(none)"

    by_field = {e.to_field: e.from_field for e in m.edges if e.to_obj == target.name}
    us_in_map = _input_map(m, node)              # us input port -> source column

    mapped: list[tuple[str, str]] = []
    for p in target.ports:
        if p.name in by_field:
            us_field = by_field[p.name]
            src_col = us_in_map.get(us_field, us_field)
            mapped.append((p.name, src_col))

    lines = [_banner(node)]
    lines.append(f"--   strategy  : {strategy}")
    lines.append(f"--   target    : {target.name}")
    lines.append("--   (write is emitted here; the target definition is informational)")

    if strategy == "DD_INSERT":
        cols = ", ".join(_ident(tf) for tf, _ in mapped)
        sel = ",\n        ".join(_ident(sc) for _, sc in mapped)
        lines.append(f"INSERT INTO {_ident(target.name)} ({cols})")
        lines.append("SELECT")
        lines.append("        " + sel)
        lines.append(f"FROM {src_cte};")
    elif strategy == "DD_UPDATE":
        key = mapped[0][0] if mapped else "?"
        sets = ",\n    ".join(f"tgt.{_ident(tf)} = src.{_ident(sc)}" for tf, sc in mapped)
        lines.append(f"--   (join key = first mapped column: {key})")
        lines.append(f"UPDATE {_ident(target.name)} tgt")
        lines.append("SET")
        lines.append("    " + sets)
        lines.append(f"FROM {src_cte} src")
        lines.append(f"WHERE tgt.{_ident(key)} = src.{_ident(key)};")
    elif strategy == "DD_DELETE":
        key = mapped[0][0] if mapped else "?"
        lines.append("DELETE tgt")
        lines.append(f"FROM {_ident(target.name)} tgt")
        lines.append(f"INNER JOIN {src_cte} src ON tgt.{_ident(key)} = src.{_ident(key)};")
    elif strategy == "DD_REJECT":
        lines.append("-- (DD_REJECT: rows are rejected, no write emitted)")
    else:
        key = mapped[0][0] if mapped else "?"
        sets = ",\n    ".join(f"tgt.{_ident(tf)} = src.{_ident(sc)}" for tf, sc in mapped)
        cols = ", ".join(_ident(tf) for tf, _ in mapped)
        sel = ", ".join(_ident(sc) for _, sc in mapped)
        lines.append(f"--   (expression-driven strategy -> MERGE; key = {key})")
        lines.append(f"MERGE INTO {_ident(target.name)} tgt")
        lines.append(f"USING {src_cte} src ON tgt.{_ident(key)} = src.{_ident(key)}")
        lines.append("WHEN MATCHED THEN UPDATE SET")
        lines.append("    " + sets)
        lines.append(f"WHEN NOT MATCHED THEN INSERT ({cols}) VALUES ({sel});")

    return "\n".join(lines)


def _emit_final_insert(m: Mapping, target: Node) -> str:
    """Target Definition -> the single final INSERT ... SELECT statement."""
    # edges feeding the target, ordered by target field order
    by_field = {e.to_field: e.from_field for e in m.edges if e.to_obj == target.name}
    mapped = [(p.name, by_field[p.name]) for p in target.ports if p.name in by_field]
    unmapped = [p.name for p in target.ports if p.name not in by_field]

    lines = [_banner(target)]
    if unmapped:
        lines.append("--   (target columns not mapped by any connector: "
                     + ", ".join(unmapped) + ")")

    insert_cols = ", ".join(_ident(tf) for tf, _ in mapped)
    select_cols = ",\n        ".join(
        (_ident(ff) if ff == tf else f"{_ident(ff)} AS {_ident(tf)}")
        for tf, ff in mapped
    )
    src = _upstream_refs(m, target)
    from_cte = src[0] if src else _ident("(none)")

    lines.append(f"INSERT INTO {_ident(target.name)} ({insert_cols})")
    lines.append("SELECT")
    lines.append("        " + select_cols)
    lines.append(f"FROM {from_cte};")
    return "\n".join(lines)


def emit(m: Mapping) -> str:
    lines: list[str] = []
    lines.append("-- ==========================================================================")
    lines.append(f"-- Mapping  : {m.name}")
    lines.append(f"-- Folder   : {m.folder}")
    lines.append(f"-- Repository: {m.repository}")
    lines.append("-- ==========================================================================")
    lines.append("-- Generated by pc2sql: one SQL object (CTE) per PowerCenter object,")
    lines.append("-- kept in data-flow order and referenced by PowerCenter object name.")
    lines.append("")

    # source / target definition banners first (informational references)
    for src in m.source_nodes:
        lines.append(_emit_source_def(src))
        lines.append("")
    for tgt in m.target_nodes:
        lines.append(_emit_target_def(tgt))
        lines.append("")

    # CTE chain in topological (data-flow) order, skipping definitions and the
    # Update Strategy (which is emitted as a statement after the CTE chain).
    cte_lines: list[str] = []
    for name in m.order:
        node = m.nodes[name]
        if node.is_source or node.is_target:
            continue
        if node.type == "Source Qualifier":
            cte_lines.append(_emit_sq(node, m))
        elif node.type == "Expression":
            cte_lines.append(_emit_expression(node, m))
        elif node.type == "Aggregator":
            cte_lines.append(_emit_aggregator(node, m))
        elif node.type == "Filter":
            cte_lines.append(_emit_filter(node, m))
        elif node.type == "Joiner":
            cte_lines.append(_emit_joiner(node, m))
        elif node.is_union:
            cte_lines.append(_emit_union(node, m))
        elif node.type == "Router":
            cte_lines.extend(_emit_router(node, m))
        elif node.type == "Update Strategy":
            continue                       # statement, emitted after the WITH block
        else:
            cte_lines.append(_banner(node) + "\n" +
                             f"-- {_ident(node.name)}: type '{node.type}' not yet emitted "
                             f"(extend emitter for this transformation type)")

    if cte_lines:
        lines.append("WITH")
        lines.append(",\n".join(cte_lines))
        lines.append("")

    # final write statements, one per target
    for tgt in m.target_nodes:
        up_names = _upstreams(m, tgt)
        up_node = m.nodes.get(up_names[0]) if len(up_names) == 1 else None
        if up_node is not None and up_node.type == "Update Strategy":
            lines.append(_emit_update_strategy(up_node, m, tgt))
        else:
            lines.append(_emit_final_insert(m, tgt))
        lines.append("")

    return "\n".join(lines)
