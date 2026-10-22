"""Check security behavior in the actual release images without cloud writes."""

import argparse
import json
import subprocess


BACKEND_PROBE = r'''
import json
import pyexpat
import xml.etree.ElementTree as ET
from xml.dom import minidom
from langgraph_sdk.auth import Auth

results = {}
results["expatPatched"] = pyexpat.version_info >= (2, 9, 0)
results["expatVersion"] = pyexpat.EXPAT_VERSION
document = '<root><item name="安全">hello &amp; world</item></root>'
results["elementTree"] = ET.fromstring(document)[0].text == "hello & world"
results["minidom"] = minidom.parseString(document).documentElement.tagName == "root"
for resource in ("threads", "assistants", "crons"):
    auth = Auth()
    async def allow_create(ctx, value):
        return None
    getattr(auth.on, resource)(actions=["create"])(allow_create)
    results[resource + "AuthorizationScoped"] = (
        (resource, "create") in auth._handlers
        and (resource, "*") not in auth._handlers
    )
print(json.dumps(results, sort_keys=True))
raise SystemExit(0 if all(v for k, v in results.items() if k != "expatVersion") else 1)
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--backend-image", required=True)
    args = parser.parse_args()
    result = subprocess.run(
        [
            "docker", "run", "--rm", "--network", "none", "--platform", "linux/amd64",
            "--entrypoint", "python", args.backend_image, "-c", BACKEND_PROBE,
        ],
        capture_output=True, text=True, timeout=120, check=False,
    )
    if result.stdout.strip():
        print(json.dumps(json.loads(result.stdout), ensure_ascii=False, sort_keys=True))
    if result.stderr:
        print(result.stderr)
    return result.returncode


if __name__ == "__main__":
    raise SystemExit(main())
