#!/usr/bin/env python3
"""Standalone launcher so a repository checkout stays runnable as ./run.py.

The actual code lives in the sibling package directory ``media_optimizer/``.
"""

import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:
    from media_optimizer.cli import main
except ModuleNotFoundError as exc:
    if exc.name != "media_optimizer":
        raise
    print("media-optimizer: the 'media_optimizer' package was not found.\n", file=sys.stderr)
    print(f"Looked in: {HERE}", file=sys.stderr)
    print(
        "\nExpected this layout:\n\n"
        "    run.py\n"
        "    media_optimizer/\n"
        "        __init__.py  __main__.py  cli.py  config.py  files.py  gpu.py  media.py\n"
        "        orchestrator.py  proc.py  runlog.py  safety.py  ui.py\n"
        "        stages/  __init__.py  base.py  images.py  gifs.py  avif.py  video.py  verify.py\n",
        file=sys.stderr,
    )
    sys.exit(2)

if __name__ == "__main__":
    sys.exit(main())
