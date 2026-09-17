#!/usr/bin/env python3
"""Validate the HACS integration layout and manifest.

Catches the regression where the integration package is moved out of the
``custom_components/<domain>/`` directory HACS expects.  The download error
``No manifest.json file found 'custom_components/None/manifest.json'`` occurs
when HACS cannot resolve the integration domain from the repo layout.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

REQUIRED_MANIFEST_KEYS = ("domain", "name", "version")
CUSTOM_COMPONENTS = Path("custom_components")
HACS_JSON = Path("hacs.json")


def _load_json(path: Path) -> dict:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        sys.exit(f"ERROR: {path} not found")
    except json.JSONDecodeError as exc:
        sys.exit(f"ERROR: {path} is not valid JSON: {exc}")


def main() -> None:
    errors: list[str] = []

    if not HACS_JSON.exists():
        errors.append("hacs.json not found at repository root")
        hacs = {}
    else:
        hacs = _load_json(HACS_JSON)

    # When content_in_root is true the manifest lives at the repo root.
    # Otherwise it must live under custom_components/<domain>/manifest.json.
    content_in_root = bool(hacs.get("content_in_root", False))

    if content_in_root:
        manifest_path = Path("manifest.json")
        if not manifest_path.exists():
            errors.append(
                "hacs.json has content_in_root=true but manifest.json is "
                "missing at the repository root"
            )
    else:
        if not CUSTOM_COMPONENTS.exists():
            errors.append(
                "custom_components/ directory not found; HACS expects the "
                "integration under custom_components/<domain>/"
            )
        integrations = sorted(
            p
            for p in CUSTOM_COMPONENTS.glob("*/manifest.json")
            if p.is_file()
        )
        if not integrations:
            errors.append(
                "No manifest.json found under custom_components/<domain>/; "
                "HACS cannot resolve the integration (this produced the "
                "'custom_components/None/manifest.json' download error)"
            )

    if content_in_root:
        manifests = [Path("manifest.json")]
    else:
        manifests = integrations if "integrations" in locals() else []
    for manifest_path in manifests:
        if not manifest_path or not Path(manifest_path).exists():
            continue
        manifest = _load_json(Path(manifest_path))
        domain = manifest.get("domain")
        if not domain:
            errors.append(f"{manifest_path}: missing 'domain' key")
            continue
        # The manifest must sit in custom_components/<domain>/ matching domain.
        if not content_in_root:
            expected_dir = CUSTOM_COMPONENTS / domain
            actual_dir = Path(manifest_path).parent
            if actual_dir != expected_dir:
                errors.append(
                    f"{manifest_path}: domain '{domain}' must live under "
                    f"{expected_dir}/ (found {actual_dir}/)"
                )
        missing = [k for k in REQUIRED_MANIFEST_KEYS if k not in manifest]
        if missing:
            errors.append(f"{manifest_path}: missing required keys: {missing}")
        # The integration entry point (__init__.py) must exist alongside.
        if not content_in_root:
            init_path = Path(manifest_path).parent / "__init__.py"
            if not init_path.exists():
                errors.append(f"{init_path}: integration __init__.py missing")

    if errors:
        print("HACS layout validation failed:")
        for err in errors:
            print(f"  - {err}")
        sys.exit(1)

    print("HACS layout validation passed.")


if __name__ == "__main__":
    main()
