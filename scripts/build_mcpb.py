#!/usr/bin/env python3
"""Build dist/spoofzy-<version>.mcpb, the one-click Claude Desktop extension.

An .mcpb file is a zip archive with manifest.json at its root. This script also
checks that every manifest declares the same version as spoofzy_mcp.py.
Usage: python3 scripts/build_mcpb.py
"""
import json
import re
import sys
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
FILES = ["manifest.json", "spoofzy_mcp.py", "assets/icon.png", "README.md", "LICENSE"]


def main():
    version = re.search(r'^__version__ = "([^"]+)"', (ROOT / "spoofzy_mcp.py").read_text(), re.M).group(1)
    manifest = json.loads((ROOT / "manifest.json").read_text())
    declared = {
        "manifest.json": manifest["version"],
        ".claude-plugin/plugin.json": json.loads((ROOT / ".claude-plugin/plugin.json").read_text())["version"],
        ".claude-plugin/marketplace.json": json.loads(
            (ROOT / ".claude-plugin/marketplace.json").read_text())["plugins"][0]["version"],
        "pyproject.toml": re.search(r'^version = "([^"]+)"', (ROOT / "pyproject.toml").read_text(), re.M).group(1),
    }
    mismatched = {name: value for name, value in declared.items() if value != version}
    if mismatched:
        sys.exit("Version mismatch: spoofzy_mcp.py is {} but {}".format(version, mismatched))
    for field in ("icon",):
        if not (ROOT / manifest[field]).is_file():
            sys.exit("manifest.json {} {} is missing".format(field, manifest[field]))
    output = ROOT / "dist" / "spoofzy-{}.mcpb".format(version)
    output.parent.mkdir(exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as bundle:
        for name in FILES:
            info = zipfile.ZipInfo(name, date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = (0o755 if name.endswith(".py") else 0o644) << 16
            bundle.writestr(info, (ROOT / name).read_bytes())
    print("Built {} ({:,} bytes, version {})".format(output.relative_to(ROOT), output.stat().st_size, version))


if __name__ == "__main__":
    main()
