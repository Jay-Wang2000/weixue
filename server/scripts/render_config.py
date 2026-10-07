#!/usr/bin/env python3
"""Render config/config.template.yaml to stdout, replacing @@NAME@@ with the
escaped value (for use inside a double-quoted YAML scalar) of environment variable NAME.
@@NAME?@@ renders as an empty string when NAME is unset. @@NAME[]@@ renders the
comma-separated items of NAME as a YAML flow sequence of strings."""
import json
import os
import re
import sys

template = open(sys.argv[1], encoding="utf-8").read()


def value(match):
    name, suffix = match.group(1), match.group(2)
    optional = suffix == "?"
    raw = os.environ.get(name)
    if raw is None:
        if optional:
            return ""
        sys.exit(f"render_config: {name} is not set")
    if suffix == "[]":
        return json.dumps([item.strip() for item in raw.split(",") if item.strip()])
    if name == "TZ_OFFSET":
        return str(int(raw))
    return json.dumps(raw)[1:-1]


sys.stdout.write(re.sub(r"@@([A-Z_]+)(\?|\[\])?@@", value, template))
