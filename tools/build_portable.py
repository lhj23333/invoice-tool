"""Build a portable release, excluding local settings and invoice data."""
from pathlib import Path
import argparse
import hashlib
import importlib.metadata as metadata
import json
import os
import shutil
import subprocess
import sys
import sysconfig
import zipfile

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from invoice_workbench import __version__

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_runtime(destination):
    original = Path(sys.base_prefix)
    if (original / "python312._pth").exists() and (original / "python312.zip").exists():
        shutil.copytree(original, destination, ignore=IGNORE)
        return
    destination.mkdir()
    for name in ("python.exe", "pythonw.exe", "python3.dll", "python312.dll", "vcruntime140.dll", "vcruntime140_1.dll", "LICENSE.txt"):
        if (original / name).exists():
            copy_file(original / name, destination / name)
    shutil.copytree(original / "DLLs", destination / "DLLs", ignore=IGNORE)
    shutil.copytree(original / "tcl", destination / "tcl", ignore=IGNORE)
    stdlib = Path(sysconfig.get_path("stdlib"))
    with zipfile.ZipFile(destination / "python312.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, names in os.walk(stdlib):
            dirs[:] = [d for d in dirs if d not in {"site-packages", "__pycache__", "test", "tests", "idlelib", "turtledemo", "ensurepip"}]
            for name in names:
                path = Path(base) / name
                if path.suffix != ".pyc":
                    archive.write(path, path.relative_to(stdlib).as_posix())
    package_root = Path(sysconfig.get_path("purelib")).resolve()
    for name in ("pypdf", "Pillow", "pypdfium2"):
        distribution = metadata.distribution(name)
        for file in distribution.files or []:
            path = Path(distribution.locate_file(file)).resolve()
            if path.is_relative_to(package_root) and path.is_file() and path.suffix != ".pyc":
                copy_file(path, destination / "Lib" / "site-packages" / path.relative_to(package_root))
    (destination / "python312._pth").write_text("python312.zip\nDLLs\nLib/site-packages\n..\nimport site\n", encoding="ascii")


def build(destination):
    destination = destination.resolve()
    if not destination.is_relative_to(ROOT / "dist"):
        raise ValueError("Output must stay in this project's dist directory")
    if destination.exists():
        raise ValueError("Output exists; choose a new name instead of overwriting")
    archive = destination.parent / f"InvoiceWorkbench-Windows-v{__version__}.zip"
    if archive.exists():
        raise ValueError("Archive already exists")
    destination.mkdir(parents=True)
    for name in ("app.py", "README.md", "requirements.txt", "AGENTS.md", "setup.ps1", "start.ps1", "Install-ToSoft.ps1"):
        copy_file(ROOT / name, destination / name)
    for name in ("invoice_workbench", "samples", "tests", "tools", "docs"):
        shutil.copytree(ROOT / name, destination / name, ignore=IGNORE)
    copy_runtime(destination / "runtime")
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                    f"/out:{destination / 'InvoiceWorkbench.exe'}", str(ROOT / "tools" / "Launcher.cs")], check=True)
    versions = {name: metadata.version(name) for name in ("pypdf", "Pillow", "pypdfium2")}
    (destination / "build-info.json").write_text(json.dumps({"version": __version__, "python": sys.version,
                                                               "packages": versions}, indent=2), encoding="utf-8")
    manifest = {p.relative_to(destination).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
                for p in sorted(destination.rglob("*")) if p.is_file()}
    (destination / "bundle-manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as bundle:
        for path in sorted(destination.rglob("*")):
            if path.is_file():
                bundle.write(path, path.relative_to(destination.parent).as_posix())
    print(json.dumps({"directory": str(destination), "archive": str(archive), "size_mib": round(archive.stat().st_size / 1024**2, 1), "versions": versions}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "dist" / f"InvoiceWorkbench-v{__version__}")
    build(parser.parse_args().output)
