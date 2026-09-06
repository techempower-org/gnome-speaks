#!/usr/bin/env python3
"""Print the /api/version payload of the service at $GS_SVC_PATH as JSON.

Runs with the counting subprocess shim in place, so the git facts are canned
and two different service files can be compared field-for-field.
"""
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from harness import VersionClient, load  # noqa: E402

payload = VersionClient(load()).get(1)
print(json.dumps(payload))
