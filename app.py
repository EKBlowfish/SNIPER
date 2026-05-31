#!/usr/bin/env python3
"""Entry point for the web UI.

    python app.py        # serves http://127.0.0.1:5000

The implementation lives in the ``sniper.web`` package; this thin launcher
keeps the familiar command (and the preview launch config) working.
"""

from sniper.web.app import main

if __name__ == "__main__":
    main()
