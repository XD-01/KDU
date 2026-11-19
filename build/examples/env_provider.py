"""Demo platform-config provider for ``build/build_kdu.py``.

This file shows the smallest useful implementation of the provider interface:
it reads environment variables and returns a *partial* platform config, which
the build script merges over its defaults and ``platforms.json``.

Supported environment variables (all optional):

    KDU_MSBUILD_PLATFORM   MSBuild platform, e.g. x64
    KDU_CONFIGURATION      MSBuild configuration, e.g. Release / Debug
    KDU_PLATFORM_TOOLSET   MSBuild PlatformToolset, e.g. v145 / v143
    KDU_DIST_DIR           artifact output directory, supports {platform},
                           {msbuild_platform}, {configuration}
    KDU_EXTRA_PROPERTIES   semicolon-separated MSBuild properties, e.g.
                           "RunCodeAnalysis=false;SpectreMitigation=false"

Usage with the build script::

    python build/build_kdu.py --provider build/examples/env_provider.py \
        --platform windows-x64 --dry-run

Standalone demo (prints the configuration the provider produces)::

    python build/examples/env_provider.py windows-x64
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# Make ``platform_config`` importable when this file is executed directly.
BUILD_DIR = Path(__file__).resolve().parents[1]
if str(BUILD_DIR) not in sys.path:
    sys.path.insert(0, str(BUILD_DIR))

from platform_config import PlatformConfigProvider, resolve_platform_config  # noqa: E402


class EnvPlatformProvider(PlatformConfigProvider):
    """Reads platform overrides from the process environment."""

    def get_platform_config(self, platform: str, context: dict) -> dict:
        overrides: dict = {}

        mapping = {
            "KDU_MSBUILD_PLATFORM": "msbuild_platform",
            "KDU_CONFIGURATION": "configuration",
            "KDU_PLATFORM_TOOLSET": "platform_toolset",
            "KDU_DIST_DIR": "dist_dir",
        }
        for variable, key in mapping.items():
            value = os.environ.get(variable)
            if value:
                overrides[key] = value

        properties = os.environ.get("KDU_EXTRA_PROPERTIES")
        if properties:
            extra_properties = {}
            for item in properties.split(";"):
                item = item.strip()
                if not item:
                    continue
                if "=" not in item:
                    raise ValueError(
                        f"KDU_EXTRA_PROPERTIES entry {item!r} must look like KEY=VALUE"
                    )
                key, value = item.split("=", 1)
                extra_properties[key.strip()] = value.strip()
            overrides["extra_properties"] = extra_properties

        # The context is available if a provider needs more information, e.g.
        # context["base_config"], context["repo_root"] or context["env"].
        if context.get("repo_root") and os.environ.get("KDU_VERBOSE_DEMO"):
            print(f"[demo] repo_root={context['repo_root']} platform={platform}", file=sys.stderr)

        return overrides


# The loader looks for a module-level ``Provider`` class first. A plain
# ``get_platform_config(platform, context)`` function is also accepted.
Provider = EnvPlatformProvider


if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) > 1 else "windows-x64"
    resolved = resolve_platform_config(
        platform=selected,
        config_path=BUILD_DIR / "platforms.json",
        provider_path=Path(__file__).resolve(),
        repo_root=BUILD_DIR.parent,
    )
    print(json.dumps(resolved, indent=2, sort_keys=True))
