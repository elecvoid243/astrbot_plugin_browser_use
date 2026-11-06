"""Inspect the relocatable private runtime, codecs, native libraries and notices."""

from __future__ import annotations

import argparse
import ctypes
import hashlib
import importlib.metadata
import json
import sys
from pathlib import Path


def main():
    """Verify imports and the software encoder entirely inside this runtime.

    Raises:
        RuntimeError: A dependency escapes the private runtime or is incompatible.
    """
    import aiortc
    import av
    import browser_use
    from browser_use.mcp.server import BrowserUseServer  # noqa: F401

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--report", type=Path)
    args = parser.parse_args()
    prefix = Path(sys.executable).resolve().parent
    packages = []
    for dist in importlib.metadata.distributions():
        files = dist.files or []
        notices = [
            str(Path(dist.locate_file(path)).resolve().relative_to(prefix))
            for path in files
            if any(
                word in str(path).lower() for word in ("license", "copying", "notice")
            )
        ]
        packages.append(
            {
                "name": dist.metadata["Name"],
                "version": dist.version,
                "license": dist.metadata.get("License-Expression")
                or dist.metadata.get("License"),
                "notices": notices,
            }
        )
    for module in (aiortc, av, browser_use):
        if not Path(module.__file__).resolve().is_relative_to(prefix):
            raise RuntimeError(
                f"Private dependency escaped the runtime: {module.__name__}"
            )
    if aiortc.__version__ != "1.14.0" or av.__version__ != "16.0.1":
        raise RuntimeError("Unverified media runtime versions.")
    encoder = av.CodecContext.create("libvpx", "w")
    encoder.width, encoder.height = 1920, 1080
    encoder.pix_fmt = "yuv420p"
    from fractions import Fraction

    encoder.time_base = Fraction(1, 30)
    frame = av.VideoFrame(1920, 1080, "yuv420p")
    packets = encoder.encode(frame) + encoder.encode(None)
    if not packets:
        raise RuntimeError("The private VP8 encoder produced no packet.")
    site = prefix / "Lib/site-packages"
    native = {
        str(path.relative_to(prefix)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(prefix.rglob("*"))
        if path.suffix.lower() in {".dll", ".pyd", ".exe"}
    }
    library = ctypes.CDLL(str(next((site / "av.libs").glob("avcodec-*.dll"))))
    library.avcodec_license.restype = ctypes.c_char_p
    library.avcodec_configuration.restype = ctypes.c_char_p
    report = {
        "python": sys.version,
        "executable": str(Path(sys.executable).resolve()),
        "sys_path": sys.path,
        "aiortc": aiortc.__version__,
        "av": av.__version__,
        "ffmpeg": av.ffmpeg_version_info,
        "ffmpeg_libraries": av.library_versions,
        "ffmpeg_license": library.avcodec_license().decode(),
        "ffmpeg_configuration": library.avcodec_configuration().decode(),
        "ffmpeg_source": "https://ffmpeg.org/releases/ffmpeg-8.0.tar.xz",
        "encoder": "libvpx",
        "encoded_dimensions": [encoder.width, encoder.height],
        "packages": sorted(packages, key=lambda package: package["name"].lower()),
        "native_sha256": native,
    }
    if args.report:
        args.report.write_text(json.dumps(report, indent=2), encoding="utf-8")
    print(
        json.dumps(
            {
                "private_runtime_verified": True,
                "packages": len(packages),
                "native_libraries": len(native),
                "encoder": "libvpx",
                "dimensions": [1920, 1080],
            }
        )
    )


if __name__ == "__main__":
    main()
