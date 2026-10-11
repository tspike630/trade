"""Allow `python -m sources.ccxt`."""

from sources.ccxt.run import main

if __name__ == "__main__":
    raise SystemExit(main())
