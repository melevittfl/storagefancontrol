#!/usr/bin/env python3
"""
Install paho-mqtt into ./lib without pip.

TrueNAS SCALE ships Python with neither pip nor ensurepip, and apt is
disabled, so the usual routes to a third-party package are all closed.
paho-mqtt is pure Python and a wheel is just a zip, so this fetches the
wheel from PyPI and unpacks it next to the script, where
storagefancontrol.py picks it up via sys.path.

Only needed if MQTT is enabled. Uses nothing outside the standard library.

    python3 install_deps.py                # latest paho-mqtt
    python3 install_deps.py 2.1.0          # a specific version
"""

import hashlib
import io
import json
import os
import shutil
import sys
import urllib.request
import zipfile

PACKAGE = "paho-mqtt"
SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LIB_DIR = os.path.join(SCRIPT_DIR, "lib")
TIMEOUT = 30


def fetch(url, binary=False):
    with urllib.request.urlopen(url, timeout=TIMEOUT) as response:
        data = response.read()
    return data if binary else json.loads(data.decode("utf-8"))


def find_wheel(version=None):
    """Return (url, sha256, filename) for a pure-Python wheel."""
    url = "https://pypi.org/pypi/%s/json" % PACKAGE
    if version:
        url = "https://pypi.org/pypi/%s/%s/json" % (PACKAGE, version)

    meta = fetch(url)
    resolved = meta["info"]["version"]
    files = meta["urls"] if version else meta["releases"][resolved]

    for f in files:
        if f["filename"].endswith("-py3-none-any.whl"):
            return f["url"], f["digests"]["sha256"], f["filename"], resolved

    # Older releases published a universal wheel instead.
    for f in files:
        if f["filename"].endswith(".whl"):
            return f["url"], f["digests"]["sha256"], f["filename"], resolved

    raise SystemExit("No wheel found for %s %s" % (PACKAGE, resolved))


def main():
    version = sys.argv[1] if len(sys.argv) > 1 else None

    try:
        url, expected_sha, filename, resolved = find_wheel(version)
    except urllib.error.URLError as e:
        raise SystemExit(
            "Could not reach PyPI: %s\n"
            "If this machine has no outbound internet access, download the\n"
            "py3-none-any.whl from https://pypi.org/project/paho-mqtt/#files\n"
            "on another machine, copy it here, and unzip it into ./lib" % e
        )

    print("Downloading %s" % filename)
    blob = fetch(url, binary=True)

    actual_sha = hashlib.sha256(blob).hexdigest()
    if actual_sha != expected_sha:
        raise SystemExit(
            "Checksum mismatch, refusing to install.\n"
            "  expected %s\n  got      %s" % (expected_sha, actual_sha)
        )
    print("sha256 verified")

    # Replace rather than merge, so an old version cannot leave stale
    # modules behind alongside the new one.
    if os.path.isdir(LIB_DIR):
        shutil.rmtree(LIB_DIR)
    os.makedirs(LIB_DIR)

    with zipfile.ZipFile(io.BytesIO(blob)) as wheel:
        wheel.extractall(LIB_DIR)

    print("Installed %s %s into %s" % (PACKAGE, resolved, LIB_DIR))

    # Import it the same way storagefancontrol.py will, as a real check.
    sys.path.insert(0, LIB_DIR)
    try:
        import paho.mqtt.client  # noqa: F401
    except ImportError as e:
        raise SystemExit("Installed, but importing it failed: %s" % e)
    print("Verified: 'import paho.mqtt.client' works")


if __name__ == "__main__":
    main()
