import hashlib
import importlib.metadata as metadata
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import sysconfig
import tempfile
import unittest
import zipfile

from tools.build_portable import build


ROOT = Path(__file__).resolve().parent.parent


@unittest.skipUnless(os.name == "nt" and sysconfig.get_platform().startswith("win"),
                     "便携包构建需要标准 Windows CPython")
class PortableTests(unittest.TestCase):
    def test_build_launch_preview_and_dependency_licenses(self):
        dist = ROOT / "dist"
        existed = dist.exists()
        dist.mkdir(exist_ok=True)
        if not existed:
            self.addCleanup(dist.rmdir)
        with tempfile.TemporaryDirectory(prefix=".test-portable-", dir=dist) as directory:
            destination = Path(directory) / "Invoice Workbench"
            build(destination)
            manifest = json.loads((destination / "bundle-manifest.json").read_text(encoding="utf-8"))
            self.assertEqual(set(manifest), {p.relative_to(destination).as_posix()
                                            for p in destination.rglob("*")
                                            if p.is_file() and p.name != "bundle-manifest.json"})
            for name, digest in manifest.items():
                self.assertEqual(hashlib.sha256((destination / name).read_bytes()).hexdigest(), digest, name)
            archives = list(Path(directory).glob("*.zip"))
            self.assertEqual(len(archives), 1)
            with zipfile.ZipFile(archives[0]) as archive:
                self.assertIsNone(archive.testzip())
                self.assertEqual(set(archive.namelist()),
                                 {f"{destination.name}/{name}" for name in manifest}
                                 | {f"{destination.name}/bundle-manifest.json"})
            runtime = destination / "runtime" / "python.exe"
            environment = dict(os.environ, PYTHONDONTWRITEBYTECODE="1")
            for name in ("PYTHONHOME", "PYTHONPATH", "TCL_LIBRARY", "TK_LIBRARY"):
                environment.pop(name, None)
            identity = subprocess.run([str(runtime), "-B", "-c",
                                       "import json,sys,_tkinter; print(json.dumps(dict(prefix=sys.prefix,version=list(sys.version_info[:2]),tk=_tkinter.TK_VERSION)))"],
                                      cwd=destination, env=environment, capture_output=True, check=True, timeout=30)
            info = json.loads(identity.stdout)
            self.assertEqual(Path(info["prefix"]), destination / "runtime")
            self.assertEqual(info["version"], list(sys.version_info[:2]))

            preview = subprocess.run([str(runtime), "-B", "-c",
                                      "from invoice_workbench.runtime import configure_bundled_tk; configure_bundled_tk(); "
                                      "import unittest; unittest.main(module=None,argv=['unittest','tests.test_gui','-v'])"],
                                     cwd=destination, env=environment, capture_output=True, timeout=60)
            self.assertEqual(preview.returncode, 0, preview.stderr.decode(errors="replace"))
            subprocess.run([str(destination / "InvoiceWorkbench.exe"), "--help"],
                           cwd=directory, env=environment, check=True, timeout=30)
            check = subprocess.run([shutil.which("powershell.exe"), "-NoProfile", "-NonInteractive", "-File",
                                    str(destination / "start.ps1"), "-CheckEnvironment"],
                                   cwd=directory, env=environment, capture_output=True, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr.decode(errors="replace"))
            self.assertIn(str(runtime).encode(), check.stdout)

            package_root = Path(sysconfig.get_path("purelib")).resolve()
            for name in ("pypdf", "Pillow", "pypdfium2"):
                distribution = metadata.distribution(name)
                licenses = [file for file in distribution.files or []
                            if any(part.lower().startswith(("license", "copying")) for part in file.parts)]
                self.assertTrue(licenses, name)
                for license_file in licenses:
                    original = Path(distribution.locate_file(license_file)).resolve()
                    stored = destination / "runtime" / "Lib" / "site-packages" / original.relative_to(package_root)
                    self.assertEqual(hashlib.sha256(stored.read_bytes()).digest(),
                                     hashlib.sha256(original.read_bytes()).digest())

            tk_library = destination / "runtime" / "tcl" / f"tk{info['tk']}"
            tk_library.rename(tk_library.with_name(tk_library.name + ".unavailable"))
            broken = subprocess.run([str(runtime), "-B", str(destination / "app.py"), "--help"],
                                    cwd=directory, env=environment, capture_output=True, timeout=30)
            self.assertNotEqual(broken.returncode, 0)
            self.assertIn("FileNotFoundError", (destination / "startup-error.log").read_text(encoding="utf-8"))


if __name__ == "__main__":
    unittest.main()
