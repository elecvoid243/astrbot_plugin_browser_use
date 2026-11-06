"""Check the distributable source payload, including private observer imports."""

import ast
import hashlib
import json
import subprocess
import sys
import zipfile
from pathlib import Path


def test_source_zip_contains_local_imports_and_runtime_lock():
    """Build the real source archive and resolve all plugin-local module imports."""
    root = Path(__file__).resolve().parents[1]
    subprocess.run(
        [sys.executable, str(root / "scripts/build_offline.py"), "--source-only"],
        check=True,
    )
    version = next(
        line.split(":", 1)[1].strip().removeprefix("v")
        for line in (root / "metadata.yaml").read_text(encoding="utf-8").splitlines()
        if line.startswith("version:")
    )
    archive_path = root / "dist" / f"{root.name}-{version}-source.zip"
    with zipfile.ZipFile(archive_path) as archive:
        names = set(archive.namelist())
        for member in names:
            if member.endswith(".py"):
                for node in ast.walk(ast.parse(archive.read(member))):
                    if isinstance(node, ast.ImportFrom) and node.level and node.module:
                        assert f"{root.name}/{node.module}.py" in names
        assert f"{root.name}/observer.py" in names
        assert f"{root.name}/observation_queue.py" in names
        for module in (
            "browser_capture",
            "browser_media",
            "frame_coordinates",
            "frame_dom",
            "frame_records",
            "media_rtp",
            "media_track",
        ):
            assert f"{root.name}/{module}.py" in names
        notices = f"{root.name}/scripts/licenses/"
        provenance = json.loads(archive.read(notices + "native-provenance.json"))
        for filename, entry in provenance["license_texts"].items():
            assert (
                hashlib.sha256(archive.read(notices + filename)).hexdigest()
                == entry["sha256"]
            )
        lock = archive.read(f"{root.name}/scripts/requirements-win-x64.lock").decode()
        assert "browser-use==0.13.11 --hash=sha256:" in lock
        assert not any("runtime/python" in name for name in names)
