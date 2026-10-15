"""High-level schematic editing on top of the KiCad IPC API.

Every connection is made with a local net label placed exactly on the pin's connection
point, instead of drawing wires between pins. Labels are reliable to place automatically
(no routing, no accidental crossings) and the result reads the same in KiCad.
"""

import atexit
import json
import os
import re
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import netlist as netlist_mod
from .client import ApiError, KiCadClient

from common.commands import base_commands_pb2 as bc  # noqa: E402  (path set up by .client)
from common.commands import editor_commands_pb2 as ed
from common.commands import library_commands_pb2 as lc
from common.commands import project_commands_pb2 as pc
from common.types import base_types_pb2 as bt
from common.types import enums_pb2 as en
from common.types import library_types_pb2 as lt
from google.protobuf import empty_pb2
from schematic import schematic_commands_pb2 as sc
from schematic import schematic_types_pb2 as st

NM_PER_MM = 1_000_000
GRID_NM = 1_270_000          # KiCad's default 50 mil schematic grid
LABEL_TEXT_NM = 1_270_000    # 1.27 mm, KiCad's default label text size
LAYOUT_MARGIN_NM = 5_080_000  # room kept around each symbol's pins for net labels
TITLE_BLOCK_NM = (175_000_000, 160_000_000)  # top-left corner of the A4 title block area

# Direction from a pin's connection point towards the symbol body, per pin orientation
# (schematic coordinates: +x right, +y down).
_BODY_DIRECTION = {
    st.SPO_RIGHT: (1, 0),
    st.SPO_LEFT: (-1, 0),
    st.SPO_UP: (0, -1),
    st.SPO_DOWN: (0, 1),
}

# Label spin that makes the label text extend in a given direction away from the pin.
_SPIN_FOR_DIRECTION = {
    (1, 0): st.SLSS_RIGHT,
    (-1, 0): st.SLSS_LEFT,
    (0, -1): st.SLSS_UP,
    (0, 1): st.SLSS_BOTTOM,
}

_ORIENTATIONS = {0: st.SSO_0, 90: st.SSO_90, 180: st.SSO_180, 270: st.SSO_270}


def snap(value_mm: float) -> int:
    """Convert millimetres to nanometres, snapped to the schematic grid."""
    return round(value_mm * NM_PER_MM / GRID_NM) * GRID_NM


def to_mm(value_nm: int) -> float:
    return round(value_nm / NM_PER_MM, 4)


def _rotate(x: int, y: int, orientation: int, mirror_x: bool, mirror_y: bool) -> tuple[int, int]:
    """Apply a symbol transform to a point in symbol-local coordinates (matches KiCad's TRANSFORM)."""
    if orientation == st.SSO_90:
        x, y = y, -x
    elif orientation == st.SSO_180:
        x, y = -x, -y
    elif orientation == st.SSO_270:
        x, y = -y, x

    if mirror_x:
        y = -y

    if mirror_y:
        x = -x

    return x, y


@dataclass
class PlacedPin:
    """A pin of a placed symbol, in absolute schematic coordinates."""

    number: str
    name: str
    electrical_type: str
    x: int
    y: int
    away: tuple[int, int]   # unit vector pointing out of the symbol body

    def as_dict(self) -> dict:
        return {"number": self.number, "name": self.name, "type": self.electrical_type,
                "x_mm": to_mm(self.x), "y_mm": to_mm(self.y)}


@dataclass
class PlacedSymbol:
    reference: str
    value: str
    footprint: str
    lib_id: str
    x: int
    y: int
    rotation: int
    instance: st.SchematicSymbolInstance
    pins: list[PlacedPin] = field(default_factory=list)

    def summary(self, with_pins: bool = False) -> dict:
        result = {"reference": self.reference, "value": self.value, "footprint": self.footprint,
                  "lib_id": self.lib_id, "x_mm": to_mm(self.x), "y_mm": to_mm(self.y),
                  "rotation": self.rotation}

        if self.pins:
            xs = [p.x for p in self.pins]
            ys = [p.y for p in self.pins]
            result["pin_extent_mm"] = {"x_min": to_mm(min(xs)), "x_max": to_mm(max(xs)),
                                       "y_min": to_mm(min(ys)), "y_max": to_mm(max(ys))}

        if with_pins:
            result["pins"] = [p.as_dict() for p in self.pins]

        return result

    def box(self, margin: int = 0) -> tuple[int, int, int, int]:
        """Bounding box of the pins (x0, y0, x1, y1), grown by `margin`."""
        xs = [p.x for p in self.pins] or [self.x]
        ys = [p.y for p in self.pins] or [self.y]
        return min(xs) - margin, min(ys) - margin, max(xs) + margin, max(ys) + margin

    def find_pins(self, pin: str) -> list[PlacedPin]:
        """Pins matching a pin number exactly, or else all pins with that name (case-insensitive)."""
        by_number = [p for p in self.pins if p.number == pin]

        if by_number:
            return by_number

        return [p for p in self.pins if p.name.lower() == pin.lower()]


def _unpack_pins(instance: st.SchematicSymbolInstance) -> list[PlacedPin]:
    orientation = instance.transform.orientation or st.SSO_0
    mirror_x = instance.transform.mirror_x
    mirror_y = instance.transform.mirror_y
    unit = instance.unit.unit or 1
    pins = []

    for child in instance.definition.items:
        if not child.item.Is(st.SchematicPin.DESCRIPTOR):
            continue

        # Pins of other units of a multi-unit symbol are not part of this instance.
        if child.HasField("unit") and child.unit.unit not in (0, unit):
            continue

        pin = st.SchematicPin()
        child.item.Unpack(pin)

        dx, dy = _rotate(pin.position.x_nm, pin.position.y_nm, orientation, mirror_x, mirror_y)
        bx, by = _BODY_DIRECTION.get(pin.orientation, (1, 0))
        bx, by = _rotate(bx, by, orientation, mirror_x, mirror_y)

        pins.append(PlacedPin(number=pin.number, name=pin.name,
                              electrical_type=bt.ElectricalPinType.Name(pin.electrical_type)
                              .removeprefix("EPT_"),
                              x=instance.position.x_nm + dx, y=instance.position.y_nm + dy,
                              away=(-bx, -by)))

    return pins


def _unpack_symbol(instance: st.SchematicSymbolInstance) -> PlacedSymbol:
    rotation = {v: k for k, v in _ORIENTATIONS.items()}.get(instance.transform.orientation, 0)
    lib = instance.lib_id if instance.lib_id.entry_name else instance.definition.id

    return PlacedSymbol(reference=instance.reference_field.text.text,
                        value=instance.value_field.text.text,
                        footprint=instance.footprint_field.text.text,
                        lib_id=f"{lib.library_nickname}:{lib.entry_name}",
                        x=instance.position.x_nm, y=instance.position.y_nm, rotation=rotation,
                        instance=instance, pins=_unpack_pins(instance))


class SchematicSession:
    """Owns a headless `kicad-cli api-server` and the schematic it is editing.

    Args:
        kicad_cli: path to kicad-cli.exe of the KiCad build to use.
        socket_path: attach to an already running KiCad API server instead of starting one.
    """

    def __init__(self, kicad_cli: Path, socket_path: str | None = None,
                 symbols_dir: Path | None = None):
        self.kicad_cli = kicad_cli
        self.symbols_dir = symbols_dir
        self._process: subprocess.Popen | None = None
        self._client: KiCadClient | None = None
        self._socket_path = socket_path
        self._attached = socket_path is not None
        self._libraries_loaded = False
        self.document: bt.DocumentSpecifier | None = None
        self.schematic_path: Path | None = None
        # The project's board, once opened by pcb.BoardEditor
        self.board_document: bt.DocumentSpecifier | None = None

    # ------------------------------------------------------------------ connection

    def client(self) -> KiCadClient:
        if self._client is None:
            if self._socket_path is None:
                self._start_server()

            self._client = self._connect(self._socket_path)

        return self._client

    def _start_server(self) -> None:
        sock_dir = Path(tempfile.gettempdir()) / "kicad-mcp"
        sock_dir.mkdir(parents=True, exist_ok=True)
        self._socket_path = str(sock_dir / f"api-{os.getpid()}.sock")

        self._process = subprocess.Popen(
                [str(self.kicad_cli), "api-server", "--socket", self._socket_path],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        atexit.register(self.shutdown)

    @staticmethod
    def _connect(socket_path: str) -> KiCadClient:
        deadline = time.time() + 60
        last_error: Exception | None = None

        while time.time() < deadline:
            try:
                client = KiCadClient(socket_path)
                client.send(bc.GetVersion(), bc.GetVersionResponse)
                return client
            except Exception as error:  # server still starting
                last_error = error
                time.sleep(0.5)

        raise RuntimeError(f"could not reach the KiCad API server at {socket_path}: {last_error}")

    def shutdown(self) -> None:
        if self._client is not None:
            # Closing releases the project lock file, so KiCad can open the project afterwards.
            if self._process is not None:
                try:
                    self._client.send(pc.CloseAllDocuments(force=True), empty_pb2.Empty)
                except Exception:
                    pass

            self._client.close()
            self._client = None

        if self._process is not None and self._process.poll() is None:
            self._process.terminate()

            try:
                self._process.wait(timeout=10)
            except subprocess.TimeoutExpired:
                self._process.kill()

        self._process = None

    def version(self) -> str:
        return self.client().send(bc.GetVersion(), bc.GetVersionResponse).version.full_version

    def load_symbol_libraries(self) -> None:
        """The headless server loads libraries on request and asynchronously."""
        if self._libraries_loaded:
            return

        client = self.client()

        try:
            client.send(lc.LoadAllLibraries(type=[lt.LT_SYMBOL]), lt.LibraryCommandStatus)
        except ApiError:
            # The GUI preloads libraries itself and does not offer LoadAllLibraries
            self._libraries_loaded = True
            return

        deadline = time.time() + 120

        while time.time() < deadline:
            status = client.send(lc.GetLibraryStatuses(types=[lt.LT_SYMBOL], scope=lt.LTS_BOTH),
                                 lc.LibraryStatusResponse)

            if all(entry.status not in (lt.LLS_LOADING, lt.LLS_UNKNOWN) for entry in status.libraries):
                self._libraries_loaded = True
                return

            time.sleep(0.5)

        raise RuntimeError("timed out loading the symbol libraries")

    # ------------------------------------------------------------------ documents

    def _require_document(self) -> bt.DocumentSpecifier:
        if self.document is None:
            raise RuntimeError("no schematic is open; call new_schematic or open_schematic first")

        return self.document

    def _close_all(self) -> None:
        self.client().send(pc.CloseAllDocuments(force=True), empty_pb2.Empty)
        self.document = None
        self.schematic_path = None
        self.board_document = None
        # Closing the project unloads its library adapters.
        self._libraries_loaded = False

    @property
    def live(self) -> bool:
        """True when attached to a KiCad window instead of a private headless server."""
        return self._attached

    def open_documents(self, doc_type: int) -> list:
        response = self.client().send(ed.GetOpenDocuments(type=doc_type), ed.GetOpenDocumentsResponse)
        return list(response.documents)

    def _attach_open_schematic(self, path: str | None) -> str:
        """Live mode: use the schematic open in the KiCad window (documents cannot be opened
        or created through the API while KiCad runs with its GUI)."""
        documents = self.open_documents(bt.DOCTYPE_SCHEMATIC)

        if not documents:
            raise RuntimeError("no schematic is open in KiCad; open it in the Schematic Editor first")

        document = documents[0]
        sch = Path(document.project.path) / f"{document.project.name}.kicad_sch"

        if path and Path(path).with_suffix(".kicad_sch").resolve() != sch.resolve():
            raise RuntimeError(f"KiCad has {sch} open, not {path}; open the right project in KiCad")

        self.document = document
        self.schematic_path = sch
        return str(sch)

    def new_schematic(self, path: str) -> str:
        if self.live:
            raise RuntimeError("in live mode create the project in KiCad, then call open_schematic")

        sch = Path(path).with_suffix(".kicad_sch").resolve()

        if sch.exists():
            raise RuntimeError(f"{sch} already exists; use open_schematic to edit it")

        sch.parent.mkdir(parents=True, exist_ok=True)
        self._close_all()
        response = self.client().send(pc.CreateDocument(type=bt.DOCTYPE_SCHEMATIC, path=str(sch)),
                                      pc.OpenDocumentResponse)
        self.document = response.document
        self.schematic_path = sch
        self.load_symbol_libraries()
        self.save()
        return str(sch)

    def open_schematic(self, path: str | None = None) -> str:
        if self.live:
            return self._attach_open_schematic(path)

        if not path:
            raise RuntimeError("path is required")

        sch = Path(path).with_suffix(".kicad_sch").resolve()

        if not sch.exists():
            raise RuntimeError(f"{sch} does not exist")

        self._close_all()
        response = self.client().send(pc.OpenDocument(type=bt.DOCTYPE_SCHEMATIC, path=str(sch)),
                                      pc.OpenDocumentResponse)
        self.document = response.document
        self.schematic_path = sch
        self.load_symbol_libraries()
        return str(sch)

    def save(self) -> str:
        self.client().send(pc.SaveDocument(document=self._require_document()), empty_pb2.Empty)
        return str(self.schematic_path)

    # ------------------------------------------------------------------ items

    def _header(self, message) -> None:
        message.header.document.CopyFrom(self._require_document())

    def _get_items(self, *types: int) -> list:
        request = ed.GetItems()
        self._header(request)
        request.types.extend(types)
        return list(self.client().send(request, ed.GetItemsResponse).items)

    def symbols(self) -> dict[str, PlacedSymbol]:
        result = {}

        for packed in self._get_items(en.KOT_SCH_SYMBOL):
            instance = st.SchematicSymbolInstance()
            packed.Unpack(instance)
            symbol = _unpack_symbol(instance)
            result[symbol.reference] = symbol

        return result

    def symbol(self, reference: str) -> PlacedSymbol:
        symbols = self.symbols()

        if reference not in symbols:
            raise RuntimeError(f"no symbol with reference {reference}; existing: {sorted(symbols)}")

        return symbols[reference]

    def _labels_by_position(self) -> dict[tuple[int, int], tuple[str, str]]:
        """Existing local labels, keyed by position, as (text, item id)."""
        labels = {}

        for packed in self._get_items(en.KOT_SCH_LABEL):
            label = st.LocalLabel()
            packed.Unpack(label)
            labels[(label.position.x_nm, label.position.y_nm)] = (label.text.text, label.id.value)

        return labels

    def _no_connects_by_position(self) -> dict[tuple[int, int], str]:
        markers = {}

        for packed in self._get_items(en.KOT_SCH_NO_CONNECT):
            marker = st.NoConnectMarker()
            packed.Unpack(marker)
            markers[(marker.position.x_nm, marker.position.y_nm)] = marker.id.value

        return markers

    def _create(self, items: list) -> list[str]:
        request = ed.CreateItems()
        self._header(request)

        for item in items:
            request.items.add().Pack(item)

        response = self.client().send(request, ed.CreateItemsResponse)
        errors = [r.status.error_message or ed.ItemStatusCode.Name(r.status.code)
                  for r in response.created_items if r.status.code != ed.ISC_OK]

        if errors:
            raise RuntimeError("; ".join(errors))

        return [r.item.TypeName() for r in response.created_items]

    def _fetch_library_symbol(self, library: str, entry: str) -> st.SchematicSymbol:
        request = lc.GetItemsFromLibrary(type=lt.LT_SYMBOL)
        request.document.CopyFrom(self._require_document())
        request.item_ids.add(library_nickname=library, entry_name=entry)
        response = self.client().send(request, ed.GetItemsResponse)

        if not response.items:
            raise RuntimeError(f"symbol {library}:{entry} not found")

        symbol = st.SchematicSymbol()
        response.items[0].Unpack(symbol)
        return symbol

    def _parent_symbol(self, library: str, entry: str) -> str | None:
        """Name of the symbol that `entry` extends, read from the stock library file."""
        if self.symbols_dir is None:
            return None

        sym_file = self.symbols_dir / f"{library}.kicad_symdir" / f"{entry}.kicad_sym"

        if not sym_file.exists():
            return None

        match = re.search(r'\(extends\s+"((?:[^"\\]|\\.)*)"', sym_file.read_text(encoding="utf-8"))
        return match.group(1) if match else None

    def library_symbol(self, lib_id: str) -> dict:
        """Pins, units and default fields of a library symbol, without placing it."""
        if ":" not in lib_id:
            raise RuntimeError("lib_id must be 'Library:Symbol', e.g. 'Device:R'")

        library, entry = lib_id.split(":", 1)
        symbol = self._fetch_library_symbol(library, entry)
        pin_source = symbol

        # Derived symbols ("extends") carry only their own fields; the pins live in the parent.
        parent = self._parent_symbol(library, entry)

        while parent and not any(c.item.Is(st.SchematicPin.DESCRIPTOR) for c in pin_source.items):
            pin_source = self._fetch_library_symbol(library, parent)
            parent = self._parent_symbol(library, parent)

        pins = []

        for child in pin_source.items:
            if child.item.Is(st.SchematicPin.DESCRIPTOR):
                pin = st.SchematicPin()
                child.item.Unpack(pin)
                pins.append({"number": pin.number, "name": pin.name,
                             "type": bt.ElectricalPinType.Name(pin.electrical_type).removeprefix("EPT_"),
                             "unit": child.unit.unit if child.HasField("unit") else 0,
                             "x_mm": to_mm(pin.position.x_nm), "y_mm": to_mm(pin.position.y_nm)})

        return {"lib_id": lib_id, "description": symbol.description_field.text.text,
                "default_footprint": symbol.footprint_field.text.text,
                "footprint_filters": list(symbol.footprint_filters),
                "unit_count": symbol.unit_count, "pins": pins,
                "note": "pin x/y are relative to the symbol anchor at rotation 0"}

    def place_symbol(self, lib_id: str, reference: str, x_mm: float, y_mm: float, rotation: int = 0,
                     value: str | None = None, footprint: str | None = None) -> PlacedSymbol:
        if ":" not in lib_id:
            raise RuntimeError("lib_id must be 'Library:Symbol', e.g. 'Device:R'")

        if rotation not in _ORIENTATIONS:
            raise RuntimeError("rotation must be 0, 90, 180 or 270")

        if reference in self.symbols():
            raise RuntimeError(f"reference {reference} is already used")

        library, entry = lib_id.split(":", 1)
        request = sc.PlaceSymbolFromLibrary()
        self._header(request)
        request.lib_id.library_nickname = library
        request.lib_id.entry_name = entry
        request.position.x_nm = snap(x_mm)
        request.position.y_nm = snap(y_mm)
        request.orientation = _ORIENTATIONS[rotation]
        request.reference = reference

        self.client().send(request, lc.PlaceFromLibraryResponse)

        if value is not None or footprint is not None:
            return self.update_symbol(reference, value, footprint)

        return self.symbol(reference)

    def layout_warnings(self, reference: str) -> list[str]:
        """Overlaps of a symbol (plus room for its labels) with other symbols or the title block."""
        symbols = self.symbols()
        target = symbols[reference]
        margin = LAYOUT_MARGIN_NM
        x0, y0, x1, y1 = target.box(margin)
        warnings = []

        for other in symbols.values():
            if other.reference == reference or other.reference.startswith("#"):
                continue

            a0, b0, a1, b1 = other.box(margin)

            if x0 < a1 and a0 < x1 and y0 < b1 and b0 < y1:
                warnings.append(f"{reference} is too close to {other.reference} "
                                f"(at {to_mm(other.x)}, {to_mm(other.y)} mm); labels may overlap")

        tx0, ty0 = TITLE_BLOCK_NM

        if x1 > tx0 and y1 > ty0:
            warnings.append(f"{reference} overlaps the title block area "
                            f"(x > {to_mm(tx0)} mm and y > {to_mm(ty0)} mm)")

        return warnings

    def update_symbol(self, reference: str, value: str | None = None,
                      footprint: str | None = None) -> PlacedSymbol:
        symbol = self.symbol(reference)
        instance = symbol.instance

        if value is not None:
            instance.value_field.text.text = value

        if footprint is not None:
            instance.footprint_field.text.text = footprint

        # Send the update without the library definition: KiCad then keeps the existing library
        # link. With the definition, a second instance of the same part gets a renamed embedded
        # copy (e.g. "C_1") detached from its library, which ERC reports as a warning.
        instance.ClearField("definition")

        update = ed.UpdateItems()
        self._header(update)
        update.items.add().Pack(instance)
        self.client().send(update, ed.UpdateItemsResponse)
        return self.symbol(reference)

    def connect(self, connections: list[dict]) -> dict:
        """Put a net label on each requested pin.

        Each connection is {"ref": "U1", "pin": "VDD" or "2", "net": "3V3"}. A pin given by
        name connects every pin of the symbol with that name (e.g. all GND pins).
        """
        symbols = self.symbols()
        existing = self._labels_by_position()
        labels = []
        done = []
        problems = []

        for connection in connections:
            ref, pin, net = connection["ref"], str(connection["pin"]), connection["net"]

            if ref not in symbols:
                problems.append(f"{ref}: no such symbol")
                continue

            pins = symbols[ref].find_pins(pin)

            if not pins:
                names = sorted({f"{p.number}/{p.name}" for p in symbols[ref].pins})
                problems.append(f"{ref}.{pin}: no such pin (pins: {', '.join(names)})")
                continue

            for target in pins:
                key = (target.x, target.y)

                if key in existing:
                    if existing[key][0] != net:
                        problems.append(f"{ref}.{target.number} ({target.name}) is already on net "
                                        f"{existing[key][0]}, not connected to {net}")
                    continue

                label = st.LocalLabel()
                label.position.x_nm, label.position.y_nm = target.x, target.y
                label.text.text = net
                label.text.position.x_nm, label.text.position.y_nm = target.x, target.y
                label.text.attributes.size.x_nm = LABEL_TEXT_NM
                label.text.attributes.size.y_nm = LABEL_TEXT_NM
                label.spin_style = _SPIN_FOR_DIRECTION.get(target.away, st.SLSS_RIGHT)
                labels.append(label)
                existing[key] = (net, "")
                done.append(f"{ref}.{target.number} ({target.name}) -> {net}")

        if labels:
            self._create(labels)

        return {"connected": done, "problems": problems}

    def mark_no_connect(self, reference: str, pins: list[str]) -> dict:
        symbol = self.symbol(reference)
        existing = self._no_connects_by_position()
        labels = self._labels_by_position()
        markers = []
        done = []
        problems = []

        for pin in pins:
            matches = symbol.find_pins(str(pin))

            if not matches:
                problems.append(f"{reference}.{pin}: no such pin")
                continue

            for target in matches:
                key = (target.x, target.y)

                if key in labels:
                    problems.append(f"{reference}.{target.number} is connected to {labels[key][0]}")
                    continue

                if key in existing:
                    continue

                marker = st.NoConnectMarker()
                marker.position.x_nm, marker.position.y_nm = target.x, target.y
                markers.append(marker)
                done.append(f"{reference}.{target.number} ({target.name})")

        if markers:
            self._create(markers)

        return {"marked": done, "problems": problems}

    def remove_symbol(self, reference: str) -> dict:
        """Delete a symbol together with the labels and no-connect markers on its pins."""
        symbol = self.symbol(reference)
        positions = {(p.x, p.y) for p in symbol.pins}
        ids = [symbol.instance.id.value]
        ids += [lid for pos, (_, lid) in self._labels_by_position().items() if pos in positions]
        ids += [mid for pos, mid in self._no_connects_by_position().items() if pos in positions]

        request = ed.DeleteItems()
        self._header(request)

        for item_id in ids:
            request.item_ids.add(value=item_id)

        self.client().send(request, ed.DeleteItemsResponse)
        return {"removed": reference, "items_deleted": len(ids)}

    # ------------------------------------------------------------------ checks (kicad-cli)

    def _run_cli(self, *args: str) -> subprocess.CompletedProcess:
        return subprocess.run([str(self.kicad_cli), *args], capture_output=True, text=True,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    def run_erc(self) -> dict:
        self.save()

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "erc.json"
            result = self._run_cli("sch", "erc", "--format", "json", "--severity-all",
                                   "-o", str(report), str(self.schematic_path))

            if not report.exists():
                raise RuntimeError(f"kicad-cli sch erc failed: {result.stderr or result.stdout}")

            data = json.loads(report.read_text(encoding="utf-8"))

        violations = []

        for sheet in data.get("sheets", []):
            for violation in sheet.get("violations", []):
                violations.append({
                    "severity": violation.get("severity"),
                    "type": violation.get("type"),
                    "description": violation.get("description"),
                    "items": [i.get("description") for i in violation.get("items", [])],
                })

        counts: dict[str, int] = {}

        for violation in violations:
            counts[violation["severity"]] = counts.get(violation["severity"], 0) + 1

        return {"counts": counts, "violations": violations}

    def export_netlist(self, keep_as: Path | None = None) -> netlist_mod.Netlist:
        """Save, export the netlist with kicad-cli and parse it (optionally keeping the file)."""
        self.save()

        with tempfile.TemporaryDirectory() as tmp:
            out = keep_as or Path(tmp) / "netlist.xml"
            result = self._run_cli("sch", "export", "netlist", "--format", "kicadxml",
                                   "-o", str(out), str(self.schematic_path))

            if not out.exists():
                raise RuntimeError(f"kicad-cli netlist export failed: {result.stderr or result.stdout}")

            return netlist_mod.parse(out)

    def netlist(self) -> dict:
        return {name: [node.label() for node in nodes]
                for name, nodes in self.export_netlist().nets.items()}

    def export_svg(self, output_dir: str | None = None) -> str:
        self.save()
        out_dir = Path(output_dir) if output_dir else self.schematic_path.parent
        out_dir.mkdir(parents=True, exist_ok=True)
        result = self._run_cli("sch", "export", "svg", "-o", str(out_dir), str(self.schematic_path))

        if result.returncode != 0:
            raise RuntimeError(f"kicad-cli svg export failed: {result.stderr or result.stdout}")

        return str(out_dir / f"{self.schematic_path.stem}.svg")


__all__ = ["ApiError", "SchematicSession", "snap", "to_mm"]
