"""Install the 'Ask Claude' toolbar button into KiCad's user plugins folder.

Run with the conda env 'kicad' Python, after building KiCad (which must have the API server
enabled: Preferences > Plugins > Enable KiCad API). Restart KiCad (or use Preferences >
Plugins > Reload) and the button appears in the Schematic and PCB editors.
"""

import ctypes
import json
import math
import struct
import sys
import zlib
from pathlib import Path

HERE = Path(__file__).resolve().parent
IDENTIFIER = "com.maxrosan.askclaude"
KICAD_VERSION_DIR = "10.99"


def documents_dir() -> Path:
    """The user's Documents folder (follows OneDrive/folder redirection on Windows)."""
    if sys.platform == "win32":
        buffer = ctypes.create_unicode_buffer(260)
        ctypes.windll.shell32.SHGetFolderPathW(None, 5, None, 0, buffer)  # CSIDL_PERSONAL
        return Path(buffer.value)

    return Path.home() / "Documents"


def write_icon(path: Path, size: int) -> None:
    """A Claude-orange disc with a white ring, as a minimal RGBA PNG."""
    rows = []
    centre = (size - 1) / 2

    for y in range(size):
        row = bytearray([0])  # filter type: none

        for x in range(size):
            r = math.hypot(x - centre, y - centre) / (size / 2)

            if r <= 0.95:
                ring = 0.45 <= r <= 0.62
                row += bytes((255, 255, 255, 255) if ring else (217, 119, 87, 255))
            else:
                row += bytes((0, 0, 0, 0))

        rows.append(bytes(row))

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))

    png = (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows))) + chunk(b"IEND", b""))
    path.write_bytes(png)


def main() -> int:
    target = documents_dir() / "KiCad" / KICAD_VERSION_DIR / "plugins" / IDENTIFIER
    target.mkdir(parents=True, exist_ok=True)

    launcher = target / "ask_claude.cmd"
    launcher.write_text(
            "@echo off\r\n"
            f"set PYTHONPATH={HERE}\r\n"
            f"\"{sys.executable}\" -m kicad_mcp.ask_claude %*\r\n", encoding="utf-8")

    for size in (24, 48):
        write_icon(target / f"icon-{size}.png", size)

    icons = ["icon-24.png", "icon-48.png"]

    def action(identifier: str, scope: str) -> dict:
        return {"identifier": identifier, "name": "Perguntar ao Claude",
                "description": "Pergunta ao Claude sobre o projeto aberto e a seleção atual",
                "show-button": True, "scopes": [scope], "entrypoint": "ask_claude.cmd",
                "args": ["--scope", scope], "icons-light": icons, "icons-dark": icons}

    plugin = {"$schema": "https://go.kicad.org/api/schemas/v1", "identifier": IDENTIFIER,
              "name": "Ask Claude", "description": "Pergunta ao Claude (Claude Code + kicad MCP)",
              "runtime": {"type": "exec"},
              "actions": [action("ask-schematic", "schematic"), action("ask-pcb", "pcb")]}
    (target / "plugin.json").write_text(json.dumps(plugin, indent=2), encoding="utf-8")
    print(f"Installed {IDENTIFIER} into {target}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
