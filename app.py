"""Desktop entry point, also used by the portable Windows launcher."""
from pathlib import Path
import sys
import traceback

from invoice_workbench.runtime import configure_bundled_tk, require_python

if __name__ == "__main__":
    root = Path(__file__).resolve().parent
    require_python()
    try:
        configure_bundled_tk()
        from invoice_workbench.gui import main
        main()
    except Exception:
        (root / "startup-error.log").write_text(traceback.format_exc(), encoding="utf-8")
        raise
