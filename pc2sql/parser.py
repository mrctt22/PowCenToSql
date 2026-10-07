"""Parser: PowerCenter repository export XML -> IR (ir.Mapping).

Only reads what it needs and makes no assumptions beyond the standard
POWERMART layout:  POWERMART/REPOSITORY/FOLDER with SOURCE, TARGET and
MAPPING children.  Inside a MAPPING, TRANSFORMATION elements are reusable
definitions, INSTANCE elements are the instances, and CONNECTOR elements
are the edges between instances.

Group-based transformations (Router / Union / Joiner) carry extra structure
that is parsed here and surfaced on the IR:

* ``<GROUP>`` elements (Router output groups hold their filter EXPRESSION,
  the fallback group is ``TYPE="OUTPUT/DEFAULT"``; Union ``INPUT`` groups
  enumerate the source pipelines).
* ``<FIELDDEPENDENCY>`` elements (Union: ``INPUTFIELD`` -> ``OUTPUTFIELD``).
* ``GROUP`` / ``REF_FIELD`` / ``OUTPUTGROUP`` TRANSFORMFIELD attributes.
* ``TEMPLATENAME`` (Custom Transformation template id).
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from pathlib import Path

if __package__:
    from .ir import Edge, Group, Mapping, Node, Port
else:
    # Allow running this file directly, e.g. ``python pc2sql/parser.py x.xml``
    # or launching it from the debugger, without the
    # "attempted relative import with no known parent package" error.
    import sys

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
    from pc2sql.ir import Edge, Group, Mapping, Node, Port  # type: ignore[import-not-found]


def _attr(el: ET.Element, name: str, default: str = "") -> str:
    return el.get(name, default) or default


def _ports_from_fields(el: ET.Element, tag: str) -> list[Port]:
    """Build Port list from SOURCEFIELD / TARGETFIELD / TRANSFORMFIELD children."""
    ports: list[Port] = []
    for f in el.findall(tag):
        ports.append(Port(
            name=_attr(f, "NAME"),
            datatype=_attr(f, "DATATYPE"),
            precision=_attr(f, "PRECISION"),
            scale=_attr(f, "SCALE"),
            porttype=_attr(f, "PORTTYPE"),
            expression=_attr(f, "EXPRESSION"),
            expressiontype=_attr(f, "EXPRESSIONTYPE"),
            group=_attr(f, "GROUP"),
            ref_field=_attr(f, "REF_FIELD"),
            outputgroup=_attr(f, "OUTPUTGROUP"),
        ))
    return ports


def _table_attributes(el: ET.Element) -> dict[str, str]:
    out: dict[str, str] = {}
    for ta in el.findall("TABLEATTRIBUTE"):
        out[_attr(ta, "NAME")] = _attr(ta, "VALUE")
    return out


def _groups(el: ET.Element) -> list[Group]:
    out: list[Group] = []
    for g in el.findall("GROUP"):
        out.append(Group(
            name=_attr(g, "NAME"),
            type=_attr(g, "TYPE"),
            expression=_attr(g, "EXPRESSION"),
            order=_attr(g, "ORDER"),
        ))
    return out


def _field_dependencies(el: ET.Element) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for fd in el.findall("FIELDDEPENDENCY"):
        out.append((_attr(fd, "INPUTFIELD"), _attr(fd, "OUTPUTFIELD")))
    return out

_ATTR_RE = re.compile(rb'([\w:.-]+)\s*=\s*"([^"]*)"')
_ENTITY_RE = re.compile(rb"&(?:[a-zA-Z][a-zA-Z0-9]*|#[0-9]+|#x[0-9a-fA-F]+);")


def _escape_attr_value(value: bytes) -> bytes:
    """Escape '<' and bare '&' inside an attribute value.

    PowerCenter exports sometimes embed SQL predicates such as ``<=`` or
    ``>=`` directly in a TABLEATTRIBUTE VALUE without escaping them, which
    makes the document invalid XML.  Existing entities (``&amp;``, ``&#39;``,
    ...) are preserved; only raw ``<`` and dangling ``&`` are rewritten.
    """
    out = bytearray()
    i, n = 0, len(value)
    while i < n:
        ch = value[i:i + 1]
        if ch == b"<":
            out += b"&lt;"
            i += 1
        elif ch == b"&":
            m = _ENTITY_RE.match(value, i)
            if m is not None:
                out += m.group(0)
                i = m.end()
            else:
                out += b"&amp;"
                i += 1
        else:
            out += ch
            i += 1
    return bytes(out)


def _load_root(path: str) -> ET.Element:
    """Parse the XML root element, tolerating common export/export-save quirks.

    Browsers (and some download tools) prepend a plain-text banner such as
    "This XML file does not appear to have any style information associated
    with it. The document tree is shown below." when an export is saved from
    the browser.  XML forbids content before the root element, so anything
    ahead of the first '<' is discarded.  In addition, stray '<' and '&'
    characters inside attribute values (unescaped SQL operators are the usual
    offenders) are repaired.  Both steps are no-ops for well-formed input.
    """
    data = Path(path).read_bytes()
    start = data.find(b"<")
    if start == -1:
        raise ET.ParseError(f"no XML content found in {path}")
    if start > 0:
        data = data[start:]
    data = _ATTR_RE.sub(
        lambda m: m.group(1) + b'="' + _escape_attr_value(m.group(2)) + b'"',
        data,
    )
    return ET.fromstring(data)


def parse(path: str) -> Mapping:
    root = _load_root(path)                     # <POWERMART>

    repo = root.find("REPOSITORY")
    folder = repo.find("FOLDER") if repo is not None else None
    mapping = Mapping()
    mapping.repository = _attr(repo, "NAME") if repo is not None else ""
    if folder is not None:
        mapping.folder = _attr(folder, "NAME")

    # --- reusable definitions (SOURCE / TARGET) -----------------------------
    source_defs: dict[str, tuple[list[Port], str, str]] = {}   # name -> (ports, dbd, owner)
    target_defs: dict[str, list[Port]] = {}

    if folder is not None:
        for src in folder.findall("SOURCE"):
            name = _attr(src, "NAME")
            source_defs[name] = (
                _ports_from_fields(src, "SOURCEFIELD"),
                _attr(src, "DBDNAME"),
                _attr(src, "OWNERNAME"),
            )
        for tgt in folder.findall("TARGET"):
            name = _attr(tgt, "NAME")
            target_defs[name] = _ports_from_fields(tgt, "TARGETFIELD")

        m = folder.find("MAPPING")
        if m is not None:
            mapping.name = _attr(m, "NAME")

            # transformation definitions
            trans_defs: dict[str, Node] = {}
            for tr in m.findall("TRANSFORMATION"):
                node = Node(
                    name=_attr(tr, "NAME"),
                    type=_attr(tr, "TYPE"),
                    definition=_attr(tr, "NAME"),
                    ports=_ports_from_fields(tr, "TRANSFORMFIELD"),
                    attributes=_table_attributes(tr),
                    groups=_groups(tr),
                    field_dependencies=_field_dependencies(tr),
                    templatename=_attr(tr, "TEMPLATENAME"),
                )
                trans_defs[node.name] = node

            # instances -> nodes (copy definition structure, keep instance name)
            for inst in m.findall("INSTANCE"):
                i_type = _attr(inst, "TYPE")               # SOURCE / TARGET / TRANSFORMATION
                t_type = _attr(inst, "TRANSFORMATION_TYPE")
                defn = _attr(inst, "TRANSFORMATION_NAME")
                name = _attr(inst, "NAME")

                assoc = inst.find("ASSOCIATED_SOURCE_INSTANCE")
                assoc_name = _attr(assoc, "NAME") if assoc is not None else ""

                if i_type == "SOURCE":
                    node = Node(name=name, type="Source Definition", definition=defn,
                                associated_source=assoc_name)
                    node.is_source = True
                    ports, dbd, owner = source_defs.get(defn, ([], "", ""))
                    node.ports = ports
                    node.dbdname = _attr(inst, "DBDNAME") or dbd
                    node.ownername = owner
                elif i_type == "TARGET":
                    node = Node(name=name, type="Target Definition", definition=defn)
                    node.is_target = True
                    node.ports = target_defs.get(defn, [])
                else:
                    # copy the reusable TRANSFORMATION definition's structure,
                    # then overwrite name with the instance name
                    src_node = trans_defs.get(defn)
                    node = Node(
                        name=name,
                        type=t_type or (src_node.type if src_node else ""),
                        definition=defn,
                        ports=list(src_node.ports) if src_node else [],
                        attributes=dict(src_node.attributes) if src_node else {},
                        groups=list(src_node.groups) if src_node else [],
                        field_dependencies=list(src_node.field_dependencies) if src_node else [],
                        templatename=src_node.templatename if src_node else "",
                    )
                    node.associated_source = assoc_name

                mapping.nodes[name] = node

            # connectors -> edges
            for c in m.findall("CONNECTOR"):
                mapping.edges.append(Edge(
                    from_obj=_attr(c, "FROMINSTANCE"),
                    from_field=_attr(c, "FROMFIELD"),
                    from_type=_attr(c, "FROMINSTANCETYPE"),
                    to_obj=_attr(c, "TOINSTANCE"),
                    to_field=_attr(c, "TOFIELD"),
                    to_type=_attr(c, "TOINSTANCETYPE"),
                ))

    _topo_sort(mapping)
    return mapping


def _topo_sort(m: Mapping) -> None:
    """Topological order of instances, following the connector edges.

    Falls back to insertion order for any disconnected objects, so a broken
    or partial graph never crashes the emitter.
    """
    indeg = {name: 0 for name in m.nodes}
    adj: dict[str, list[str]] = {name: [] for name in m.nodes}
    for e in m.edges:
        if e.from_obj in adj and e.to_obj in adj and e.to_obj not in adj[e.from_obj]:
            adj[e.from_obj].append(e.to_obj)
            indeg[e.to_obj] += 1

    ready = [n for n, d in indeg.items() if d == 0]
    # deterministic: sources first, then stable by name
    ready.sort()
    order: list[str] = []
    while ready:
        n = ready.pop(0)
        order.append(n)
        for t in adj[n]:
            indeg[t] -= 1
            if indeg[t] == 0:
                ready.append(t)
                ready.sort()

    for n in m.nodes:                       # anything disconnected / cycles
        if n not in order:
            order.append(n)

    m.order = order


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("usage: python parser.py <mapping.xml>", file=sys.stderr)
        raise SystemExit(2)

    parsed = parse(sys.argv[1])
    print(
        f"mapping {parsed.name!r} [{parsed.repository}/{parsed.folder}]: "
        f"{len(parsed.nodes)} objects, {len(parsed.edges)} connectors"
    )
    for object_name in parsed.order:
        print(f"  {object_name}  ({parsed.nodes[object_name].type})")
