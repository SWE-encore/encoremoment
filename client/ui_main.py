"""Compatibility launcher; prefer python -m encore_client."""
from encore_client.__main__ import main

if __name__ == "__main__":
    raise SystemExit(main())
