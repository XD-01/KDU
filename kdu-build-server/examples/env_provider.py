"""Demo platform-config provider for the KDU build service.

Configure it in ``config/service.json``::

    "build": { "provider": "examples/env_provider.py", ... }

or override it per process with ``KDU_PROVIDER=examples/env_provider.py``.

The provider reads environment variables and returns a partial configuration
that is merged over the JSON platform profiles:

    KDU_MSBUILD_PLATFORM   e.g. x64
    KDU_CONFIGURATION      e.g. Release / Debug
    KDU_PLATFORM_TOOLSET   e.g. v145 / v143
    KDU_EXTRA_PROPERTIES   "RunCodeAnalysis=false;SpectreMitigation=false"

Run it directly to print the configuration it resolves::

    python examples/env_provider.py windows-x64
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

SERVICE_ROOT = Path(__file__).resolve().parents[1]
if str(SERVICE_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVICE_ROOT))

from kdu_build_server.platform_config import (  # noqa: E402
    PlatformConfigProvider,
    default_platform,
    resolve_platform_config,
)

PLATFORMS_FILE = SERVICE_ROOT / "config" / "platforms.json"


class EnvPlatformProvider(PlatformConfigProvider):
    """Read platform overrides from the process environment."""

    def get_platform_config(self, platform: str, context: dict) -> dict:
        overrides: dict = {}
        mapping = {
            "KDU_MSBUILD_PLATFORM": "msbuild_platform",
            "KDU_CONFIGURATION": "configuration",
            "KDU_PLATFORM_TOOLSET": "platform_toolset",
        }
        for variable, key in mapping.items():
            value = os.environ.get(variable)
            if value:
                overrides[key] = value

        raw_properties = os.environ.get("KDU_EXTRA_PROPERTIES")
        if raw_properties:
            properties = {}
            for item in raw_properties.split(";"):
                item = item.strip()
                if not item:
                    continue
                if "=" not in item:
                    raise ValueError(
                        f"KDU_EXTRA_PROPERTIES entry {item!r} must look like KEY=VALUE"
                    )
                key, value = item.split("=", 1)
                properties[key.strip()] = value.strip()
            overrides["extra_properties"] = properties

        if os.environ.get("KDU_PROVIDER_DEBUG"):
            print(
                f"[env_provider] platform={platform} repo_root={context.get('repo_root')} "
                f"base_config={context['base_config'].get('configuration')}",
                file=sys.stderr,
            )
        return overrides


# The loader prefers a module-level Provider class, then falls back to a plain
# get_platform_config(platform, context) function.
Provider = EnvPlatformProvider


if __name__ == "__main__":
    selected = sys.argv[1] if len(sys.argv) > 1 else default_platform(PLATFORMS_FILE)
    resolved = resolve_platform_config(
        platform=selected,
        platforms_file=PLATFORMS_FILE,
        provider=Path(__file__).resolve(),
        repo_root=SERVICE_ROOT,
    )
    print(json.dumps(resolved, indent=2, sort_keys=True))
