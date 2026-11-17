import argparse
import json
from pathlib import Path
import sys
import sysconfig

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from invoice_workbench.runtime import configure_bundled_tk, require_python


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--setup", action="store_true")
    parser.add_argument("--environment", type=Path)
    args = parser.parse_args()
    require_python()
    configure_bundled_tk()
    import tkinter

    tkinter.Tcl()
    if args.setup:
        import ensurepip
        import venv
    else:
        import pypdf
        from PIL import Image, ImageTk
        import pypdfium2

    result = {"executable": sys.executable, "prefix": sys.prefix, "base_prefix": sys.base_prefix,
              "version": list(sys.version_info[:2]), "platform": sysconfig.get_platform()}
    if args.environment:
        variables = {key: str(args.environment.resolve())
                     for key in ("base", "platbase", "installed_base", "installed_platbase")}
        scripts = Path(sysconfig.get_path("scripts", scheme="venv", vars=variables))
        result["environment_python"] = str(scripts / Path(sys._base_executable).name)
    print(json.dumps(result))


if __name__ == "__main__":
    main()
