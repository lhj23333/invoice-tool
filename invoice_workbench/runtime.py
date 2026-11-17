import os
from pathlib import Path
import sys


MIN_PYTHON = (3, 11)


def require_python():
    if sys.platform != "win32" or sys.version_info.major != 3 or sys.version_info[:2] < MIN_PYTHON:
        minimum = ".".join(map(str, MIN_PYTHON))
        raise SystemExit(f"需要 Windows Python >= {minimum}（Python 3）；当前为 {sys.version.split()[0]}（{sys.platform}）。")


def configure_bundled_tk():
    runtime = Path(__file__).resolve().parent.parent / "runtime"
    if not runtime.is_dir() or Path(sys.base_prefix).resolve() != runtime.resolve():
        return
    import _tkinter

    tcl_library = runtime / "tcl" / f"tcl{_tkinter.TCL_VERSION}"
    tk_library = runtime / "tcl" / f"tk{_tkinter.TK_VERSION}"
    if tcl_library.is_dir():
        os.environ["TCL_LIBRARY"] = str(tcl_library)
    else:
        # Tcl 9 的标准 Windows 发行包将 Tcl 脚本嵌入 DLL。
        os.environ.pop("TCL_LIBRARY", None)
    if not tk_library.is_dir():
        raise FileNotFoundError(f"便携运行环境缺少 Tk 脚本目录：{tk_library}")
    os.environ["TK_LIBRARY"] = str(tk_library)
