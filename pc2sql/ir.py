"""IR (Intermediate Representation) for a PowerCenter mapping.

The IR is deliberately object-per-object: every PowerCenter object
(source definition, target definition, source qualifier, expression,
aggregator, filter, router, joiner, union, update strategy, ...) becomes one
Node.  Connectors become Edges.  This is what lets the emitter keep one SQL
object (one CTE / statement block) per PowerCenter object instead of fusing
them into a single SELECT.

Extensions for the group-based transformations (Router / Union / Joiner):

* ``Port.group`` / ``Port.ref_field`` / ``Port.outputgroup`` carry the extra
  TRANSFORMFIELD attributes only present on group transformations.
* ``Node.groups`` holds the ``<GROUP>`` elements (Router output groups carry
  their filter ``EXPRESSION``; the default group is ``TYPE="OUTPUT/DEFAULT"``).
* ``Node.field_dependencies`` holds ``<FIELDDEPENDENCY>`` pairs (Union:
  ``INPUTFIELD`` -> ``OUTPUTFIELD``, i.e. which input field feeds each output).
* ``Node.templatename`` distinguishes a Custom Transformation by its template
  (e.g. ``"Union Transformation"``).
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Port:
    """A single port on a transformation / source / target."""
    name: str
    datatype: str = ""
    precision: str = ""
    scale: str = ""
    porttype: str = ""          # INPUT / OUTPUT / INPUT/OUTPUT / LOCAL VARIABLE / INPUT/OUTPUT/MASTER
    expression: str = ""        # PowerCenter expression (raw, XML-decoded)
    expressiontype: str = ""    # GENERAL / GROUPBY / ...
    group: str = ""             # GROUP attribute (Router / Union output/input groups)
    ref_field: str = ""         # REF_FIELD attribute (Router output -> input field)
    outputgroup: str = ""       # OUTPUTGROUP attribute (Union)


@dataclass
class Group:
    """One ``<GROUP>`` element of a group-based transformation.

    Router: the input group is ``TYPE="INPUT"``, each output group is
    ``TYPE="OUTPUT"`` and carries its filter ``EXPRESSION``, and the fallback
    group is ``TYPE="OUTPUT/DEFAULT"`` (no expression).  Union: ``INPUT``
    groups for each source pipeline plus one ``OUTPUT`` group.
    """
    name: str
    type: str = ""              # INPUT / OUTPUT / OUTPUT/DEFAULT
    expression: str = ""        # filter condition (Router OUTPUT groups)
    order: str = ""             # ORDER attribute


@dataclass
class Node:
    """One PowerCenter object, at instance granularity.

    ``name`` is the instance name (what CONNECTOR elements reference).
    ``definition`` is the reusable definition name (TRANSFORMATION_NAME or
    the SOURCE/TARGET definition NAME).  For source/target instances they
    coincide.
    """
    name: str
    type: str                   # "Source Qualifier", "Expression", "Aggregator",
                                # "Filter", "Router", "Joiner", "Update Strategy",
                                # "Custom Transformation", "Source Definition", ...
    definition: str = ""
    ports: list[Port] = field(default_factory=list)
    attributes: dict[str, str] = field(default_factory=dict)  # TABLEATTRIBUTE map
    dbdname: str = ""           # source/target DBDNAME
    ownername: str = ""         # source OWNERNAME (used as schema)
    associated_source: str = "" # Source Qualifier -> its ASSOCIATED_SOURCE_INSTANCE
    is_source: bool = False
    is_target: bool = False
    groups: list[Group] = field(default_factory=list)             # <GROUP> elements
    field_dependencies: list[tuple[str, str]] = field(default_factory=list)  # (inputfield, outputfield)
    templatename: str = ""      # TEMPLATENAME (Custom Transformation template)

    @property
    def output_ports(self) -> list[Port]:
        return [p for p in self.ports if "OUTPUT" in (p.porttype or "")]

    @property
    def input_ports(self) -> list[Port]:
        return [p for p in self.ports if "INPUT" in (p.porttype or "")]

    @property
    def local_variables(self) -> list[Port]:
        return [p for p in self.ports if (p.porttype or "") == "LOCAL VARIABLE"]

    @property
    def is_union(self) -> bool:
        """True when this Custom Transformation is the Union template."""
        return self.type == "Custom Transformation" and self.templatename == "Union Transformation"

    def port(self, name: str) -> Port | None:
        for p in self.ports:
            if p.name == name:
                return p
        return None

    def ports_in_group(self, group_name: str) -> list[Port]:
        return [p for p in self.ports if p.group == group_name]


@dataclass
class Edge:
    """A connector between two instances."""
    from_obj: str
    from_field: str
    from_type: str
    to_obj: str
    to_field: str
    to_type: str


@dataclass
class Mapping:
    name: str = ""
    folder: str = ""
    repository: str = ""
    nodes: dict[str, Node] = field(default_factory=dict)
    edges: list[Edge] = field(default_factory=list)
    order: list[str] = field(default_factory=list)   # topo-sorted instance names

    def node(self, name: str) -> Node | None:
        return self.nodes.get(name)

    @property
    def source_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_source]

    @property
    def target_nodes(self) -> list[Node]:
        return [n for n in self.nodes.values() if n.is_target]

    @property
    def source_node(self) -> Node | None:
        for n in self.nodes.values():
            if n.is_source:
                return n
        return None

    @property
    def target_node(self) -> Node | None:
        for n in self.nodes.values():
            if n.is_target:
                return n
        return None
