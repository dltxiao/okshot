"""Allow ``python3 -m macshot`` to run the tool."""

import sys

from .cli import main

if __name__ == "__main__":
    sys.exit(main())
