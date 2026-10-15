"""'Ask Claude' action launched from a KiCad toolbar button (API plugin, exec runtime).

KiCad starts this with KICAD_API_SOCKET/KICAD_API_TOKEN pointing at the running editor. The
script reads the current selection, asks the user for a question, and runs Claude Code
headless (`claude -p`) with this repository's kicad MCP attached live to the same KiCad, so
Claude can inspect (and, if allowed, edit) the open project. The answer is shown in a window.
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import tkinter as tk
from pathlib import Path
from tkinter import scrolledtext

from .client import KiCadClient

from board import board_types_pb2 as bty  # noqa: E402  (path set up by .client)
from common.commands import editor_commands_pb2 as ed
from common.types import base_types_pb2 as bt
from schematic import schematic_types_pb2 as st

MCP_DIR = Path(__file__).resolve().parents[1]

# Tools Claude may use when the user did not allow changes to the project.
READ_ONLY_TOOLS = ["kicad_status", "open_schematic", "search_symbols", "search_footprints",
                   "symbol_pins", "list_symbols", "run_erc", "get_netlist", "review_design",
                   "get_pin_map", "list_footprints", "run_drc", "export_board_view"]


def _describe_item(packed) -> str:
    if packed.Is(st.SchematicSymbolInstance.DESCRIPTOR):
        sym = st.SchematicSymbolInstance()
        packed.Unpack(sym)
        lib = sym.definition.id
        return (f"symbol {sym.reference_field.text.text} = {sym.value_field.text.text} "
                f"({lib.library_nickname}:{lib.entry_name})")

    if packed.Is(st.LocalLabel.DESCRIPTOR):
        label = st.LocalLabel()
        packed.Unpack(label)
        return f"net label {label.text.text}"

    if packed.Is(bty.FootprintInstance.DESCRIPTOR):
        fp = bty.FootprintInstance()
        packed.Unpack(fp)
        lib = fp.definition.id
        return (f"footprint {fp.reference_field.text.text.text} = {fp.value_field.text.text.text} "
                f"({lib.library_nickname}:{lib.entry_name})")

    return packed.TypeName().rsplit(".", 1)[-1]


def read_context(scope: str) -> dict:
    """Project and selection of the editor that launched the action."""
    client = KiCadClient(os.environ["KICAD_API_SOCKET"], client_name="kicad-ask-claude")
    client.token = os.environ.get("KICAD_API_TOKEN", "")
    doc_type = bt.DOCTYPE_PCB if scope == "pcb" else bt.DOCTYPE_SCHEMATIC

    try:
        documents = client.send(ed.GetOpenDocuments(type=doc_type), ed.GetOpenDocumentsResponse).documents

        if not documents:
            return {"project": None, "selection": []}

        document = documents[0]
        request = ed.GetSelection()
        request.header.document.CopyFrom(document)
        selection = client.send(request, ed.SelectionResponse).items
        return {"project": document.project.name, "project_dir": document.project.path,
                "selection": [_describe_item(item) for item in selection]}
    finally:
        client.close()


def build_prompt(context: dict, scope: str, question: str, allow_edits: bool) -> str:
    editor = "PCB Editor" if scope == "pcb" else "Schematic Editor"
    selection = "\n".join(f"- {s}" for s in context["selection"]) or "- (nothing selected)"
    mode = ("You may change the project with the kicad tools; the user is watching KiCad, so "
            "describe what you changed." if allow_edits else
            "Do not change the project: only inspect it and answer.")
    return (f"You are helping inside KiCad's {editor}. The open project is '{context['project']}'. "
            f"Use the kicad MCP tools (live mode, attached to this KiCad: call open_schematic with no "
            f"path first) when you need to look at the design. {mode} Answer in Portuguese, "
            f"briefly.\n\nSelected items:\n{selection}\n\nQuestion: {question}")


def run_claude(prompt: str, context: dict, allow_edits: bool) -> str:
    claude = shutil.which("claude") or str(Path.home() / ".local" / "bin" / "claude.exe")

    if not Path(claude).exists():
        return "Claude Code (claude) não foi encontrado no PATH."

    env = {"PYTHONPATH": str(MCP_DIR), "KICAD_API_SOCKET": os.environ["KICAD_API_SOCKET"]}
    config = {"mcpServers": {"kicad": {"command": sys.executable, "args": ["-m", "kicad_mcp"], "env": env}}}
    allowed = ["mcp__kicad"] if allow_edits else [f"mcp__kicad__{t}" for t in READ_ONLY_TOOLS]

    with tempfile.TemporaryDirectory() as tmp:
        config_path = Path(tmp) / "mcp.json"
        config_path.write_text(json.dumps(config), encoding="utf-8")
        result = subprocess.run([claude, "-p", prompt, "--mcp-config", str(config_path),
                                 "--strict-mcp-config", "--allowedTools", *allowed,
                                 "--output-format", "text"],
                                cwd=context.get("project_dir") or None, capture_output=True, text=True,
                                encoding="utf-8", errors="replace",
                                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    return result.stdout.strip() or result.stderr.strip() or "(sem resposta)"


class AskWindow:
    def __init__(self, scope: str, context: dict):
        self.scope = scope
        self.context = context
        self.root = tk.Tk()
        self.root.title("Perguntar ao Claude")
        self.root.geometry("720x560")

        selection = "\n".join(context["selection"]) or "(nada selecionado)"
        tk.Label(self.root, text=f"Projeto: {context['project']}    Seleção:", anchor="w").pack(fill="x", padx=8,
                                                                                                pady=(8, 0))
        info = scrolledtext.ScrolledText(self.root, height=5, wrap="word")
        info.insert("1.0", selection)
        info.configure(state="disabled")
        info.pack(fill="x", padx=8)

        tk.Label(self.root, text="Pergunta:", anchor="w").pack(fill="x", padx=8, pady=(8, 0))
        self.question = scrolledtext.ScrolledText(self.root, height=4, wrap="word")
        self.question.pack(fill="x", padx=8)
        self.question.focus_set()

        controls = tk.Frame(self.root)
        controls.pack(fill="x", padx=8, pady=6)
        self.allow_edits = tk.BooleanVar(value=False)
        tk.Checkbutton(controls, text="Permitir que o Claude altere o projeto",
                       variable=self.allow_edits).pack(side="left")
        self.button = tk.Button(controls, text="Perguntar", command=self.ask)
        self.button.pack(side="right")

        tk.Label(self.root, text="Resposta:", anchor="w").pack(fill="x", padx=8)
        self.answer = scrolledtext.ScrolledText(self.root, wrap="word")
        self.answer.pack(fill="both", expand=True, padx=8, pady=(0, 8))

    def ask(self) -> None:
        question = self.question.get("1.0", "end").strip()

        if not question:
            return

        self.button.configure(state="disabled")
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", "Pensando...")
        allow = self.allow_edits.get()

        def work() -> None:
            text = run_claude(build_prompt(self.context, self.scope, question, allow), self.context, allow)
            self.root.after(0, lambda: self.show(text))

        threading.Thread(target=work, daemon=True).start()

    def show(self, text: str) -> None:
        self.answer.delete("1.0", "end")
        self.answer.insert("1.0", text)
        self.button.configure(state="normal")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--scope", choices=["schematic", "pcb"], default="schematic")
    args = parser.parse_args()

    if "KICAD_API_SOCKET" not in os.environ:
        print("Run this from the KiCad toolbar button (KICAD_API_SOCKET is not set).", file=sys.stderr)
        sys.exit(1)

    AskWindow(args.scope, read_context(args.scope)).root.mainloop()


if __name__ == "__main__":
    main()
