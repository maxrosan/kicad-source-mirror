"""Generate a firmware pin map header from the schematic netlist.

Each microcontroller GPIO that reaches a signal net becomes a #define named after the net,
so firmware and schematic share one source of truth. Resistive dividers read by a GPIO also
get their multiplier, so ADC readings can be scaled back to the real voltage.
"""

import re
from datetime import date

from .netlist import Netlist
from .review import Review, gpio_number, is_ground, net_voltage

_POWER_PIN = re.compile(r"^(VDD\w*|VCC\w*|VBAT|VIN|GND\w*|EN|CHIP_PU|NC|3V3|5V)$", re.IGNORECASE)


def _macro(name: str) -> str:
    macro = re.sub(r"[^A-Za-z0-9]+", "_", name).strip("_").upper()
    return f"PIN_{macro}" if macro[:1].isdigit() else macro


def _find_mcu(netlist: Netlist, reference: str | None) -> str:
    if reference:
        if reference not in netlist.components:
            raise RuntimeError(f"no component {reference} in the schematic")

        return reference

    candidates = []

    for comp in netlist.components.values():
        gpios = sum(1 for nodes in netlist.nets.values() for n in nodes
                    if n.ref == comp.ref and gpio_number(n.function) is not None)

        if gpios:
            candidates.append((gpios, comp.ref))

    if not candidates:
        raise RuntimeError("no component with GPIO/IO pins found; pass the microcontroller reference")

    return max(candidates)[1]


def pin_map(netlist: Netlist, reference: str | None = None) -> dict:
    """GPIOs of the microcontroller that reach signal nets, plus dividers they read."""
    mcu = _find_mcu(netlist, reference)
    review = Review(netlist)
    review.collect_resistor_networks()
    dividers = {d["net"]: d for d in review.facts["dividers"]}
    pins = []

    for net, nodes in netlist.nets.items():
        if net.startswith("unconnected-") or is_ground(net) or net_voltage(net) is not None:
            continue

        for node in nodes:
            if node.ref != mcu or _POWER_PIN.match(node.function):
                continue

            gpio = gpio_number(node.function)

            if gpio is None:
                continue

            others = [n.label() for n in nodes if n.ref != mcu]
            pins.append({"net": net, "gpio": gpio, "pin": node.label(), "connects_to": others,
                         "divider": dividers.get(net)})

    pins.sort(key=lambda p: p["gpio"])
    return {"mcu": mcu, "value": netlist.components[mcu].value, "pins": pins}


def render_header(pins: dict, style: str = "arduino", project: str = "") -> str:
    """C header text for a pin map. `style` is 'arduino' (plain numbers) or 'esp-idf' (GPIO_NUM_x)."""
    guard = _macro(f"{project or 'board'}_pins_h")
    lines = [
        "// Generated from the KiCad schematic by the kicad MCP (get_pin_map / write_pin_header).",
        f"// Project: {project or '-'}    MCU: {pins['mcu']} ({pins['value']})    Date: {date.today()}",
        "// Do not edit by hand: change the schematic and regenerate.",
        "",
        f"#ifndef {guard}",
        f"#define {guard}",
        "",
    ]

    if style == "esp-idf":
        lines += ["#include \"driver/gpio.h\"", ""]

    for pin in pins["pins"]:
        value = f"GPIO_NUM_{pin['gpio']}" if style == "esp-idf" else str(pin["gpio"])
        others = ", ".join(pin["connects_to"]) or "nothing else"
        lines.append(f"// {pin['pin']} -> {others}")
        lines.append(f"#define {_macro(pin['net']):<24} {value}")

        if pin["divider"]:
            divider = pin["divider"]
            lines.append(f"// Divider {divider['top']} / {divider['bottom']}: "
                         f"V_real = V_adc * {divider['multiplier']}")
            lines.append(f"#define {_macro(pin['net']) + '_DIVIDER':<24} {divider['multiplier']}f")

        lines.append("")

    lines += [f"#endif // {guard}", ""]
    return "\n".join(lines)
