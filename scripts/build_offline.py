"""Build a Windows x64 plugin ZIP containing Python, dependencies, and Chrome."""

from __future__ import annotations

import argparse
import hashlib
import json
import platform
import shutil
import subprocess
import sys
import tempfile
import urllib.request
import zipfile
from email.parser import BytesParser
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SOURCE_FILES = (
    "__init__.py",
    "main.py",
    "runtime.py",
    "mcp_entry.py",
    "observer.py",
    "observation_queue.py",
    "owned_tool.py",
    "metadata.yaml",
    "_conf_schema.json",
    "requirements.txt",
    "README.md",
)


def download(url: str, cache_dir: Path) -> Path:
    """Download a pinned build input once and atomically cache it.

    Args:
        url: HTTPS URL of an official runtime or browser archive.
        cache_dir: Directory holding downloaded build inputs.

    Returns:
        Path to the complete cached archive.

    Raises:
        OSError: The download or local write fails.
    """
    destination = cache_dir / url.rsplit("/", 1)[-1]
    if destination.is_file():
        print(f"Using cached {destination.name}", flush=True)
        return destination
    print(f"Downloading {url}", flush=True)
    temporary = destination.with_suffix(destination.suffix + ".part")
    with (
        urllib.request.urlopen(url, timeout=60) as response,
        temporary.open("wb") as output,
    ):
        shutil.copyfileobj(response, output)
    temporary.replace(destination)
    return destination


def run_step(command: list[str]) -> None:
    """Run a build subprocess with visible diagnostics and no extra Windows console.

    Args:
        command: Executable and arguments for the build step.

    Raises:
        subprocess.CalledProcessError: The step exits unsuccessfully.
    """
    with subprocess.Popen(
        command,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        encoding="utf-8",
        errors="replace",
        creationflags=subprocess.CREATE_NO_WINDOW,
    ) as process:
        for line in process.stdout:
            print(line.rstrip(), flush=True)
        code = process.wait()
    if code:
        raise subprocess.CalledProcessError(code, command)


def main() -> None:
    """Resolve dependencies online and package an already installed private runtime.

    Raises:
        RuntimeError: The builder is not running on Windows x64.
        Exception: A download, wheel resolution, runtime check, or packaging fails.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--browser-use-version", default="0.13.11")
    parser.add_argument("--python-version", default="3.13.17")
    parser.add_argument("--chrome-version", default="156.0.8078.47")
    parser.add_argument("--pip-version", default="26.2.2")
    parser.add_argument("--source-only", action="store_true")
    args = parser.parse_args()
    output_dir = ROOT / "dist"
    output_dir.mkdir(exist_ok=True)
    source_zip = output_dir / f"{ROOT.name}-0.1.0-source.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        source_paths = [ROOT / name for name in SOURCE_FILES]
        source_paths.extend((ROOT / "scripts").glob("*.py"))
        for path in source_paths:
            archive.write(path, Path(ROOT.name) / path.relative_to(ROOT))
    if args.source_only:
        print(f"Source plugin ready: {source_zip}", flush=True)
        return
    if sys.platform != "win32" or platform.machine().lower() not in {"amd64", "x86_64"}:
        raise RuntimeError("Build this package on Windows x64.")
    for value in (
        args.browser_use_version,
        args.python_version,
        args.chrome_version,
        args.pip_version,
    ):
        if not value or not all(part.isdecimal() for part in value.split(".")):
            raise ValueError("Build versions must contain only dot-separated numbers.")

    build_dir = ROOT / ".build"
    cache_dir = build_dir / "downloads"
    cache_dir.mkdir(parents=True, exist_ok=True)
    python_archive = download(
        f"https://www.python.org/ftp/python/{args.python_version}/"
        f"python-{args.python_version}-embed-amd64.zip",
        cache_dir,
    )
    chrome_archive = download(
        f"https://storage.googleapis.com/chrome-for-testing-public/{args.chrome_version}/"
        "win64/chrome-win64.zip",
        cache_dir,
    )
    with tempfile.TemporaryDirectory(prefix="offline-", dir=build_dir) as temporary:
        staging = Path(temporary).resolve()
        if not staging.is_relative_to(build_dir.resolve()):
            raise RuntimeError("Build staging directory escaped the workspace.")
        plugin = staging / ROOT.name
        plugin.mkdir()
        for filename in SOURCE_FILES:
            shutil.copy2(ROOT / filename, plugin / filename)
        shutil.copytree(
            ROOT / "scripts",
            plugin / "scripts",
            ignore=shutil.ignore_patterns("__pycache__"),
        )
        runtime_dir = plugin / "runtime"
        python_dir = runtime_dir / "python"
        python_dir.mkdir(parents=True)
        with zipfile.ZipFile(python_archive) as archive:
            archive.extractall(python_dir)
        pth_path = next(python_dir.glob("python*._pth"))
        python_tag = "".join(args.python_version.split(".")[:2])
        pth_path.write_text(
            f"python{python_tag}.zip\n.\nLib/site-packages\nimport site\n",
            encoding="utf-8",
        )
        site_packages = python_dir / "Lib" / "site-packages"
        site_packages.mkdir(parents=True)
        python_exe = python_dir / "python.exe"
        # pip is used only while building; the installed plugin never installs packages.
        run_step(
            [
                sys.executable,
                "-m",
                "pip",
                "--disable-pip-version-check",
                "download",
                "--no-deps",
                "--only-binary=:all:",
                "--dest",
                str(cache_dir),
                f"pip=={args.pip_version}",
            ],
        )
        pip_wheel = cache_dir / f"pip-{args.pip_version}-py3-none-any.whl"
        with zipfile.ZipFile(pip_wheel) as archive:
            archive.extractall(site_packages)
        wheelhouse = staging / "wheelhouse"
        wheelhouse.mkdir()
        run_step(
            [
                str(python_exe),
                "-X",
                "utf8",
                "-m",
                "pip",
                "--disable-pip-version-check",
                "download",
                "--only-binary=:all:",
                "--dest",
                str(wheelhouse),
                f"browser-use=={args.browser_use_version}",
            ],
        )
        requirements = []
        inputs = {}
        for wheel in sorted(wheelhouse.glob("*.whl")):
            with zipfile.ZipFile(wheel) as archive:
                metadata_name = next(
                    name
                    for name in archive.namelist()
                    if name.endswith(".dist-info/METADATA")
                )
                metadata = BytesParser().parsebytes(archive.read(metadata_name))
            with wheel.open("rb") as stream:
                digest = hashlib.file_digest(stream, "sha256").hexdigest()
            requirements.append(
                f"{metadata['Name']}=={metadata['Version']} --hash=sha256:{digest}"
            )
            inputs[wheel.name] = digest
        lock_path = runtime_dir / "requirements.lock"
        lock_path.write_text("\n".join(requirements) + "\n", encoding="utf-8")
        run_step(
            [
                str(python_exe),
                "-X",
                "utf8",
                "-m",
                "pip",
                "--disable-pip-version-check",
                "install",
                "--no-index",
                "--find-links",
                str(wheelhouse),
                "--require-hashes",
                "--no-compile",
                "--only-binary=:all:",
                "--target",
                str(site_packages),
                "-r",
                str(lock_path),
            ],
        )
        run_step([str(python_exe), "-X", "utf8", "-m", "pip", "check"])
        with zipfile.ZipFile(chrome_archive) as archive:
            archive.extractall(staging / "chrome")
        shutil.copytree(staging / "chrome" / "chrome-win64", runtime_dir / "chromium")
        for item in (python_archive, chrome_archive, pip_wheel):
            with item.open("rb") as stream:
                inputs[item.name] = hashlib.file_digest(stream, "sha256").hexdigest()
        (runtime_dir / "manifest.json").write_text(
            json.dumps(
                {
                    "platform": "windows-x64",
                    "python": args.python_version,
                    "browser_use": args.browser_use_version,
                    "chrome": args.chrome_version,
                    "inputs_sha256": inputs,
                },
                indent=2,
            ),
            encoding="utf-8",
        )
        run_step(
            [
                sys.executable,
                str(ROOT / "scripts" / "smoke_test.py"),
                "--plugin-dir",
                str(plugin),
            ],
        )
        output_path = output_dir / f"{ROOT.name}-0.1.0-win-x64.zip"
        with zipfile.ZipFile(
            output_path, "w", zipfile.ZIP_DEFLATED, compresslevel=6
        ) as archive:
            for name in ("install.cmd", "install.ps1", "README.md"):
                archive.write(ROOT / name, name)
            for path in sorted(plugin.rglob("*")):
                if path.is_file() and "__pycache__" not in path.parts:
                    archive.write(path, path.relative_to(staging))
        with output_path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        output_path.with_suffix(".zip.sha256").write_text(
            f"{digest}  {output_path.name}\n", encoding="utf-8"
        )
        print(f"Offline plugin ready: {output_path}", flush=True)


if __name__ == "__main__":
    main()
