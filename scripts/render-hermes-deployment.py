#!/usr/bin/env python3
"""Render the standalone deployment through the same runtime contract as SaaS."""
import json
import pathlib
import sys
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
from hermes_runtime import render_deployment as render

if __name__ == "__main__":
    print(json.dumps(render(*sys.argv[1:])))
