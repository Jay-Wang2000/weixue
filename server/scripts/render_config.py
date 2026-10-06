#!/usr/bin/env python3
"""Render config/config.template.yaml to stdout, replacing @@NAME@@ with the
escaped value (for use inside a double-quoted YAML scalar) of environment variable NAME.
@@NAME?@@ renders as an empty string when NAME is unset."""
import json
import os
import re
import sys

template = open(sys.argv[1], encoding="utf-8").read()


def value(match):
    name, optional = match.group(1), match.group(2)
    raw = os.environ.get(name)
    if raw is None:
        if optional:
            return ""
        sys.exit(f"render_config: {name} is not set")
    if name == "TZ_OFFSET":
        return str(int(raw))
    return json.dumps(raw)[1:-1]


sys.stdout.write(re.sub(r"@@([A-Z_]+)(\??)@@", value, template))
