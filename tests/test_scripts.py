import json
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parent.parent
POWERSHELL = shutil.which("powershell.exe")


def ps_literal(value):
    return "'" + str(value).replace("'", "''") + "'"


@unittest.skipUnless(os.name == "nt" and POWERSHELL, "需要 Windows PowerShell")
class ScriptTests(unittest.TestCase):
    @classmethod
    def resolve(cls, project, python=None, env=None):
        command = (f". {ps_literal(ROOT / 'tools' / 'python_runtime.ps1')}; "
                   f"Get-WorkbenchPython -ProjectRoot {ps_literal(project)} -Setup")
        if python:
            command += f" -Python {ps_literal(python)}"
        command += " | ConvertTo-Json -Compress"
        return subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-Command", command],
                              capture_output=True, env=env)

    @classmethod
    def setUpClass(cls):
        probe = cls.resolve(ROOT)
        if probe.returncode:
            raise unittest.SkipTest("没有可用的 Windows Python >= 3.11 与 Tcl/Tk")
        cls.base = Path(json.loads(probe.stdout)["executable"])
        cls.temp = tempfile.TemporaryDirectory(prefix="python environment ")
        cls.addClassCleanup(cls.temp.cleanup)
        cls.project = Path(cls.temp.name)
        subprocess.run([str(cls.base), "-B", "-m", "venv", "--without-pip", str(cls.project / ".venv")],
                       check=True, capture_output=True)
        info = cls.resolve(cls.project)
        if info.returncode:
            raise AssertionError(info.stderr.decode(errors="replace"))
        cls.python = Path(json.loads(info.stdout)["executable"])
        if Path(json.loads(info.stdout)["prefix"]) != cls.project / ".venv":
            raise AssertionError("未使用刚创建的项目虚拟环境")

    def test_project_environment_with_spaces_precedes_path(self):
        result = self.resolve(self.project)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.python)

    def test_explicit_python_precedes_project_environment(self):
        result = self.resolve(self.project, self.base)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.base)

    def test_versioned_python_command_is_supported(self):
        versioned = shutil.which("python3.12.exe")
        if not versioned:
            self.skipTest("没有其他版本的 Python 命令可用于验证")
        result = self.resolve(self.project, versioned)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertGreaterEqual(json.loads(result.stdout)["version"], [3, 11])

    def test_active_environment_is_discovered(self):
        env = dict(os.environ, VIRTUAL_ENV=str(self.project / ".venv"))
        result = self.resolve(self.project / "another project", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.python)

    def test_path_python_is_found_without_launcher(self):
        env = dict(os.environ, PATH=str(self.base.parent))
        env.pop("VIRTUAL_ENV", None)
        env.pop("CONDA_PREFIX", None)
        result = self.resolve(self.project / "empty project", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.base)

    def test_command_name_can_be_explicitly_selected(self):
        env = dict(os.environ, PATH=str(self.python.parent))
        result = self.resolve(self.project, "python", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.python)

    def test_registered_python_is_found_without_path(self):
        env = dict(os.environ, PATH=str(self.project / "empty path"))
        env.pop("VIRTUAL_ENV", None)
        env.pop("CONDA_PREFIX", None)
        result = self.resolve(self.project / "empty project", env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(Path(json.loads(result.stdout)["executable"]).is_file())

    def test_windowed_python_uses_matching_console(self):
        windowed = self.python.with_name("pythonw.exe")
        self.assertTrue(windowed.is_file())
        result = self.resolve(self.project, windowed)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(Path(json.loads(result.stdout)["executable"]), self.python)

    def test_missing_explicit_python_fails_without_switching(self):
        missing = self.project / "missing-python.exe"
        result = self.resolve(self.project, missing)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"missing-python.exe", result.stderr)
        self.assertEqual(result.stdout, b"")

    def test_start_reports_missing_dependencies_before_launch(self):
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File", str(ROOT / "start.ps1"),
                                 "-Python", str(self.python), "-CheckEnvironment"], capture_output=True)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b"ModuleNotFoundError", result.stderr)
        self.assertIn(b"pypdf", result.stderr)

    def test_setup_rejects_another_python_without_changing_environment(self):
        other = shutil.which("python3.12.exe")
        if not other:
            self.skipTest("没有另一个 Python 安装可用于验证环境切换")
        before = json.loads(self.resolve(self.project).stdout)
        other_info = json.loads(self.resolve(self.project, other).stdout)
        if Path(before["base_prefix"]) == Path(other_info["base_prefix"]):
            self.skipTest("当前两个命令属于同一 Python 安装")
        for folder, names in {"tools": ["python_runtime.ps1", "check_python.py"],
                              "invoice_workbench": ["__init__.py", "runtime.py"]}.items():
            (self.project / folder).mkdir(exist_ok=True)
            for name in names:
                shutil.copy2(ROOT / folder / name, self.project / folder / name)
        shutil.copy2(ROOT / "setup.ps1", self.project / "setup.ps1")
        result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                                 str(self.project / "setup.ps1"), "-Python", other],
                                capture_output=True, timeout=30)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn(b".venv", result.stderr)
        after = json.loads(self.resolve(self.project).stdout)
        self.assertEqual(after["base_prefix"], before["base_prefix"])
        self.assertEqual(after["version"], before["version"])

    def test_setup_initializes_pip_when_reusing_environment(self):
        test_root = ROOT / ".test-tmp"
        test_root.mkdir(exist_ok=True)
        with tempfile.TemporaryDirectory(prefix="setup without pip ", dir=test_root) as directory:
            project = Path(directory)
            for name in ("tools", "invoice_workbench"):
                shutil.copytree(ROOT / name, project / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
            for name in ("setup.ps1", "start.ps1", "requirements.txt"):
                shutil.copy2(ROOT / name, project / name)
            subprocess.run([str(self.base), "-B", "-m", "venv", "--without-pip", str(project / ".venv")],
                           check=True, capture_output=True, timeout=30)
            info = json.loads(self.resolve(project).stdout)
            python = info["executable"]
            before = subprocess.run([python, "-B", "-c",
                                     "import importlib.util; print(importlib.util.find_spec('pip'))"],
                                    check=True, capture_output=True, timeout=10)
            self.assertEqual(before.stdout.strip(), b"None")
            config = project / ".venv" / "pyvenv.cfg"
            original_config = config.read_bytes()
            env = dict(os.environ, PIP_DISABLE_PIP_VERSION_CHECK="1", PIP_NO_CACHE_DIR="1")
            for _ in range(2):
                result = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                                         str(project / "setup.ps1")],
                                        capture_output=True, env=env, timeout=180)
                self.assertEqual(result.returncode, 0, result.stderr.decode(errors="replace"))
                self.assertEqual(config.read_bytes(), original_config)
                installed = subprocess.run([python, "-B", "-m", "pip", "--version"],
                                           check=True, capture_output=True, env=env, timeout=10)
                self.assertIn(str(project / ".venv").encode(), installed.stdout)
            check = subprocess.run([POWERSHELL, "-NoProfile", "-NonInteractive", "-File",
                                    str(project / "start.ps1"), "-CheckEnvironment"],
                                   capture_output=True, env=env, timeout=30)
            self.assertEqual(check.returncode, 0, check.stderr.decode(errors="replace"))
            self.assertIn(python.encode(), check.stdout)


if __name__ == "__main__":
    unittest.main()
