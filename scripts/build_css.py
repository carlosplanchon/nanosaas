#!/usr/bin/env python3
"""
Build web/static/tailwind.css with the classes used in web/templates.
Run it after changing the templates. It uses Tailwind's standalone CLI,
which doesn't need Node.js: the first run downloads it to ~/.cache.
"""
import hashlib
import os
import platform
import subprocess
import urllib.request
from pathlib import Path

VERSION = "v3.4.17"
# From the release's sha256sums.txt.
SHA256 = {
    "linux-x64": "7d24f7fa191d2193b78cd5f5a42a6093e14409521908529f42d80b11fde1f1d4",
    "linux-arm64": "69b1378b8133192d7d2feb12a116fa12d035594f58db3eff215879e4ad8cf39b",
    "macos-x64": "6cbdad74be776c087ffa5e9a057512c54898f9fe8828d3362212dfe32fc933a3",
    "macos-arm64": "a1d0c7985759accca0bf12e51ac1dcbf0f6cf2fffb62e6e0f62d091c477a10a3",
    "windows-x64.exe": "67f1c5e3f5a03406a7bf5badf5ada09b79f3ae78ec43450c15f7e983068da346",
    "windows-arm64.exe": "76f516476784c00f1562160b5758e3d8f0e6c48957efb26b5b50fbdfd76aa382",
}
REPO = Path(__file__).resolve().parent.parent


def tailwind_cli() -> Path:
    system = {"Linux": "linux", "Darwin": "macos", "Windows": "windows"}.get(platform.system())
    machine = {"x86_64": "x64", "AMD64": "x64", "aarch64": "arm64", "arm64": "arm64", "ARM64": "arm64"}.get(
        platform.machine()
    )
    name = f"{system}-{machine}" + (".exe" if system == "windows" else "")
    if name not in SHA256:
        raise SystemExit(f"Tailwind has no standalone CLI for {platform.system()} {platform.machine()}.")

    cli = Path.home() / ".cache" / "tailwindcss" / f"tailwindcss-{VERSION}-{name}"
    if not cli.exists():
        url = f"https://github.com/tailwindlabs/tailwindcss/releases/download/{VERSION}/tailwindcss-{name}"
        data = urllib.request.urlopen(url).read()
        if hashlib.sha256(data).hexdigest() != SHA256[name]:
            raise SystemExit(f"{url} doesn't match its checksum.")
        cli.parent.mkdir(parents=True, exist_ok=True)
        cli.write_bytes(data)
        cli.chmod(0o755)
    return cli


if __name__ == "__main__":
    subprocess.run(
        [tailwind_cli(), "--content", "web/templates/**/*.html", "-o", "web/static/tailwind.css", "--minify"],
        cwd=REPO,
        check=True,
        # Without it, the CLI suggests updating its browser list, which it can't do.
        env={**os.environ, "BROWSERSLIST_IGNORE_OLD_DATA": "1"},
    )
