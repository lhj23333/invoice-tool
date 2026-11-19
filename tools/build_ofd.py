from argparse import ArgumentParser
import hashlib
import os
from pathlib import Path
import shutil
import subprocess
from tempfile import TemporaryDirectory
from zipfile import ZipFile

import requests


ROOT = Path(__file__).resolve().parent.parent
MAVEN = "https://repo.maven.apache.org/maven2/org/apache/maven/apache-maven/3.9.11/apache-maven-3.9.11-bin.zip"
JAVA = ("https://github.com/adoptium/temurin21-binaries/releases/download/jdk-21.0.12.1%2B1/"
        "OpenJDK21U-jdk_x64_windows_hotspot_21.0.12.1_1.zip")


def _install_tool(url, algorithm, directory):
    with requests.get(url + "." + algorithm + (".txt" if url == JAVA else ""), timeout=30) as response:
        response.raise_for_status()
        expected = response.text.split()[0].lower()
    archive = directory / url.rsplit("/", 1)[-1]
    digest = hashlib.new(algorithm)
    with requests.get(url, timeout=(15, 60), stream=True) as response, archive.open("wb") as file:
        response.raise_for_status()
        for chunk in response.iter_content(256 * 1024):
            digest.update(chunk)
            file.write(chunk)
    if digest.hexdigest() != expected:
        raise ValueError(f"构建工具校验失败：{archive.name}")
    with ZipFile(archive) as bundle:
        top = {name.partition("/")[0] for name in bundle.namelist() if name}
        if len(top) != 1:
            raise ValueError("构建工具归档目录结构无效。")
        bundle.extractall(directory)
        return directory / top.pop()


def build(rebuild=False):
    destination = ROOT / "runtime-ofd"
    specification = ROOT / "tools" / "ofd"
    version = hashlib.sha256((specification / "pom.xml").read_bytes() + (specification / "InvoiceOFD.java").read_bytes()).hexdigest()
    marker = destination / "source.sha256"
    if not rebuild and marker.is_file() and marker.read_text(encoding="ascii") == version and (destination / "java" / "bin" / "java.exe").is_file():
        return
    cache = ROOT / ".test-tmp"
    cache.mkdir(exist_ok=True)
    with TemporaryDirectory(prefix="ofd-build-", dir=cache) as path:
        path = Path(path)
        maven = _install_tool(MAVEN, "sha512", path)
        jdk = _install_tool(JAVA, "sha256", path)
        project = path / "project"
        shutil.copytree(specification, project)
        env = {**os.environ, "JAVA_HOME": str(jdk)}
        subprocess.run([str(maven / "bin" / "mvn.cmd"), "-B", "-ntp", f"-Dmaven.repo.local={path / 'maven-repository'}",
                        "-DincludeScope=runtime", "package", "dependency:copy-dependencies"], cwd=project, env=env, check=True)
        bundle = path / "runtime-ofd"
        bundle.mkdir()
        shutil.copy2(project / "target" / "invoice-ofd.jar", bundle)
        shutil.copytree(project / "target" / "dependency", bundle / "lib")
        shutil.copy2(project / "pom.xml", bundle / "dependency-versions.xml")
        modules = "java.desktop,java.logging,java.management,java.naming,java.scripting,java.sql,jdk.charsets,jdk.unsupported"
        subprocess.run([str(jdk / "bin" / "jlink.exe"), "--add-modules", modules, "--strip-debug", "--no-header-files",
                        "--no-man-pages", "--output", str(bundle / "java")], check=True)
        (bundle / marker.name).write_text(version, encoding="ascii")
        previous = path / "previous-runtime"
        if destination.exists():
            destination.rename(previous)
        try:
            shutil.copytree(bundle, destination)
        except OSError:
            if destination.exists():
                destination.rename(path / "failed-runtime")
            if previous.exists():
                previous.rename(destination)
            raise


if __name__ == "__main__":
    parser = ArgumentParser(description="构建本地 OFD 转换运行环境")
    parser.add_argument("--rebuild", action="store_true")
    build(parser.parse_args().rebuild)
