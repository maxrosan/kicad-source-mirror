"""Board (PCB) editing on top of the KiCad IPC API.

The board lives in the same project as the schematic opened by SchematicSession and is
edited through the same API server. Netlist transfer, zone filling and Specctra session
import go through the API; DRC, Specctra DSN export and images go through kicad-cli.
"""

import json
import math
import shutil
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

from .review import is_ground
from .schematic import NM_PER_MM, SchematicSession, to_mm

from board import board_commands_pb2 as bc  # noqa: E402  (path set up by .client)
from board import board_types_pb2 as bty
from common.commands import editor_commands_pb2 as ed
from common.commands import library_commands_pb2 as lc
from common.commands import project_commands_pb2 as pc
from common.types import base_types_pb2 as bt
from common.types import enums_pb2 as en
from common.types import library_types_pb2 as lt
from common.types import project_settings_pb2 as ps
from google.protobuf import descriptor_pool, empty_pb2, message_factory

def _nm(value_mm: float) -> int:
    return round(value_mm * NM_PER_MM)


# Fields holding absolute board coordinates, and angles that turn with their parent, inside a
# footprint message. Sizes and offsets are local and must not be transformed.
_POINT_FIELDS = {"position", "start", "end", "mid", "center", "radius_point", "control1",
                 "control2", "top_left", "bottom_right", "point"}
_ANGLE_FIELDS = {"angle", "rotation"}


def _message_class(type_name: str):
    descriptor = descriptor_pool.Default().FindMessageTypeByName(type_name)
    return message_factory.GetMessageClass(descriptor)


def _transform(message, point_fn, delta_degrees: float) -> None:
    """Apply a rigid transform to every absolute point (and turning angle) in `message`."""
    for field, value in message.ListFields():
        if field.message_type is None:
            continue

        values = value if getattr(field, "is_repeated", False) else [value]

        for item in values:
            type_name = field.message_type.full_name

            if type_name == "kiapi.common.types.Vector2" and field.name in _POINT_FIELDS:
                item.x_nm, item.y_nm = point_fn(item.x_nm, item.y_nm)
            elif type_name == "kiapi.common.types.Angle" and field.name in _ANGLE_FIELDS:
                item.value_degrees = (item.value_degrees + delta_degrees) % 360
            elif type_name == "google.protobuf.Any":
                inner = _message_class(item.TypeName())()
                item.Unpack(inner)
                _transform(inner, point_fn, delta_degrees)
                item.Pack(inner)
            else:
                _transform(item, point_fn, delta_degrees)

        if field.message_type.full_name == "kiapi.common.types.GraphicRectangleAttributes":
            # A rotated rectangle keeps its corners, but they must stay top-left/bottom-right.
            rect = value
            xs = sorted((rect.top_left.x_nm, rect.bottom_right.x_nm))
            ys = sorted((rect.top_left.y_nm, rect.bottom_right.y_nm))
            rect.top_left.x_nm, rect.bottom_right.x_nm = xs
            rect.top_left.y_nm, rect.bottom_right.y_nm = ys


def move_footprint_instance(instance: bty.FootprintInstance, x_nm: int, y_nm: int,
                            rotation_degrees: float | None) -> None:
    """Move/rotate a footprint and everything inside it.

    The API serializes a footprint's pads, graphics and fields in absolute board coordinates,
    so changing only the footprint position would leave its pads behind.
    """
    old_x, old_y = instance.position.x_nm, instance.position.y_nm
    old_angle = instance.orientation.value_degrees
    new_angle = old_angle if rotation_degrees is None else rotation_degrees % 360
    delta = new_angle - old_angle
    rad = math.radians(delta)
    cos_a, sin_a = math.cos(rad), math.sin(rad)

    def point_fn(px: int, py: int) -> tuple[int, int]:
        # KiCad angles are counter-clockwise on screen, and screen y grows downwards.
        dx, dy = px - old_x, py - old_y
        return (round(x_nm + dx * cos_a + dy * sin_a), round(y_nm - dx * sin_a + dy * cos_a))

    for field_name in ("definition", "reference_field", "value_field", "datasheet_field",
                       "description_field"):
        if instance.HasField(field_name):
            _transform(getattr(instance, field_name), point_fn, delta)

    instance.position.x_nm, instance.position.y_nm = x_nm, y_nm
    instance.orientation.value_degrees = new_angle


@dataclass
class PlacedFootprint:
    reference: str
    value: str
    lib_id: str
    instance: bty.FootprintInstance
    # Bounding box of the footprint itself (pads, courtyard, graphics; no fields).
    box_x: int
    box_y: int
    box_w: int
    box_h: int
    pad_count: int

    @property
    def side(self) -> str:
        return "bottom" if self.instance.layer == bty.BL_B_Cu else "top"

    def summary(self) -> dict:
        return {"reference": self.reference, "value": self.value, "footprint": self.lib_id,
                "x_mm": to_mm(self.instance.position.x_nm), "y_mm": to_mm(self.instance.position.y_nm),
                "rotation": round(self.instance.orientation.value_degrees, 2), "side": self.side,
                "size_mm": [to_mm(self.box_w), to_mm(self.box_h)], "pads": self.pad_count}


class BoardEditor:
    """Creates and edits the .kicad_pcb that belongs to the open schematic's project."""

    def __init__(self, session: SchematicSession, freerouting_jar: Path | None = None,
                 java: str | None = None):
        self.session = session
        self.freerouting_jar = freerouting_jar
        self.java = java or shutil.which("java")

    # ------------------------------------------------------------------ document

    @property
    def board_path(self) -> Path:
        if self.session.schematic_path is None:
            raise RuntimeError("no schematic is open; call new_schematic or open_schematic first")

        return self.session.schematic_path.with_suffix(".kicad_pcb")

    def _client(self):
        return self.session.client()

    def _board(self) -> bt.DocumentSpecifier:
        if self.session.board_document is None:
            raise RuntimeError("no board is open; call create_board first")

        return self.session.board_document

    def _load_footprint_libraries(self) -> None:
        self.session.load_libraries(lt.LT_FOOTPRINT)

    def open_or_create(self) -> str:
        """Open the project's board, creating an empty one if it does not exist yet."""
        self.session._require_document()
        path = self.board_path

        if self.session.board_document is None and self.session.live:
            boards = self.session.open_documents(bt.DOCTYPE_PCB)

            if not boards:
                raise RuntimeError("no board is open in KiCad; open (or create) it in the PCB Editor first")

            self.session.board_document = boards[0]
            return str(path)

        if self.session.board_document is None:
            self._load_footprint_libraries()

            if path.exists():
                response = self._client().send(pc.OpenDocument(type=bt.DOCTYPE_PCB, path=str(path)),
                                               pc.OpenDocumentResponse)
            else:
                response = self._client().send(pc.CreateDocument(type=bt.DOCTYPE_PCB, path=str(path)),
                                               pc.OpenDocumentResponse)

            self.session.board_document = response.document
            self.save()

        return str(path)

    def save(self) -> str:
        self._client().send(pc.SaveDocument(document=self._board()), empty_pb2.Empty)
        return str(self.board_path)

    # ------------------------------------------------------------------ netlist

    def update_from_schematic(self) -> dict:
        """Bring footprints and nets from the schematic (like Tools > Update PCB from Schematic)."""
        self.open_or_create()
        self.session.save()

        with tempfile.TemporaryDirectory() as tmp:
            netlist = Path(tmp) / "board.net"
            result = self.session._run_cli("sch", "export", "netlist", "--format", "kicadsexpr",
                                           "-o", str(netlist), str(self.session.schematic_path))

            if not netlist.exists():
                raise RuntimeError(f"netlist export failed: {result.stderr or result.stdout}")

            response = self._client().send(
                    bc.ImportNetlist(board=self._board(), netlist_path=str(netlist),
                                     match_mode=bc.NMM_UUID, update_footprints=True,
                                     delete_extra_footprints=True), bc.ImportNetlistResponse)

        self.save()
        # Only the lines that are problems; the full report lists every routine change.
        issues = [line for line in response.report.splitlines()
                  if line.lower().startswith(("error", "warning")) or "not found" in line.lower()]
        return {"errors": response.error_count, "warnings": response.warning_count,
                "new_footprints": response.new_footprint_count, "issues": issues}

    # ------------------------------------------------------------------ footprints

    def _get_items(self, *types: int) -> list:
        request = ed.GetItems()
        request.header.document.CopyFrom(self._board())
        request.types.extend(types)
        return list(self._client().send(request, ed.GetItemsResponse).items)

    def footprints(self) -> dict[str, PlacedFootprint]:
        instances = []

        for packed in self._get_items(en.KOT_PCB_FOOTPRINT):
            instance = bty.FootprintInstance()
            packed.Unpack(instance)
            instances.append(instance)

        boxes = {}

        if instances:
            request = ed.GetBoundingBox(mode=ed.BBM_ITEM_ONLY)
            request.header.document.CopyFrom(self._board())

            for instance in instances:
                request.items.add(value=instance.id.value)

            response = self._client().send(request, ed.GetBoundingBoxResponse)
            boxes = {item.value: box for item, box in zip(response.items, response.boxes)}

        result = {}

        for instance in instances:
            box = boxes.get(instance.id.value)
            lib = instance.definition.id
            pads = sum(1 for item in instance.definition.items if item.Is(bty.Pad.DESCRIPTOR))
            ref = instance.reference_field.text.text.text
            result[ref] = PlacedFootprint(
                    reference=ref, value=instance.value_field.text.text.text,
                    lib_id=f"{lib.library_nickname}:{lib.entry_name}", instance=instance,
                    box_x=box.position.x_nm if box else instance.position.x_nm,
                    box_y=box.position.y_nm if box else instance.position.y_nm,
                    box_w=box.size.x_nm if box else 0, box_h=box.size.y_nm if box else 0,
                    pad_count=pads)

        return result

    def move_footprints(self, moves: list[dict]) -> dict:
        """Move/rotate/flip footprints. Each move: {"ref", "x_mm", "y_mm", "rotation"?, "side"?}.

        x/y set the footprint origin (usually its centre)."""
        footprints = self.footprints()
        updates = []
        flips = []
        problems = []

        for move in moves:
            ref = move["ref"]

            if ref not in footprints:
                problems.append(f"{ref}: no such footprint")
                continue

            footprint = footprints[ref]
            instance = footprint.instance
            rotation = move.get("rotation")
            move_footprint_instance(instance, _nm(move["x_mm"]), _nm(move["y_mm"]),
                                    None if rotation is None else float(rotation))
            updates.append(instance)

            if move.get("side") and move["side"] != footprint.side:
                flips.append(instance.id.value)

        self._update(updates)

        if flips:
            request = bc.FlipItems()
            request.header.document.CopyFrom(self._board())

            for item_id in flips:
                request.items.add(value=item_id)

            self._client().send(request, bc.FlipItemsResponse)

        self.save()
        return {"moved": [m["ref"] for m in moves if m["ref"] in footprints], "problems": problems}

    def _update(self, instances: list) -> None:
        if not instances:
            return

        request = ed.UpdateItems()
        request.header.document.CopyFrom(self._board())

        for instance in instances:
            request.items.add().Pack(instance)

        response = self._client().send(request, ed.UpdateItemsResponse)
        errors = [r.status.error_message for r in response.updated_items if r.status.code != ed.ISC_OK]

        if errors:
            raise RuntimeError("; ".join(errors))

    def auto_place(self, origin_x_mm: float = 20, origin_y_mm: float = 20, max_width_mm: float = 80,
                   gap_mm: float = 1.5) -> dict:
        """Initial placement: each small part goes next to the larger part it shares most signal
        nets with (decoupling caps next to their IC, pull-ups next to their pin), then the groups
        are packed in rows. Meant as a starting point to refine with move_footprints."""
        footprints = self.footprints()
        netlist = self.session.export_netlist()
        gap = _nm(gap_mm)

        def is_anchor(fp: PlacedFootprint) -> bool:
            return fp.pad_count >= 3 or fp.reference[:1] in "UJMPK"

        # Affinity between parts: small nets bind strongly, big distribution nets weakly, and a
        # two-pin part on an IC's power pin net (a decoupling capacitor) binds to that IC.
        weights: dict[tuple[str, str], float] = {}

        def bind(a: str, b: str, weight: float) -> None:
            weights[(a, b)] = weights.get((a, b), 0) + weight
            weights[(b, a)] = weights.get((b, a), 0) + weight

        for net, nodes in netlist.nets.items():
            if is_ground(net):
                continue

            refs = sorted({n.ref for n in nodes if n.ref in footprints})

            if len(refs) < 2:
                continue

            weight = 1 / (len(refs) - 1)

            for i, a in enumerate(refs):
                for b in refs[i + 1:]:
                    bind(a, b, weight)

            powered = {n.ref for n in nodes if n.pin_type in ("power_in", "power_out")}

            for ref in refs:
                if footprints[ref].pad_count == 2:
                    for ic in powered - {ref}:
                        bind(ref, ic, 1.0)

        anchors = sorted((fp for fp in footprints.values() if is_anchor(fp)),
                         key=lambda fp: -(fp.box_w * fp.box_h))
        groups: dict[str, list[PlacedFootprint]] = {a.reference: [] for a in anchors}
        loose = []

        for fp in footprints.values():
            if is_anchor(fp):
                continue

            best = max(anchors, key=lambda a: (weights.get((fp.reference, a.reference), 0),
                                               a.box_w * a.box_h), default=None)

            if best and weights.get((fp.reference, best.reference), 0) > 0:
                groups[best.reference].append(fp)
            else:
                loose.append(fp)

        # Bottom edge of each part's keepout (e.g. an RF antenna area): nothing goes beside it.
        keepout_bottom: dict[str, int] = {}

        for ref, zone in self._footprint_keepouts():
            ys = [n.point.y_nm for polygon in zone.outline.polygons for n in polygon.outline.nodes]

            if ys:
                keepout_bottom[ref] = max(keepout_bottom.get(ref, ys[0]), max(ys))

        # Lay each group out: anchor on the left, its parts in columns to its right.
        blocks = []

        for anchor in anchors:
            members = sorted(groups[anchor.reference], key=lambda fp: fp.reference)
            start_y = 0

            if anchor.reference in keepout_bottom:
                start_y = max(0, keepout_bottom[anchor.reference] - anchor.box_y + gap)

            column_height = max(anchor.box_h, start_y + _nm(10))
            positions = [(anchor, 0, 0)]
            x = anchor.box_w + gap
            y = start_y
            column_width = 0

            for fp in members:
                if y > start_y and y + fp.box_h > column_height:
                    x += column_width + gap
                    y = start_y
                    column_width = 0

                positions.append((fp, x, y))
                y += fp.box_h + gap
                column_width = max(column_width, fp.box_w)

            width = max(p[1] + p[0].box_w for p in positions)
            height = max(p[2] + p[0].box_h for p in positions)
            blocks.append((width, height, positions))

        for fp in loose:
            blocks.append((fp.box_w, fp.box_h, [(fp, 0, 0)]))

        # Shelf packing of the blocks into rows.
        moves = []
        max_width = _nm(max_width_mm)
        x = y = row_height = 0

        for width, height, positions in blocks:
            if x > 0 and x + width > max_width:
                x = 0
                y += row_height + 2 * gap
                row_height = 0

            for fp, dx, dy in positions:
                # Shift from the box's top-left corner to the footprint origin.
                origin_dx = fp.instance.position.x_nm - fp.box_x
                origin_dy = fp.instance.position.y_nm - fp.box_y
                moves.append({"ref": fp.reference,
                              "x_mm": (_nm(origin_x_mm) + x + dx + origin_dx) / NM_PER_MM,
                              "y_mm": (_nm(origin_y_mm) + y + dy + origin_dy) / NM_PER_MM})

            x += width + 2 * gap
            row_height = max(row_height, height)

        self.move_footprints(moves)
        placed = self.footprints()
        x0 = min(fp.box_x for fp in placed.values())
        y0 = min(fp.box_y for fp in placed.values())
        x1 = max(fp.box_x + fp.box_w for fp in placed.values())
        y1 = max(fp.box_y + fp.box_h for fp in placed.values())
        return {"groups": {a: [m.reference for m in ms] for a, ms in groups.items() if ms},
                "ungrouped": [fp.reference for fp in loose],
                "parts_extent_mm": {"x": to_mm(x0), "y": to_mm(y0), "width": to_mm(x1 - x0),
                                    "height": to_mm(y1 - y0)}}

    # ------------------------------------------------------------------ outline, zones, rules

    def set_outline(self, x_mm: float, y_mm: float, width_mm: float, height_mm: float,
                    corner_radius_mm: float = 1.0) -> dict:
        """Replace the board outline (Edge.Cuts) with a rectangle."""
        old = []

        for packed in self._get_items(en.KOT_PCB_SHAPE):
            shape = bty.BoardGraphicShape()
            packed.Unpack(shape)

            if shape.layer == bty.BL_Edge_Cuts:
                old.append(shape.id.value)

        if old:
            request = ed.DeleteItems()
            request.header.document.CopyFrom(self._board())

            for item_id in old:
                request.item_ids.add(value=item_id)

            self._client().send(request, ed.DeleteItemsResponse)

        shape = bty.BoardGraphicShape(layer=bty.BL_Edge_Cuts)
        rect = shape.shape.rectangle
        rect.top_left.x_nm, rect.top_left.y_nm = _nm(x_mm), _nm(y_mm)
        rect.bottom_right.x_nm, rect.bottom_right.y_nm = _nm(x_mm + width_mm), _nm(y_mm + height_mm)
        rect.corner_radius.value_nm = _nm(corner_radius_mm)
        shape.shape.attributes.stroke.width.value_nm = _nm(0.1)
        self._create([shape])
        self.save()
        return {"outline_mm": [x_mm, y_mm, width_mm, height_mm], "replaced": len(old)}

    def fit_outline(self, margin_mm: float = 3.0, corner_radius_mm: float = 1.0) -> dict:
        """Rectangular outline around all footprints plus a margin."""
        footprints = self.footprints()

        if not footprints:
            raise RuntimeError("the board has no footprints; call update_board_from_schematic first")

        x0 = min(fp.box_x for fp in footprints.values())
        y0 = min(fp.box_y for fp in footprints.values())
        x1 = max(fp.box_x + fp.box_w for fp in footprints.values())
        y1 = max(fp.box_y + fp.box_h for fp in footprints.values())
        margin = _nm(margin_mm)
        return self.set_outline(to_mm(x0 - margin), to_mm(y0 - margin), to_mm(x1 - x0 + 2 * margin),
                                to_mm(y1 - y0 + 2 * margin), corner_radius_mm)

    def _outline_box(self) -> tuple[int, int, int, int]:
        request = bc.GetBoardBoundingBox(board=self._board(), mode=bc.BBBM_BOARD_EDGES_ONLY)
        box = self._client().send(request, bc.BoardBoundingBoxResponse).box
        return box.position.x_nm, box.position.y_nm, box.size.x_nm, box.size.y_nm

    def _create(self, items: list) -> None:
        request = ed.CreateItems()
        request.header.document.CopyFrom(self._board())

        for item in items:
            request.items.add().Pack(item)

        response = self._client().send(request, ed.CreateItemsResponse)
        errors = [r.status.error_message or ed.ItemStatusCode.Name(r.status.code)
                  for r in response.created_items if r.status.code != ed.ISC_OK]

        if errors:
            raise RuntimeError("; ".join(errors))

    def add_ground_zone(self, net: str = "GND", layers: tuple[str, ...] = ("F.Cu", "B.Cu"),
                        clearance_mm: float = 0.3) -> dict:
        """Copper pour on the given net covering the board outline, then fill it."""
        x, y, w, h = self._outline_box()

        if w <= 0 or h <= 0:
            raise RuntimeError("the board has no outline; call set_board_outline first")

        layer_ids = {"F.Cu": bty.BL_F_Cu, "B.Cu": bty.BL_B_Cu}
        inset = _nm(0.5)
        zone = bty.Zone(type=bty.ZT_COPPER, name=f"{net}_pour")
        zone.layers.extend(layer_ids[layer] for layer in layers)
        outline = zone.outline.polygons.add().outline
        outline.closed = True

        for px, py in ((x + inset, y + inset), (x + w - inset, y + inset),
                       (x + w - inset, y + h - inset), (x + inset, y + h - inset)):
            node = outline.nodes.add()
            node.point.x_nm, node.point.y_nm = px, py

        settings = zone.copper_settings
        settings.net.name = net if net.startswith("/") else f"/{net}"
        settings.clearance.value_nm = _nm(clearance_mm)
        settings.min_thickness.value_nm = _nm(0.25)
        settings.connection.zone_connection = bty.ZCS_THERMAL
        self._create([zone])
        self.refill_zones()
        return {"zone": zone.name, "net": settings.net.name, "layers": list(layers)}

    def refill_zones(self) -> None:
        self._client().send(bc.RefillZones(board=self._board()), empty_pb2.Empty)
        self.save()

    def set_power_netclass(self, nets: list[str], track_width_mm: float = 1.0,
                           clearance_mm: float = 0.25, name: str = "Power") -> dict:
        """Create/replace a net class with wider tracks and assign the given nets (or patterns
        such as '+*V*') to it. Freerouting and the interactive router both honour it."""
        project = self._board().project
        netclass = ps.NetClass(name=name, priority=10)
        netclass.board.track_width.value_nm = _nm(track_width_mm)
        netclass.board.clearance.value_nm = _nm(clearance_mm)
        self._client().send(pc.SetNetClasses(project=project, net_classes=[netclass],
                                             merge_mode=bt.MMM_MERGE), empty_pb2.Empty)

        request = pc.SetNetClassAssignments(project=project, merge_mode=bt.MMM_MERGE)

        for net in nets:
            pattern = net if net.startswith("/") or "*" in net else f"/{net}"
            request.pattern_assignments.add(pattern=pattern, netclass=name)

        self._client().send(request, empty_pb2.Empty)
        self.save()
        return {"netclass": name, "track_width_mm": track_width_mm, "nets": nets}

    def set_fab_rules(self, preset: str = "jlcpcb") -> dict:
        """Board minimums matching a fabricator's 2-layer capabilities."""
        presets = {
            # JLCPCB 2-layer, 1 oz: 5 mil track/space, 0.3 mm drill, 0.3 mm copper to edge
            "jlcpcb": {"min_track_width": 0.127, "min_clearance": 0.127, "min_through_drill": 0.3,
                       "min_via_size": 0.5, "copper_edge_clearance": 0.3, "hole_to_hole_min": 0.5},
        }

        if preset not in presets:
            raise RuntimeError(f"unknown preset {preset}; available: {sorted(presets)}")

        rules = self._client().send(bc.GetBoardDesignRules(board=self._board()),
                                    bc.BoardDesignRulesResponse).rules

        for name, value_mm in presets[preset].items():
            getattr(rules.constraints, name).value_nm = _nm(value_mm)

        self._client().send(bc.SetBoardDesignRules(board=self._board(), rules=rules), bc.BoardDesignRulesResponse)
        self.save()
        return {"preset": preset, "constraints_mm": presets[preset]}

    def _footprint_keepouts(self) -> list:
        """Rule areas inside footprints that forbid tracks or vias (e.g. RF module antennas)."""
        keepouts = []

        for fp in self.footprints().values():
            for item in fp.instance.definition.items:
                if not item.Is(bty.Zone.DESCRIPTOR):
                    continue

                zone = bty.Zone()
                item.Unpack(zone)
                rules = zone.rule_area_settings

                if zone.type == bty.ZT_RULE_AREA and (rules.keepout_tracks or rules.keepout_vias):
                    keepouts.append((fp.reference, zone))

        return keepouts

    # ------------------------------------------------------------------ routing and checks

    def autoroute(self, passes: int = 30, timeout_s: int = 600) -> dict:
        """Route all unrouted connections with Freerouting (Specctra DSN out, session back in)."""
        jar = self.freerouting_jar

        if not jar or not Path(jar).exists():
            raise RuntimeError("Freerouting is not configured: set FREEROUTING_JAR to the "
                               "freerouting .jar path (https://github.com/freerouting/freerouting)")

        if not self.java:
            raise RuntimeError("java was not found (Freerouting 2.5 needs Java 25; set JAVA)")

        # The Specctra export only carries board-level keepouts, so footprint keepouts (an RF
        # module's antenna area) are copied to the board for the duration of the routing.
        temporary = []

        for ref, zone in self._footprint_keepouts():
            copy = bty.Zone()
            copy.CopyFrom(zone)
            copy.ClearField("id")
            copy.ClearField("parent")
            copy.name = f"autoroute_keepout_{ref}"
            temporary.append(copy)

        if temporary:
            self._create(temporary)

        self.save()

        try:
            with tempfile.TemporaryDirectory() as tmp:
                dsn = Path(tmp) / "board.dsn"
                ses = Path(tmp) / "board.ses"
                result = self.session._run_cli("pcb", "export", "specctra", "-o", str(dsn), str(self.board_path))

                if not dsn.exists():
                    raise RuntimeError(f"Specctra export failed: {result.stderr or result.stdout}")

                started = time.time()
                router = subprocess.run([self.java, "-jar", str(jar), "-de", str(dsn), "-do", str(ses),
                                         "-mp", str(passes), "-l", "en", "--gui.enabled=false"],
                                        capture_output=True, text=True, encoding="utf-8", errors="replace",
                                        timeout=timeout_s, cwd=tmp,  # Freerouting writes its log here
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

                if not ses.exists():
                    tail = (router.stderr or router.stdout)[-1500:]
                    raise RuntimeError(f"Freerouting did not produce a session file:\n{tail}")

                response = self._client().send(bc.ImportSpecctraSession(board=self._board(),
                                                                        session_path=str(ses)),
                                               bc.ImportSpecctraSessionResponse)
        finally:
            self._delete_named_zones("autoroute_keepout_")

        self.refill_zones()
        log = [line for line in (router.stdout + router.stderr).splitlines()
               if any(k in line.lower() for k in ("unrouted", "incomplete", "completed", "error"))]
        return {"seconds": round(time.time() - started, 1), "skipped": response.skipped_count,
                "router_log": log[-15:], "report": response.report[-1500:]}

    def _delete_named_zones(self, prefix: str) -> None:
        ids = []

        for packed in self._get_items(en.KOT_PCB_ZONE):
            zone = bty.Zone()
            packed.Unpack(zone)

            if zone.name.startswith(prefix):
                ids.append(zone.id.value)

        if ids:
            request = ed.DeleteItems()
            request.header.document.CopyFrom(self._board())

            for item_id in ids:
                request.item_ids.add(value=item_id)

            self._client().send(request, ed.DeleteItemsResponse)
            self.save()

    def run_drc(self) -> dict:
        self.save()

        with tempfile.TemporaryDirectory() as tmp:
            report = Path(tmp) / "drc.json"
            result = self.session._run_cli("pcb", "drc", "--format", "json", "--severity-all",
                                           "--schematic-parity", "--refill-zones",
                                           "-o", str(report), str(self.board_path))

            if not report.exists():
                raise RuntimeError(f"kicad-cli pcb drc failed: {result.stderr or result.stdout}")

            data = json.loads(report.read_text(encoding="utf-8"))

        def simplify(items: list) -> list:
            return [{"severity": v.get("severity"), "type": v.get("type"),
                     "description": v.get("description"),
                     "items": [i.get("description") for i in v.get("items", [])]} for v in items]

        sections = {"violations": simplify(data.get("violations", [])),
                    "unconnected": simplify(data.get("unconnected_items", [])),
                    "schematic_parity": simplify(data.get("schematic_parity", []))}
        counts = {name: len(items) for name, items in sections.items()}
        # Keep the reply small: unconnected items repeat per pad pair.
        sections["unconnected"] = sections["unconnected"][:40]
        return {"counts": counts, **sections}

    def export_view(self, kind: str = "2d", output_path: str | None = None,
                    layers: str = "F.Cu,B.Cu,F.Silkscreen,F.Courtyard,Edge.Cuts") -> str:
        """Picture of the board for reviewing placement and routing.

        '2d' plots the given layers into one PDF page; '3d' renders a PNG of the top side."""
        self.save()

        if kind == "3d":
            out = Path(output_path) if output_path else self.board_path.with_name(
                    f"{self.board_path.stem}-3d.png")
            result = self.session._run_cli("pcb", "render", "--side", "top", "--quality", "basic",
                                           "--width", "1600", "--height", "1200",
                                           "--background", "opaque", "-o", str(out), str(self.board_path))
        else:
            out = Path(output_path) if output_path else self.board_path.with_name(
                    f"{self.board_path.stem}-layout.pdf")
            result = self.session._run_cli("pcb", "export", "pdf", "--mode-single", "-l", layers,
                                           "--check-zones", "-o", str(out), str(self.board_path))

        if not out.exists():
            raise RuntimeError(f"kicad-cli board image failed: {result.stderr or result.stdout}")

        return str(out)

    def export_step(self, output_path: str | None = None) -> str:
        """3D STEP model of the board, for enclosure and mechanical design."""
        self.save()
        out = Path(output_path) if output_path else self.board_path.with_suffix(".step")
        result = self.session._run_cli("pcb", "export", "step", "--subst-models", "-f", "-o", str(out),
                                       str(self.board_path))

        if not out.exists():
            raise RuntimeError(f"kicad-cli pcb export step failed: {result.stderr or result.stdout}")

        return str(out)
