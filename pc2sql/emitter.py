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
* Union            -> CTE with ``UNION ALL`` of every input group: the
  connector gives the upstream column of each input field and
  ``FIELDDEPENDENCY`` gives the output field it feeds.  An input field no
  connector feeds contributes NULL (PowerCenter's unconnected port).
* Sorter           -> passthrough CTE (``SELECT DISTINCT`` when
  ``Distinct=YES``); sort keys are recorded as comments because T-SQL has no
  ordered relation and a CTE-level ORDER BY would not survive downstream.
* Update Strategy  -> an INSERT / UPDATE / DELETE / MERGE statement (a
  statement block, not a CTE) whose form follows ``Update Strategy Expression``.

Per-object details worth knowing:

* Source Qualifier -> CTE over the associated source table; ``Source Filter``
  becomes its WHERE clause (a ``Sql Query`` override takes precedence, as in
  PowerCenter).
* Expression / Aggregator -> ``LOCAL VARIABLE`` ports become a CROSS APPLY
  chain, so aggregates can reference them (``SUM(SALDO_CBI_IN)`` where the
  variable is a constant).

Two kinds of object are emitted outside the CTE chain:

* ``<MAPPINGVARIABLE>`` -> batch-scoped ``DECLARE`` statements (``$$DATA_ELAB``
  becomes ``@DATA_ELAB``), emitted before the ``WITH``.
* Source Qualifier ``Sql Query`` overrides -> the statement is translated from
  the source dialect to T-SQL (``expressions.translate_sql``), including the
  ``--`` comments that the export flattened onto a single line; without that
  step a comment would swallow the rest of the statement.
"""

from __future__ import annotations

import re

from .expressions import has_flattened_comments, translate, translate_sql
from .ir import Edge, Mapping, MappingVariable, Node, Port


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


def _translate(expr: str, m: Mapping) -> str:
    """Translate a PowerCenter expression, resolving its mapping variables."""
    return translate(expr, m.variable_names)


def _input_map(m: Mapping, node: Node) -> dict[str, str]:
    """input port name -> upstream column name (from the connectors)."""
    return {e.to_field: e.from_field for e in m.edges if e.to_obj == node.name}


def _render_select_list(items: list[tuple[str, str]]) -> str:
    """Render SELECT items as ``code[,]  -- comment`` lines.

    ``items`` holds ``(code, comment)`` pairs.  A T-SQL ``--`` comment runs to
    the end of the line, so the separator comma belongs to the SQL item and
    must be emitted *before* the comment: ``expr AS [x],  -- x``.  Emitting
    ``expr AS [x]  -- x,`` would comment out the comma and leave the SELECT
    list unterminated.
    """
    out: list[str] = []
    last = len(items) - 1
    for idx, (code, comment) in enumerate(items):
        line = "        " + code
        if idx != last:
            line += ","
        if comment:
            line += f"  -- {comment}"
        out.append(line)
    return "\n".join(out)


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


# PowerCenter datatype -> T-SQL type used for the variable declarations.
_PC_TYPE_TO_SQL = {
    "string": "VARCHAR",
    "nstring": "NVARCHAR",
    "date/time": "DATETIME2",
    "date": "DATE",
    "decimal": "DECIMAL",
    "double": "FLOAT",
    "real": "REAL",
    "integer": "INT",
    "bigint": "BIGINT",
    "smallint": "SMALLINT",
    "tinyint": "TINYINT",
}
_STRING_TYPES = ("string", "nstring")
_TEMPORAL_TYPES = ("date/time", "date")


def _variable_declaration(v: MappingVariable) -> str:
    """``DECLARE @NAME <type> = <value>;`` for one mapping variable.

    T-SQL variables (rather than SQLCMD ``:setvar``) keep the script runnable
    in SSMS without SQLCMD mode.  The declaration carries no value when the
    PowerCenter variable has no default, so a forgotten setting cannot silently
    filter on an arbitrary date.
    """
    kind = (v.datatype or "").strip().lower()
    sql_type = _PC_TYPE_TO_SQL.get(kind, "VARCHAR(255)")
    if kind in _STRING_TYPES:
        limit = 4000 if kind == "nstring" else 8000
        precision = int(v.precision) if v.precision.isdigit() else 0
        sql_type = f"{sql_type}(MAX)" if not 0 < precision <= limit else f"{sql_type}({precision})"
    elif kind == "decimal":
        if v.precision.isdigit():
            sql_type = f"DECIMAL({v.precision}{', ' + v.scale if v.scale.isdigit() else ''})"
        else:
            sql_type = "DECIMAL(18, 2)"

    if not v.default:
        value = "NULL"
    elif kind in _STRING_TYPES or kind in _TEMPORAL_TYPES:
        value = "'" + v.default.replace("'", "''") + "'"
    else:
        value = v.default

    return f"DECLARE @{v.reference_name} {sql_type} = {value};"


def _emit_mapping_variables(m: Mapping) -> str:
    """MAPPINGVARIABLE objects -> batch-scoped DECLARE statements."""
    if not m.variables:
        return ""
    lines = [
        "-- ==========================================================================",
        "-- Mapping variables (PowerCenter MAPPINGVARIABLE)",
        "-- ==========================================================================",
    ]
    for v in m.variables:
        details = [v.type_text or "string"]
        if v.aggfunction:
            details.append(f"aggregate={v.aggfunction}")
        if v.isparam:
            details.append(f"parameter={v.isparam}")
        if v.description:
            details.append(v.description)
        lines.append(f"--   {v.name}  ({', '.join(details)})")
    lines.append("--   Set each value before running the script: a variable left NULL makes")
    lines.append("--   the date comparisons evaluate to NULL (no rows) instead of comparing")
    lines.append("--   against an arbitrary date.")
    for v in m.variables:
        lines.append(_variable_declaration(v))
    return "\n".join(lines)


def _emit_sq(node: Node, m: Mapping) -> str:
    """Source Qualifier -> a CTE that SELECTs the source columns from the table.

    The physical table is the *associated source definition* (the
    ASSOCIATED_SOURCE_INSTANCE on the SQ instance), not the SQ's own name, and
    its OWNERNAME is the schema: a mapping can read several sources, so the
    owner is resolved per Source Qualifier.  The ``Source Filter`` attribute is
    the row filter PowerCenter applies on the source read, so it becomes the
    WHERE clause (a ``Sql Query`` override replaces the whole read instead).
    """
    assoc = m.nodes.get(node.associated_source) if node.associated_source else None
    table_name = node.associated_source or node.definition or node.name
    owner = assoc.ownername if assoc is not None else ""
    table = _ident(table_name)
    if owner:
        table = f"{_ident(owner)}.{table}"

    cols = [p.name for p in node.output_ports]

    filter_attr = node.attributes.get("Source Filter", "").strip()
    query_attr = node.attributes.get("Sql Query", "").strip()

    lines = [_banner(node)]
    if query_attr:
        # Custom SQL override: dialect functions are translated to T-SQL and
        # the statement is wrapped in a CTE so downstream references resolve
        # to a defined name.
        lines.append("--   custom SQL override (source dialect translated, wrapped in a CTE)")
        if has_flattened_comments(query_attr):
            lines.append("--   the export had flattened this override onto one line: its '--'")
            lines.append("--   comments are back on their own lines (verify their extent)")
        lines.append(f"{_ident(node.name)} AS (")
        lines.append(translate_sql(query_attr, m.variable_names))
        lines.append(")")
        return "\n".join(lines)

    lines.append(f"{_ident(node.name)} AS (")
    lines.append("    SELECT")
    lines.append("        " + ",\n        ".join(_ident(c) for c in cols))
    lines.append(f"    FROM {table}")
    if filter_attr:
        # The Source Filter is the WHERE of the source read: translate it (it
        # is written in the source dialect and may use mapping variables) and
        # apply it, rather than keeping it as a comment.
        cond = translate_sql(filter_attr, m.variable_names)
        body = "\n".join(
            line for line in cond.splitlines() if not line.lstrip().startswith("--")
        ).strip()
        if body:
            if "\n" in cond:
                lines.append("    WHERE")
                lines.extend(
                    ("        " + line) if line.strip() else line
                    for line in cond.splitlines()
                )
            else:
                lines.append(f"    WHERE {cond}")
        else:
            lines.append("    -- Source Filter is fully commented out in the export:")
            lines.append("    -- " + filter_attr)
    lines.append(")")
    return "\n".join(lines)


# --- relational (CTE) transformations ----------------------------------------

def _local_variable_applies(node: Node, m: Mapping) -> tuple[list[str], dict[str, str]]:
    """``LOCAL VARIABLE`` ports -> sequential CROSS APPLY lines + alias map.

    A PowerCenter local variable is a row-level expression, evaluated before
    the node's own expressions (and usable in any of them).  The CROSS APPLY
    chain keeps that order, so a variable can reference the previous ones.
    """
    applies: list[str] = []
    alias: dict[str, str] = {}
    for idx, lv in enumerate(node.local_variables):
        expr = _substitute(lv.expression, alias)
        expr = _translate(expr, m)
        name = f"lv{idx}"
        applies.append(
            f"    CROSS APPLY (SELECT {expr} AS {_ident(lv.name)}) {name}"
        )
        alias[lv.name] = f"{name}.{_ident(lv.name)}"
    return applies, alias


def _rename_columns(node: Node, in_map: dict[str, str]) -> list[str]:
    """Upstream columns renamed to this node's input port names."""
    cols: list[str] = []
    for inp in node.input_ports:
        if inp.name in in_map:
            src_col = in_map[inp.name]
            if src_col != inp.name:
                cols.append(f"{_ident(src_col)} AS {_ident(inp.name)}")
            else:
                cols.append(_ident(inp.name))
    return cols


def _emit_expression(node: Node, m: Mapping) -> str:
    upstream = _upstream_refs(m, node)
    from_clause = f"FROM {upstream[0]}" if len(upstream) == 1 else \
        f"FROM {upstream[0]} /* + {len(upstream)-1} more inputs */"

    in_map = _input_map(m, node)
    applies, lv_alias = _local_variable_applies(node, m)
    rename_cols = _rename_columns(node, in_map)

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")

    select_lines: list[tuple[str, str]] = []
    for p in node.output_ports:
        if p in node.local_variables:
            continue
        is_passthrough = (p.expression or "").strip() == p.name or not (p.expression or "").strip()
        if is_passthrough:
            select_lines.append((_ident(p.name), ""))
        else:
            expr = _substitute(p.expression, lv_alias)
            expr = _translate(expr, m)
            select_lines.append((f"{expr} AS {_ident(p.name)}", p.name))

    # Always wrap the upstream in a rename subquery so this node's input-port
    # names (which may differ from the upstream column names) are available to
    # both the output expressions and any CROSS APPLY local variables.
    lines.append("    SELECT")
    lines.append(_render_select_list(select_lines))
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
    """Aggregator -> CTE with GROUP BY (``GROUPBY`` ports) and aggregates.

    ``LOCAL VARIABLE`` ports are resolved like in an Expression (CROSS APPLY),
    because an aggregate can reference them (``SUM(SALDO_CBI_IN)`` where the
    variable is 0): without them the aggregate would name a column that the
    input does not have.
    """
    upstream = _upstream_refs(m, node)
    from_clause = f"FROM {upstream[0]}" if len(upstream) == 1 else \
        f"FROM {upstream[0]} /* + {len(upstream)-1} more inputs */"
    in_map = _input_map(m, node)
    applies, lv_alias = _local_variable_applies(node, m)

    group_ports = [p for p in node.output_ports if (p.expressiontype or "").upper() == "GROUPBY"]
    agg_ports = [p for p in node.output_ports if (p.expressiontype or "").upper() != "GROUPBY"]

    rename_cols = _rename_columns(node, in_map)

    lines = [_banner(node)]
    lines.append(f"{_ident(node.name)} AS (")

    select_lines: list[tuple[str, str]] = []
    for p in group_ports:
        select_lines.append((_ident(p.name), ""))
    for p in agg_ports:
        expr = _translate(_substitute(p.expression, lv_alias), m)
        select_lines.append((f"{expr} AS {_ident(p.name)}", p.name))

    lines.append("    SELECT")
    lines.append(_render_select_list(select_lines))
    lines.append("    FROM (")
    lines.append("        SELECT " + ", ".join(rename_cols))
    lines.append(f"        {from_clause}")
    lines.append("    ) src")
    for a in applies:
        lines.append(a)
    if group_ports:
        lines.append("    GROUP BY " + ", ".join(_ident(p.name) for p in group_ports))
    lines.append(")")
    return "\n".join(lines)


def _emit_sorter(node: Node, m: Mapping) -> str:
    """Sorter -> passthrough CTE, with the PowerCenter sort keys documented.

    A Sorter only orders rows (PowerCenter uses it to feed Sorted Input
    aggregators/joiners).  T-SQL has no ordered relation and the optimizer is
    free to discard an ORDER BY inside a CTE, so emitting one would falsely
    promise ordering: the CTE passes the rows through instead and records the
    keys as comments.  ``Distinct=YES`` is a real semantic (deduplication),
    so it is emitted as ``SELECT DISTINCT``.
    """
    upstream = _upstream_refs(m, node)
    from_cte = upstream[0] if upstream else "(none)"
    distinct = (node.attributes.get("Distinct", "") or "").strip().upper() == "YES"
    sort_keys = [
        (p.name, (p.sortdirection or "ASCENDING").upper())
        for p in node.ports
        if (p.issortkey or "").strip().upper() == "YES"
    ]
    cols = [p.name for p in node.output_ports] or [p.name for p in node.ports]

    lines = [_banner(node)]
    lines.append("--   sort keys : " + (
        ", ".join(f"{name} {direction}" for name, direction in sort_keys)
        if sort_keys else "(none)"
    ))
    lines.append("--   (ordering is informational: T-SQL does not preserve CTE row order)")
    lines.append(f"{_ident(node.name)} AS (")
    lines.append("    SELECT" + (" DISTINCT" if distinct else ""))
    lines.append("        " + ",\n        ".join(_ident(c) for c in cols))
    lines.append(f"    FROM {from_cte}")
    lines.append(")")
    return "\n".join(lines)


def _emit_filter(node: Node, m: Mapping) -> str:
    """Filter -> CTE that passes the input columns through a WHERE clause."""
    in_map = _input_map(m, node)
    upstream = _upstream_refs(m, node)
    from_cte = upstream[0] if upstream else "(none)"

    cond = _translate(node.attributes.get("Filter Condition", ""), m)
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
            non_default_conds.append(_translate(_substitute(g.expression, in_map), m))

    blocks: list[str] = []
    for g in output_groups:
        out_fields = [p for p in node.ports
                      if p.group == g.name and "OUTPUT" in (p.porttype or "")]
        select_lines = [
            f"{_ident(in_map.get(f.ref_field, f.ref_field))} AS {_ident(f.name)}"
            for f in out_fields
        ]

        if g.type == "OUTPUT":
            where = _translate(_substitute(g.expression, in_map), m)
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


# PowerCenter join type -> T-SQL, with the MASTER instance emitted on the left.
# PowerCenter keeps the rows of the *other* side for its outer joins:
#   Detail Outer Join -> all master rows + matching detail rows -> LEFT JOIN,
#   Master Outer Join -> all detail rows + matching master rows -> RIGHT JOIN.
_JOIN_TYPE_MAP = {
    "Normal Join": "INNER JOIN",
    "Master Outer Join": "RIGHT OUTER JOIN",
    "Detail Outer Join": "LEFT OUTER JOIN",
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

    cond = _translate(node.attributes.get("Join Condition", ""), m)
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
    join_type = node.attributes.get("Join Type", "")
    lines.append(f"--   join type : {join_type}" + (f"  ->  {join_kw}" if join_type else ""))
    lines.append(f"--   master    : {master_up or '(n/a)'}   detail: {detail_up or '(n/a)'}")
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

    One ``UNION ALL`` branch per ``<GROUP TYPE="INPUT">``.  A connector carries
    the upstream column name to the Union's input field name (they rarely
    coincide: ``IMPORTO_ENTRATEL_AG`` -> ``IMPORTO_ENTRATEL11``), and
    ``FIELDDEPENDENCY`` maps each input field to the output field, so a branch
    selects ``<upstream column> AS <output field>``.
    """
    out_group_name = next(
        (g.name for g in node.groups if g.type == "OUTPUT"), "OUTPUT"
    )
    input_groups = [g for g in node.groups if g.type == "INPUT"]
    dep = dict(node.field_dependencies)          # inputfield -> outputfield
    # (upstream instance, input field) -> upstream column
    conn = {(e.from_obj, e.to_field): e.from_field
            for e in m.edges if e.to_obj == node.name}

    out_fields = [p for p in node.ports if p.group == out_group_name]

    branches: list[tuple[str, list[tuple[str, str]]]] = []
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

        select_lines: list[tuple[str, str]] = []
        for out_p in out_fields:
            in_p = next((p for p in node.ports
                         if p.group == g.name and dep.get(p.name) == out_p.name), None)
            if in_p is None:
                select_lines.append((
                    f"NULL AS {_ident(out_p.name)}",
                    f"WARNING: group {g.name} has no input field for {out_p.name}",
                ))
                continue
            src_col = conn.get((up_name, in_p.name))
            if src_col is None:
                up_node = m.nodes.get(up_name) if up_name else None
                if up_node is not None and up_node.port(in_p.name) is not None:
                    src_col = in_p.name
                else:
                    # An unconnected union input port contributes NULL rows.
                    select_lines.append((
                        f"NULL AS {_ident(out_p.name)}",
                        f"WARNING: input field {in_p.name} is not connected "
                        f"(group {g.name})",
                    ))
                    continue
            select_lines.append((f"{_ident(src_col)} AS {_ident(out_p.name)}", ""))
        branches.append((up_name, select_lines))

    lines = [_banner(node)]
    lines.append("--   groups    : " + ", ".join(g.name for g in input_groups))
    lines.append(f"{_ident(node.name)} AS (")
    for idx, (up_name, select_lines) in enumerate(branches):
        if idx > 0:
            lines.append("    UNION ALL")
        lines.append("    SELECT")
        lines.append(_render_select_list(select_lines))
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

    # mapping variables -> DECLARE statements (referenced by the overrides)
    variables_block = _emit_mapping_variables(m)
    if variables_block:
        lines.append(variables_block)
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
        elif node.type == "Sorter":
            cte_lines.append(_emit_sorter(node, m))
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
