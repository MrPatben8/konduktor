"""PyInstaller entry point for the frozen stem engine (`konduktor-engine`)."""
import sys

from konduktor_engine.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
