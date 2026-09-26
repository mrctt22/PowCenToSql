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

import xml.etree.ElementTree as ET

from .ir import Edge, Group, Mapping, Node, Port


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


def parse(path: str) -> Mapping:
    tree = ET.parse(path)
    root = tree.getroot()                       # <POWERMART>

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
