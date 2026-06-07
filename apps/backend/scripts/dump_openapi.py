"""Print the backend's OpenAPI schema as JSON to stdout.

The Mini App's TypeScript types are generated from this schema (ADR-0003). The
root ``gen:api`` script pipes this into ``openapi-typescript``. Run directly with:

    uv --directory apps/backend run python scripts/dump_openapi.py
"""

import json

from balam.server import openapi_schema

if __name__ == "__main__":
    print(json.dumps(openapi_schema(), indent=2))
