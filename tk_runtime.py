"""Use bundled Tcl/Tk scripts if this Python installation only ships ZIPs."""
import os
from pathlib import Path
import sys
import zipfile


def prepare_tk():
    if getattr(sys, "frozen", False):
        return  # PyInstaller's runtime hook sets the bundled Tcl/Tk paths.
    bundled = Path(sys.base_prefix) / "tcl"
    for kind, variable, marker in (("tcl", "TCL_LIBRARY", "init.tcl"),
                                   ("tk", "TK_LIBRARY", "tk.tcl")):
        if os.environ.get(variable):
            continue
        expanded = next((folder for folder in bundled.glob(f"{kind}[0-9]*")
                         if (folder / marker).is_file()), None)
        if expanded:
            os.environ[variable] = str(expanded)
            continue
        archives = sorted(bundled.glob(f"lib{kind}[0-9]*.zip"))
        if not archives:
            continue
        archive = archives[-1]
        destination = Path(__file__).resolve().parent / ".tk-runtime" / archive.stem
        library = destination / f"{kind}_library"
        if not (library / marker).exists():
            destination.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(archive) as package:
                base = destination.resolve()
                for member in package.infolist():
                    target = (base / member.filename).resolve()
                    if not target.is_relative_to(base):
                        raise ValueError("잘못된 Tcl/Tk 런타임 경로입니다.")
                package.extractall(destination)
        if (library / marker).exists():
            os.environ[variable] = str(library)


def create_root():
    prepare_tk()
    import tkinter
    return tkinter.Tk()
