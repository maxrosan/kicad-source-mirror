"""MCP server exposing KiCad schematic editing as tools.

Configuration (environment variables):
    KICAD_CLI          path to kicad-cli.exe (default: this repository's install build)
    KICAD_SHARE        KiCad share/kicad directory with symbols/ and footprints/
    KICAD_API_SOCKET   attach to a running KiCad API server instead of starting a headless one
    KICAD_MCP_MODE     auto (default: the KiCad window if one runs, else headless), live, headless
"""

import functools
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

from . import firmware, review
from .library_search import LibraryIndex
from .pcb import BoardEditor
from .schematic import SchematicSession

_REPO = Path(__file__).resolve().parents[2]
_INSTALL = _REPO / "build" / "install" / "conda-release"

KICAD_CLI = Path(os.environ.get("KICAD_CLI", _INSTALL / "bin" / "kicad-cli.exe"))
KICAD_SHARE = Path(os.environ.get("KICAD_SHARE", _INSTALL / "share" / "kicad"))

mcp = MCPServer(
    "kicad",
    instructions=(
        "Builds KiCad schematics. Workflow: new_schematic (or open_schematic) -> search_symbols / "
        "symbol_pins to pick parts -> place_symbol for each part -> connect_pins to wire them with "
        "net labels -> mark_no_connect for unused pins -> run_erc and get_netlist to verify. "
        "Coordinates are in millimetres on an A4 sheet (about 297 x 210 mm, y grows downwards) "
        "and are snapped to the 1.27 mm grid. Keep parts inside x 15-280 mm, y 15-195 mm and "
        "out of the title block (x > 175 mm and y > 160 mm). Leave room between parts for net "
        "labels (about 2 mm per character) and fix any layout_warnings that place_symbol "
        "returns (remove_symbol, then place again). Power nets are ordinary labels (e.g. 'GND', '+3V3'); add a "
        "power:PWR_FLAG on nets that are only fed by a connector or battery to satisfy ERC. "
        "After ERC, run review_design and fix what it finds; write_pin_header gives firmware the "
        "pin map. Board: update_board_from_schematic -> auto_place -> move_footprints to refine "
        "(look at export_board_view) -> set_board_outline -> set_power_netclass -> "
        "add_ground_zone -> autoroute -> run_drc."),
)

_session = SchematicSession(KICAD_CLI, os.environ.get("KICAD_API_SOCKET"), KICAD_SHARE / "symbols",
                            os.environ.get("KICAD_MCP_MODE", "auto"))
# Freerouting 2.5 needs Java 25; prefer the JDK in this Python's conda env over the one on PATH.
_ENV_JAVA = Path(sys.prefix) / "Library" / "bin" / "java.exe"
_board = BoardEditor(_session,
                     Path(os.environ["FREEROUTING_JAR"]) if os.environ.get("FREEROUTING_JAR") else None,
                     os.environ.get("JAVA") or (str(_ENV_JAVA) if _ENV_JAVA.exists() else None))
_library = LibraryIndex(KICAD_SHARE)


def tool(function):
    """Register an MCP tool that runs inside the session's activity tracking (which reopens a
    project released while idle and releases it again after the call)."""
    @functools.wraps(function)
    def wrapper(*args, **kwargs):
        with _session.activity():
            return function(*args, **kwargs)

    return mcp.tool()(wrapper)


@tool
def kicad_status() -> dict:
    """KiCad version, mode (headless or live in the KiCad window) and the schematic being edited."""
    return {"version": _session.version(), "mode": "live" if _session.live else "headless",
            "schematic": str(_session.schematic_path) if _session.schematic_path else None}


@tool
def new_schematic(path: str) -> str:
    """Create a new KiCad project and schematic at `path` (a .kicad_sch file path) and open it."""
    return _session.new_schematic(path)


@tool
def open_schematic(path: str | None = None) -> str:
    """Open an existing .kicad_sch file for editing. In live mode, attaches to the schematic
    open in the KiCad window (path optional)."""
    return _session.open_schematic(path)


@tool
def search_symbols(query: str, limit: int = 20) -> list[dict]:
    """Search the KiCad symbol libraries. All words in `query` must match the name,
    description or keywords. Returns lib_ids such as 'RF_Module:ESP32-WROOM-32E'."""
    return [vars(e) for e in _library.search_symbols(query, limit)]


@tool
def search_footprints(query: str, limit: int = 20) -> list[dict]:
    """Search the KiCad footprint libraries (e.g. 'SOT-223', 'JST PH 2'). Returns ids such as
    'Package_TO_SOT_SMD:SOT-223-3_TabPin2' to use as a symbol's footprint."""
    return [vars(e) for e in _library.search_footprints(query, limit)]


@tool
def symbol_pins(lib_id: str) -> dict:
    """Pins (number, name, electrical type), default footprint and footprint filters of a
    library symbol, without placing it. Requires an open schematic."""
    return _session.library_symbol(lib_id)


@tool
def place_symbol(lib_id: str, reference: str, x_mm: float, y_mm: float, rotation: int = 0,
                 value: str | None = None, footprint: str | None = None) -> dict:
    """Place a library symbol. `reference` like 'U1', 'R3', 'C2'. `rotation` is 0/90/180/270.
    Returns the placed symbol with every pin's absolute position."""
    symbol = _session.place_symbol(lib_id, reference, x_mm, y_mm, rotation, value, footprint)
    result = symbol.summary(with_pins=True)
    result["layout_warnings"] = _session.layout_warnings(reference)
    return result


@tool
def update_symbol(reference: str, value: str | None = None, footprint: str | None = None) -> dict:
    """Change the value and/or footprint of a placed symbol."""
    return _session.update_symbol(reference, value, footprint).summary()


@tool
def remove_symbol(reference: str) -> dict:
    """Delete a placed symbol and the labels/no-connect markers on its pins."""
    return _session.remove_symbol(reference)


@tool
def list_symbols(with_pins: bool = False) -> list[dict]:
    """All placed symbols with value, footprint, position and pin extent (optionally pins)."""
    return [s.summary(with_pins) for s in _session.symbols().values()]


@tool
def connect_pins(connections: list[dict]) -> dict:
    """Connect pins to nets by placing a net label on each pin.

    `connections` is a list of {"ref": "U1", "pin": "<number or name>", "net": "<net name>"}.
    Pins that share a net name are connected. A pin given by name connects every pin with
    that name on the symbol (e.g. "GND"). Returns what was connected and any problems."""
    return _session.connect(connections)


@tool
def mark_no_connect(reference: str, pins: list[str]) -> dict:
    """Mark pins (numbers or names) of a symbol as intentionally unconnected."""
    return _session.mark_no_connect(reference, pins)


@tool
def save_schematic() -> str:
    """Save the schematic to disk."""
    return _session.save()


@tool
def run_erc() -> dict:
    """Save and run KiCad's electrical rules check. Returns counts per severity and violations."""
    return _session.run_erc()


@tool
def get_netlist() -> dict:
    """Save and export the netlist: net name -> list of 'REF.pin (pin name)'."""
    return _session.netlist()


@tool
def review_design() -> dict:
    """Design review beyond ERC: missing footprints, single-pin nets, decoupling, regulator
    capacitors, capacitor voltage ratings, and ESP32 pin rules (strapping pins, ADC2 with Wi-Fi,
    input-only GPIO34-39, flash pins, EN reset network). Also returns facts (regulators, voltage
    dividers with their ratio, pull-ups/downs) to reason about values. Judge the findings: they
    are heuristics, not proofs."""
    return review.review(_session.export_netlist())


@tool
def get_pin_map(mcu_reference: str | None = None) -> dict:
    """GPIO of the microcontroller (auto-detected if omitted) on each signal net, with what it
    connects to and any voltage divider it reads."""
    return firmware.pin_map(_session.export_netlist(), mcu_reference)


@tool
def write_pin_header(output_path: str, style: str = "arduino", mcu_reference: str | None = None) -> str:
    """Write a C header (pins.h) with a #define per GPIO named after its net, plus divider
    multipliers for analog inputs. `style` is 'arduino' (numbers) or 'esp-idf' (GPIO_NUM_x)."""
    if style not in ("arduino", "esp-idf"):
        raise ValueError("style must be 'arduino' or 'esp-idf'")

    pins = firmware.pin_map(_session.export_netlist(), mcu_reference)
    project = _session.schematic_path.stem if _session.schematic_path else ""
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(firmware.render_header(pins, style, project), encoding="utf-8")
    return str(path)


@tool
def update_board_from_schematic() -> dict:
    """Create the project's board if needed and bring footprints and nets from the schematic
    (like Tools > Update PCB from Schematic). Run again after schematic changes."""
    return _board.update_from_schematic()


@tool
def list_footprints() -> list[dict]:
    """Footprints on the board: position (origin, usually the centre), rotation, side, size, pads."""
    return [fp.summary() for fp in _board.footprints().values()]


@tool
def auto_place(origin_x_mm: float = 20, origin_y_mm: float = 20, max_width_mm: float = 80) -> dict:
    """Automatic first placement: small parts are grouped next to the IC they connect to
    (decoupling capacitors to their IC's power pins), groups are packed in rows. Refine with
    move_footprints: e.g. put an ESP32 module's antenna on the board edge, connectors on edges,
    and keep a switching regulator's input capacitor, diode and inductor tight around it."""
    return _board.auto_place(origin_x_mm, origin_y_mm, max_width_mm)


@tool
def move_footprints(moves: list[dict]) -> dict:
    """Move footprints: list of {"ref", "x_mm", "y_mm", "rotation" (degrees, optional),
    "side" ("top"/"bottom", optional)}. x/y is the footprint origin."""
    return _board.move_footprints(moves)


@tool
def set_board_outline(x_mm: float | None = None, y_mm: float | None = None, width_mm: float | None = None,
                      height_mm: float | None = None, margin_mm: float = 3.0,
                      corner_radius_mm: float = 1.0) -> dict:
    """Rectangular board outline (Edge.Cuts). Without x/y/width/height it is fitted around all
    footprints plus `margin_mm`."""
    if None in (x_mm, y_mm, width_mm, height_mm):
        return _board.fit_outline(margin_mm, corner_radius_mm)

    return _board.set_outline(x_mm, y_mm, width_mm, height_mm, corner_radius_mm)


@tool
def set_power_netclass(nets: list[str], track_width_mm: float = 1.0, clearance_mm: float = 0.25) -> dict:
    """Net class 'Power' with wider tracks for the given nets (names or patterns like '+*V').
    About 1 mm carries ~2.5 A on 1 oz outer copper; use it for battery, motor and regulator nets."""
    return _board.set_power_netclass(nets, track_width_mm, clearance_mm)


@tool
def set_fab_rules(preset: str = "jlcpcb") -> dict:
    """Set the board's minimum track, clearance, drill and edge clearance to a fabricator's
    2-layer capabilities (preset 'jlcpcb'). Run before autoroute and run_drc."""
    return _board.set_fab_rules(preset)


@tool
def add_ground_zone(net: str = "GND", layers: list[str] | None = None, clearance_mm: float = 0.3) -> dict:
    """Copper pour on `net` over the whole board on the given layers (default F.Cu and B.Cu)."""
    return _board.add_ground_zone(net, tuple(layers or ("F.Cu", "B.Cu")), clearance_mm)


@tool
def autoroute(passes: int = 30) -> dict:
    """Route all unrouted connections with Freerouting (needs FREEROUTING_JAR and Java), then
    refill zones. Check the result with run_drc and export_board_view."""
    return _board.autoroute(passes)


@tool
def run_drc() -> dict:
    """Board design rules check, including unrouted connections and schematic parity."""
    return _board.run_drc()


@tool
def export_board_view(kind: str = "2d", output_path: str | None = None) -> str:
    """Picture of the board to review it: '2d' (PDF of copper, silkscreen, courtyards, outline)
    or '3d' (PNG render of the top). Returns the file path; open it to look at the board."""
    return _board.export_view(kind, output_path)


@tool
def export_step(output_path: str | None = None) -> str:
    """STEP 3D model of the board for enclosure/mechanical design (e.g. in Blender or CAD)."""
    return _board.export_step(output_path)


@tool
def export_svg(output_dir: str | None = None) -> str:
    """Save and export the schematic as SVG (default: next to the schematic). Returns the path."""
    return _session.export_svg(output_dir)


def main() -> None:
    try:
        mcp.run()
    finally:
        _session.shutdown()


if __name__ == "__main__":
    main()
