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
from invoice_workbench.domain import UserError
from invoice_workbench.outlook_auth import CLIENT_ID_ENV, CLIENT_ID_FILE, configured_client_id, validate_client_id
from invoice_workbench.runtime import require_python

IGNORE = shutil.ignore_patterns("__pycache__", "*.pyc")


def pinned_packages():
    return [line.partition("==")[0] for line in (ROOT / "requirements.txt").read_text(encoding="utf-8").splitlines()
            if "==" in line]


def copy_file(source, destination):
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, destination)


def copy_runtime(destination):
    require_python()
    original = Path(sys.base_prefix)
    library = Path(sysconfig.get_config_var("LDLIBRARY"))
    runtime_name = library.stem
    if (original / f"{runtime_name}._pth").exists():
        shutil.copytree(original, destination, ignore=IGNORE)
        return
    if not (original / "DLLs").is_dir() or not (original / "tcl").is_dir():
        raise ValueError("便携包构建需要包含 DLLs 和 tcl 的标准 Windows CPython 发行环境。")
    destination.mkdir()
    for name in ("python.exe", "pythonw.exe", library.name, "LICENSE.txt"):
        copy_file(original / name, destination / name)
    for name in ("python3.dll", "vcruntime140.dll", "vcruntime140_1.dll"):
        if (original / name).exists():
            copy_file(original / name, destination / name)
    shutil.copytree(original / "DLLs", destination / "DLLs", ignore=IGNORE)
    shutil.copytree(original / "tcl", destination / "tcl", ignore=IGNORE)
    stdlib = Path(sysconfig.get_path("stdlib"))
    with zipfile.ZipFile(destination / f"{runtime_name}.zip", "w", zipfile.ZIP_DEFLATED) as archive:
        for base, dirs, names in os.walk(stdlib):
            dirs[:] = [d for d in dirs if d not in {"site-packages", "__pycache__", "test", "tests", "idlelib", "turtledemo", "ensurepip"}]
            for name in names:
                path = Path(base) / name
                if path.suffix != ".pyc":
                    archive.write(path, path.relative_to(stdlib).as_posix())
    package_root = Path(sysconfig.get_path("purelib")).resolve()
    for name in pinned_packages():
        distribution = metadata.distribution(name)
        for file in distribution.files or []:
            path = Path(distribution.locate_file(file)).resolve()
            if path.is_relative_to(package_root) and path.is_file() and path.suffix != ".pyc":
                copy_file(path, destination / "Lib" / "site-packages" / path.relative_to(package_root))
    (destination / f"{runtime_name}._pth").write_text(
        f"{runtime_name}.zip\nDLLs\nLib/site-packages\n..\nimport site\n", encoding="ascii")


def build(destination, outlook_client_id=None):
    destination = destination.resolve()
    if not destination.is_relative_to(ROOT / "dist"):
        raise ValueError("Output must stay in this project's dist directory")
    if destination.exists():
        raise ValueError("Output exists; choose a new name instead of overwriting")
    archive = destination.parent / f"InvoiceWorkbench-Windows-v{__version__}.zip"
    if archive.exists():
        raise ValueError("Archive already exists")
    application_id = (validate_client_id(outlook_client_id) if outlook_client_id is not None
                      else configured_client_id(ROOT))
    if not application_id:
        raise UserError(f"发布构建需要 Outlook OAuth 客户端 ID。请使用 --outlook-client-id、{CLIENT_ID_ENV} 或 {CLIENT_ID_FILE} 提供。")
    files = ("app.py", "README.md", "requirements.txt", "AGENTS.md", "setup.ps1", "start.ps1", "Install-ToSoft.ps1")
    directories = ("invoice_workbench", "samples", "tests", "tools")
    for name in files:
        if not (ROOT / name).is_file():
            raise FileNotFoundError(f"构建缺少必需文件：{ROOT / name}")
    for name in directories:
        if not (ROOT / name).is_dir():
            raise FileNotFoundError(f"构建缺少必需目录：{ROOT / name}")
    ofd = ROOT / "runtime-ofd"
    if not (ofd / "invoice-ofd.jar").is_file() or not (ofd / "java" / "bin" / "java.exe").is_file():
        raise FileNotFoundError("请先运行 tools/build_ofd.py 构建本地 OFD 转换组件。")
    destination.mkdir(parents=True)
    for name in files:
        copy_file(ROOT / name, destination / name)
    for name in directories:
        shutil.copytree(ROOT / name, destination / name, ignore=IGNORE)
    (destination / CLIENT_ID_FILE).write_text(application_id + "\n", encoding="ascii")
    copy_runtime(destination / "runtime")
    shutil.copytree(ofd, destination / "runtime-ofd")
    compiler = Path(os.environ.get("WINDIR", "C:/Windows")) / "Microsoft.NET" / "Framework64" / "v4.0.30319" / "csc.exe"
    subprocess.run([str(compiler), "/nologo", "/target:winexe", "/reference:System.Windows.Forms.dll",
                    f"/out:{destination / 'InvoiceWorkbench.exe'}", str(ROOT / "tools" / "Launcher.cs")], check=True)
    versions = {name: metadata.version(name) for name in pinned_packages()}
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
    parser.add_argument("--outlook-client-id", help="发布者注册的 Outlook OAuth 应用客户端 ID")
    args = parser.parse_args()
    try:
        build(args.output, args.outlook_client_id)
    except UserError as exc:
        parser.exit(1, f"{exc}\n")
