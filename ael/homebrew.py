"""Render the Homebrew formula from immutable AEL release inputs."""

from __future__ import annotations

import argparse
import re
from pathlib import Path


VERSION_RE = re.compile(r"^\d+\.\d+\.\d+(?:-[0-9A-Za-z.-]+)?$")
SHA_RE = re.compile(r"^[0-9a-f]{64}$")
LICENSE_RE = re.compile(r"^[A-Za-z0-9.+-]+$")


def render(template: str, version: str, sha256: str, license_id: str) -> str:
    if not VERSION_RE.fullmatch(version):
        raise ValueError("version must be SemVer without a leading v")
    if not SHA_RE.fullmatch(sha256):
        raise ValueError("sha256 must be 64 lowercase hexadecimal characters")
    if not LICENSE_RE.fullmatch(license_id):
        raise ValueError("license must be a single SPDX identifier")
    result = (
        template.replace("@VERSION@", version)
        .replace("@SHA256@", sha256)
        .replace("@LICENSE@", license_id)
    )
    unresolved = sorted(set(re.findall(r"@[A-Z0-9_]+@", result)))
    if unresolved:
        raise ValueError(f"unresolved formula placeholders: {unresolved}")
    return result


def main() -> int:
    source_root = Path(__file__).resolve().parent.parent
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--template",
        type=Path,
        default=source_root / "packaging" / "homebrew" / "ael.rb.in",
    )
    parser.add_argument("--version", required=True)
    parser.add_argument("--sha256", required=True)
    parser.add_argument("--license", required=True, dest="license_id")
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    rendered = render(
        args.template.read_text(), args.version, args.sha256, args.license_id
    )
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(rendered)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
