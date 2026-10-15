"""Parsed KiCad netlist (kicadxml format exported by `kicad-cli sch export netlist`)."""

import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class Node:
    """One pin on a net."""

    ref: str
    pin: str
    function: str   # pin name, e.g. "IO18" (empty for unnamed pins such as resistor ends)
    pin_type: str   # KiCad electrical type, e.g. "passive", "power_in", "input"

    def label(self) -> str:
        return f"{self.ref}.{self.pin}" + (f" ({self.function})" if self.function else "")


@dataclass
class Component:
    ref: str
    value: str
    footprint: str
    lib_id: str
    description: str
    nets: dict[str, str] = field(default_factory=dict)   # pin number -> net name


@dataclass
class Netlist:
    components: dict[str, Component]
    nets: dict[str, list[Node]]

    def net_of(self, ref: str, pin: str) -> str | None:
        return self.components[ref].nets.get(pin) if ref in self.components else None

    def other_end(self, ref: str, pin: str) -> str | None:
        """Net on the other pin of a two-pin part (resistor, capacitor, inductor...)."""
        component = self.components.get(ref)

        if not component or len(component.nets) != 2 or pin not in component.nets:
            return None

        return next(net for p, net in component.nets.items() if p != pin)


def parse(path: Path) -> Netlist:
    root = ET.parse(path).getroot()
    components = {}

    for comp in root.iter("comp"):
        lib = comp.find("libsource")
        components[comp.get("ref")] = Component(
                ref=comp.get("ref"),
                value=comp.findtext("value") or "",
                footprint=comp.findtext("footprint") or "",
                lib_id=f"{lib.get('lib')}:{lib.get('part')}" if lib is not None else "",
                description=comp.findtext("description") or "")

    nets = {}

    for net in root.iter("net"):
        name = net.get("name").lstrip("/")
        nodes = []

        for node in net.iter("node"):
            pin = node.get("pin")
            # pinfunction is "<pin name>_<pin number>" for symbols with several pins.
            function = (node.get("pinfunction") or "").removesuffix(f"_{pin}")
            nodes.append(Node(node.get("ref"), pin, function, node.get("pintype") or ""))

            if node.get("ref") in components:
                components[node.get("ref")].nets[pin] = name

        nets[name] = nodes

    return Netlist(components, nets)


_SI = {"p": 1e-12, "n": 1e-9, "u": 1e-6, "µ": 1e-6, "m": 1e-3, "k": 1e3, "K": 1e3, "M": 1e6, "R": 1.0}


def parse_value(text: str) -> float | None:
    """Numeric value of a part value such as '10k', '4k7', '330', '100nF', '1M' (None if unknown)."""
    match = re.match(r"^\s*(\d+)(?:([pnuµmkKMR])(\d*)|\.(\d+)([pnuµmkKMR]?))?", text)

    if not match:
        return None

    whole, prefix, decimals, dot_decimals, dot_prefix = match.groups()

    if prefix is not None:
        # "4k7" style: the prefix replaces the decimal point
        return float(f"{whole}.{decimals or 0}") * _SI[prefix]

    if dot_decimals is not None:
        return float(f"{whole}.{dot_decimals}") * _SI.get(dot_prefix or "R", 1.0)

    return float(whole)
