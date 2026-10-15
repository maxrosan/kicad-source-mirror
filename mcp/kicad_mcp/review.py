"""Design review checks that KiCad's ERC does not cover.

The checks are heuristics over the netlist. They report findings for Claude (or a person)
to judge, plus the structured facts they were based on (regulators, dividers, pull-ups),
so the reviewer can reason about values without re-deriving the circuit.
"""

import re

from .netlist import Netlist, parse_value

_GROUND = re.compile(r"^(GND\w*|VSS\w*|0V|AGND|DGND|PGND)$", re.IGNORECASE)
_GPIO_NAME = re.compile(r"(?:^|/)(?:GPIO|IO)(\d+)(?:/|$)")

# ESP32 (classic, e.g. WROOM-32/32E) pin facts, by GPIO number.
_ESP32_STRAPPING = {
    0: "IO0 selects the boot mode: it must be high at reset to run the firmware (low = download mode)",
    2: "IO2 must be low or floating at reset to enter download mode",
    5: "IO5 sets SDIO slave timing at reset",
    12: "IO12 (MTDI) selects the flash voltage: if it is high at reset the module boots with 1.8 V "
        "flash and fails on 3.3 V flash modules",
    15: "IO15 (MTDO) high at reset enables the boot log on UART0",
}
_ESP32_ADC2 = {0, 2, 4, 12, 13, 14, 15, 25, 26, 27}
_ESP32_INPUT_ONLY = {34, 35, 36, 37, 38, 39}
_ESP32_FLASH = {6, 7, 8, 9, 10, 11}
_ANALOG_HINT = re.compile(r"ADC|SENSE|VBAT|BAT_|ANALOG|AIN|POT|TEMP|NTC", re.IGNORECASE)
_OUTPUT_HINT = re.compile(r"PWM|LED|OUT|SERVO|MOTOR|EN_|CTRL|TX|DRV|RELAY|BUZZ", re.IGNORECASE)


def is_ground(net: str) -> bool:
    return bool(_GROUND.match(net))


def net_voltage(net: str) -> float | None:
    """Nominal voltage from a net name like '+5V', '3V3', '+12V' (None if not a voltage name)."""
    match = re.match(r"^\+?(\d+)V(\d*)", net, re.IGNORECASE)

    if not match:
        return None

    return float(f"{match.group(1)}.{match.group(2) or 0}")


def gpio_number(pin_name: str) -> int | None:
    match = _GPIO_NAME.search(pin_name)
    return int(match.group(1)) if match else None


def _is_power_net(netlist: Netlist, net: str) -> bool:
    if is_ground(net) or net_voltage(net) is not None:
        return True

    return any(n.pin_type in ("power_in", "power_out") for n in netlist.nets.get(net, []))


def _two_pin_parts(netlist: Netlist, prefix: str) -> list:
    return [c for c in netlist.components.values()
            if c.ref.startswith(prefix) and not c.ref.startswith("#") and len(c.nets) == 2]


def _is_esp32_classic(lib_id: str, value: str) -> bool:
    text = f"{lib_id} {value}".upper()
    return "ESP32" in text and not re.search(r"ESP32-?(S2|S3|C2|C3|C5|C6|C61|H2|P4)", text)


class Review:
    def __init__(self, netlist: Netlist):
        self.netlist = netlist
        self.findings: list[dict] = []
        self.facts: dict = {}

    def add(self, severity: str, check: str, message: str) -> None:
        self.findings.append({"severity": severity, "check": check, "message": message})

    # ------------------------------------------------------------------ generic checks

    def check_footprints(self) -> None:
        for comp in self.netlist.components.values():
            if not comp.footprint and not comp.ref.startswith("#"):
                self.add("warning", "footprint", f"{comp.ref} ({comp.value}) has no footprint")

    def check_single_pin_nets(self) -> None:
        for net, nodes in self.netlist.nets.items():
            if net.startswith("unconnected-") or len(nodes) != 1:
                continue

            self.add("warning", "single_pin_net",
                     f"net {net} only reaches {nodes[0].label()}; a typo in a net name leaves "
                     f"the pin unconnected")

    def check_decoupling(self) -> None:
        caps = _two_pin_parts(self.netlist, "C")

        for comp in self.netlist.components.values():
            if not comp.ref.startswith("U"):
                continue

            power_nets = {self.netlist.net_of(comp.ref, n.pin)
                          for net, nodes in self.netlist.nets.items() for n in nodes
                          if n.ref == comp.ref and n.pin_type == "power_in" and not is_ground(net)}

            for net in sorted(filter(None, power_nets)):
                decoupled = [c.ref for c in caps
                             if net in c.nets.values() and any(is_ground(n) for n in c.nets.values())]

                if not decoupled:
                    self.add("warning", "decoupling",
                             f"{comp.ref} ({comp.value}) power pin net {net} has no capacitor to ground")

    def collect_regulators(self) -> None:
        regulators = []
        caps = _two_pin_parts(self.netlist, "C")

        for comp in self.netlist.components.values():
            if not comp.ref.startswith("U"):
                continue

            by_name = {n.function.upper(): n for nodes in self.netlist.nets.values() for n in nodes
                       if n.ref == comp.ref}
            vin = next((by_name[k] for k in ("VIN", "VI", "IN", "VCC_IN") if k in by_name), None)
            vout = next((by_name[k] for k in ("VOUT", "VO", "OUT", "OUTPUT") if k in by_name), None)

            if not vin or not vout:
                continue

            in_net = self.netlist.net_of(comp.ref, vin.pin)
            out_net = self.netlist.net_of(comp.ref, vout.pin)
            entry = {"ref": comp.ref, "value": comp.value, "input": in_net, "output": out_net,
                     "input_caps": [f"{c.ref} {c.value}" for c in caps if in_net in c.nets.values()],
                     "output_caps": [f"{c.ref} {c.value}" for c in caps if out_net in c.nets.values()]}
            regulators.append(entry)

            if not entry["input_caps"]:
                self.add("warning", "regulator", f"{comp.ref} ({comp.value}) has no input capacitor on {in_net}")

            # A switching regulator's OUT pin is the switch node; its capacitor sits after the inductor.
            if not entry["output_caps"] and vout.pin_type != "output":
                self.add("warning", "regulator", f"{comp.ref} ({comp.value}) has no output capacitor on {out_net}")

        self.facts["regulators"] = regulators

    def collect_resistor_networks(self) -> None:
        resistors = _two_pin_parts(self.netlist, "R")
        dividers = []
        pulls = []

        for net, nodes in self.netlist.nets.items():
            if _is_power_net(self.netlist, net):
                continue

            here = [r for r in resistors if net in r.nets.values()]
            to_ground = [r for r in here if any(is_ground(n) for n in r.nets.values() if n != net)]
            to_supply = [r for r in here
                         if any(not is_ground(n) and _is_power_net(self.netlist, n)
                                for n in r.nets.values() if n != net)]
            readers = [n.label() for n in nodes if not n.ref.startswith(("R", "C"))]

            if len(here) >= 2 and to_ground:
                low = to_ground[0]
                high = next((r for r in here if r is not low), None)
                top_net = next((n for n in high.nets.values() if n != net), None) if high else None
                r_low, r_high = parse_value(low.value), parse_value(high.value) if high else None

                if high and r_low and r_high:
                    ratio = r_low / (r_low + r_high)
                    entry = {"net": net, "top": f"{high.ref} {high.value} from {top_net}",
                             "bottom": f"{low.ref} {low.value} to ground",
                             "ratio": round(ratio, 4), "multiplier": round(1 / ratio, 4),
                             "read_by": readers}
                    v_top = net_voltage(top_net or "")

                    if v_top:
                        entry["output_at_nominal_V"] = round(v_top * ratio, 3)

                    dividers.append(entry)
                    continue

            for r in to_supply:
                supply = next(n for n in r.nets.values() if n != net)
                pulls.append({"net": net, "resistor": f"{r.ref} {r.value}", "pull": "up", "to": supply,
                              "pins": readers})

            for r in to_ground:
                pulls.append({"net": net, "resistor": f"{r.ref} {r.value}", "pull": "down", "pins": readers})

        self.facts["dividers"] = dividers
        self.facts["pulls"] = pulls

    def check_capacitor_voltage(self) -> None:
        for comp in _two_pin_parts(self.netlist, "C"):
            match = re.search(r"(\d+(?:\.\d+)?)\s*V\b", comp.value, re.IGNORECASE)

            if not match:
                continue

            rating = float(match.group(1))
            nominal = max((net_voltage(n) or 0 for n in comp.nets.values()), default=0)

            if nominal and rating < nominal * 1.25:
                self.add("warning", "capacitor_voltage",
                         f"{comp.ref} ({comp.value}) sits on a {nominal} V net; use at least "
                         f"{nominal * 1.25:.1f} V (25% margin), ideally double")

    # ------------------------------------------------------------------ ESP32

    def check_esp32(self) -> None:
        mcus = [c for c in self.netlist.components.values() if _is_esp32_classic(c.lib_id, c.value)]
        pulls = {p["net"]: p for p in self.facts.get("pulls", [])}

        for mcu in mcus:
            pins = [n for nodes in self.netlist.nets.values() for n in nodes if n.ref == mcu.ref]

            for node in pins:
                net = self.netlist.net_of(mcu.ref, node.pin)
                gpio = gpio_number(node.function)

                if net is None or net.startswith("unconnected-") or gpio is None:
                    continue

                where = f"{mcu.ref} {node.function} (GPIO{gpio}) on net {net}"

                if gpio in _ESP32_FLASH:
                    self.add("error", "esp32_flash_pin", f"{where}: GPIO6-11 drive the module's flash; leave them unconnected")

                if gpio in _ESP32_STRAPPING:
                    pull = pulls.get(net)
                    state = f"pulled {pull['pull']} by {pull['resistor']}" if pull else "no pull resistor"
                    severity = "error" if gpio == 12 and pull and pull["pull"] == "up" else "info"
                    self.add(severity, "esp32_strapping", f"{where}: {state}. {_ESP32_STRAPPING[gpio]}")

                if gpio in _ESP32_ADC2 and _ANALOG_HINT.search(net):
                    self.add("warning", "esp32_adc2",
                             f"{where}: looks analog but GPIO{gpio} is ADC2, which cannot be read "
                             f"while Wi-Fi is on; use an ADC1 pin (GPIO32-39)")

                if gpio in _ESP32_INPUT_ONLY and _OUTPUT_HINT.search(net):
                    self.add("error", "esp32_input_only",
                             f"{where}: GPIO34-39 are input-only and cannot drive {net}")

            en = next((n for n in pins if n.function.upper() in ("EN", "CHIP_PU")), None)

            if en:
                en_net = self.netlist.net_of(mcu.ref, en.pin)
                has_pull = en_net in pulls and pulls[en_net]["pull"] == "up"
                has_cap = any(en_net in c.nets.values() and any(is_ground(n) for n in c.nets.values())
                              for c in _two_pin_parts(self.netlist, "C"))

                if not (has_pull and has_cap):
                    self.add("warning", "esp32_en",
                             f"{mcu.ref} EN should have a pull-up (10k) and a capacitor to ground "
                             f"(1uF) for a clean reset; found pull-up={has_pull}, capacitor={has_cap}")

        if not mcus and any("ESP32" in c.lib_id.upper() for c in self.netlist.components.values()):
            self.add("info", "esp32", "ESP32 variant other than the classic ESP32 found; "
                     "pin-specific checks (strapping, ADC2, input-only) were skipped")

    def run(self) -> dict:
        self.check_footprints()
        self.check_single_pin_nets()
        self.check_decoupling()
        self.collect_regulators()
        self.collect_resistor_networks()
        self.check_capacitor_voltage()
        self.check_esp32()

        counts: dict[str, int] = {}

        for finding in self.findings:
            counts[finding["severity"]] = counts.get(finding["severity"], 0) + 1

        return {"counts": counts, "findings": self.findings, "facts": self.facts}


def review(netlist: Netlist) -> dict:
    return Review(netlist).run()
