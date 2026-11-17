"""Desktop entry point, also used by the portable Windows launcher."""
from pathlib import Path
import os
import sys
import traceback

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    runtime = root / "runtime"
    if runtime.is_dir():
        os.environ["TCL_LIBRARY"] = str(runtime / "tcl" / "tcl8.6")
        os.environ["TK_LIBRARY"] = str(runtime / "tcl" / "tk8.6")
    try:
        from invoice_workbench.gui import main
        main()
    except Exception:
        (root / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
