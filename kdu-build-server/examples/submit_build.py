#!/usr/bin/env python3
"""Example client: submit a build request and wait for the result.

    python examples/submit_build.py --url http://127.0.0.1:8600 \
        --token change-me --platform windows-x64 --wait --download kdu.zip

Only the Python standard library is required.
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


def request_json(url: str, token: str, payload: dict | None = None) -> dict:
    data = json.dumps(payload).encode("utf-8") if payload is not None else None
    headers = {"Accept": "application/json", "User-Agent": "kdu-example-client/1.0"}
    if payload is not None:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = Request(url, data=data, headers=headers, method="POST" if data else "GET")
    with urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Submit a build to the KDU build service")
    parser.add_argument("--url", default="http://127.0.0.1:8600")
    parser.add_argument("--token", default="", help="API bearer token, if the service requires one")
    parser.add_argument("--platform", default="windows-x64")
    parser.add_argument("--configuration", help="Override MSBuild configuration")
    parser.add_argument("--toolset", help="Override PlatformToolset")
    parser.add_argument("--property", action="append", default=[], metavar="KEY=VALUE")
    parser.add_argument("--callback-url", help="URL notified when the build finishes")
    parser.add_argument("--wait", action="store_true", help="Poll until the job finishes")
    parser.add_argument("--download", metavar="ZIP", help="Save the artifact archive to this path")
    parser.add_argument("--timeout", type=int, default=3600, help="Wait timeout in seconds")
    args = parser.parse_args(argv)

    properties = {}
    for item in args.property:
        if "=" not in item:
            parser.error(f"--property expects KEY=VALUE, got {item!r}")
        key, value = item.split("=", 1)
        properties[key] = value

    payload: dict = {"platform": args.platform}
    if args.configuration:
        payload["configuration"] = args.configuration
    if args.toolset:
        payload["platform_toolset"] = args.toolset
    if properties:
        payload["extra_properties"] = properties
    if args.callback_url:
        payload["callback_url"] = args.callback_url

    try:
        job = request_json(f"{args.url}/api/v1/builds", args.token, payload)
    except HTTPError as exc:
        print(f"error: HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')}", file=sys.stderr)
        return 1
    except URLError as exc:
        print(f"error: cannot reach {args.url}: {exc}", file=sys.stderr)
        return 1

    job_id = job["id"]
    print(f"submitted job {job_id} ({job['status']})")

    if not args.wait:
        print(json.dumps(job, indent=2, sort_keys=True))
        return 0

    deadline = time.monotonic() + args.timeout
    while True:
        if time.monotonic() > deadline:
            print("error: timed out waiting for the build", file=sys.stderr)
            return 1
        time.sleep(2)
        try:
            job = request_json(f"{args.url}/api/v1/builds/{job_id}", args.token)
        except HTTPError as exc:
            print(f"error: HTTP {exc.code}", file=sys.stderr)
            return 1
        if job["status"] in ("succeeded", "failed"):
            break

    print(json.dumps(job, indent=2, sort_keys=True))
    if job["status"] != "succeeded":
        return 2

    if args.download:
        artifact_url = job["links"].get("artifact")
        if not artifact_url:
            print("error: the service returned no artifact link", file=sys.stderr)
            return 2
        try:
            with urlopen(
                Request(artifact_url, headers={"Authorization": f"Bearer {args.token}"}),
                timeout=120,
            ) as response, Path(args.download).open("wb") as handle:
                handle.write(response.read())
        except (HTTPError, URLError, OSError) as exc:
            print(f"error: artifact download failed: {exc}", file=sys.stderr)
            return 2
        print(f"saved artifact to {args.download}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
