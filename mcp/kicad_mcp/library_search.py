"""Search the stock KiCad symbol and footprint libraries on disk.

The headless API server does not implement SearchLibraries, and scanning the library files
directly is fast enough: each library is indexed once, on the first search.
"""

import re
from dataclasses import dataclass
from pathlib import Path

_SYM_DESCR = re.compile(r'\(property\s+"Description"\s+"((?:[^"\\]|\\.)*)"')
_SYM_KEYWORDS = re.compile(r'\(property\s+"ki_keywords"\s+"((?:[^"\\]|\\.)*)"')
_SYM_NAME = re.compile(r'^\s*\(symbol\s+"((?:[^"\\]|\\.)*)"', re.MULTILINE)
_FP_DESCR = re.compile(r'\(descr\s+"((?:[^"\\]|\\.)*)"')
_FP_TAGS = re.compile(r'\(tags\s+"((?:[^"\\]|\\.)*)"')


@dataclass
class LibraryEntry:
    lib_id: str
    description: str
    keywords: str


def _first(pattern: re.Pattern, text: str) -> str:
    match = pattern.search(text)
    return match.group(1).replace('\\"', '"') if match else ""


class LibraryIndex:
    """Lazy index of the symbols and footprints shipped with KiCad.

    Args:
        share_dir: the KiCad `share/kicad` directory that holds `symbols/` and `footprints/`.
    """

    def __init__(self, share_dir: Path):
        self.symbols_dir = share_dir / "symbols"
        self.footprints_dir = share_dir / "footprints"
        self._symbols: list[LibraryEntry] | None = None
        self._footprints: list[LibraryEntry] | None = None

    def _index_symbols(self) -> list[LibraryEntry]:
        entries = []

        # Current format: one directory per library, one file per symbol.
        for lib_dir in sorted(self.symbols_dir.glob("*.kicad_symdir")):
            for sym_file in sorted(lib_dir.glob("*.kicad_sym")):
                text = sym_file.read_text(encoding="utf-8", errors="replace")
                entries.append(LibraryEntry(f"{lib_dir.stem}:{sym_file.stem}",
                                            _first(_SYM_DESCR, text), _first(_SYM_KEYWORDS, text)))

        # Legacy format: one file per library holding many symbols.
        for lib_file in sorted(self.symbols_dir.glob("*.kicad_sym")):
            text = lib_file.read_text(encoding="utf-8", errors="replace")
            names = [n for n in _SYM_NAME.findall(text) if not re.search(r"_\d+_\d+$", n)]

            for name in names:
                entries.append(LibraryEntry(f"{lib_file.stem}:{name}", "", ""))

        return entries

    def _index_footprints(self) -> list[LibraryEntry]:
        entries = []

        for lib_dir in sorted(self.footprints_dir.glob("*.pretty")):
            for fp_file in sorted(lib_dir.glob("*.kicad_mod")):
                # The description and tags are near the top; avoid reading big footprints entirely.
                with fp_file.open(encoding="utf-8", errors="replace") as handle:
                    head = handle.read(4096)

                entries.append(LibraryEntry(f"{lib_dir.stem}:{fp_file.stem}",
                                            _first(_FP_DESCR, head), _first(_FP_TAGS, head)))

        return entries

    @staticmethod
    def _search(entries: list[LibraryEntry], query: str, limit: int) -> list[LibraryEntry]:
        """Match every whitespace-separated term against the id, description and keywords.

        Entries whose name matches a term rank above entries that only match the description.
        """
        terms = [t.lower() for t in query.split() if t]
        scored = []

        for entry in entries:
            name = entry.lib_id.lower()
            haystack = f"{name} {entry.description.lower()} {entry.keywords.lower()}"

            if all(t in haystack for t in terms):
                score = sum(2 if t in name else 1 for t in terms)
                scored.append((-score, len(entry.lib_id), entry))

        scored.sort(key=lambda s: (s[0], s[1]))
        return [s[2] for s in scored[:limit]]

    def search_symbols(self, query: str, limit: int = 20) -> list[LibraryEntry]:
        if self._symbols is None:
            self._symbols = self._index_symbols()

        return self._search(self._symbols, query, limit)

    def search_footprints(self, query: str, limit: int = 20) -> list[LibraryEntry]:
        if self._footprints is None:
            self._footprints = self._index_footprints()

        return self._search(self._footprints, query, limit)
