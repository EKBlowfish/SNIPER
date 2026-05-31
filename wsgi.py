"""PythonAnywhere WSGI entry point.

Point your PythonAnywhere web app's WSGI configuration file at this module.
The ``application`` object is the Flask app instance that PythonAnywhere
expects.
"""

import sys
import os
import logging
import threading

# Add the project directory to the Python path
project_dir = os.path.dirname(os.path.abspath(__file__))
if project_dir not in sys.path:
    sys.path.insert(0, project_dir)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)

from sniper.web.app import app, start_fetch, _auto_fetch_loop, AUTO_FETCH_ENABLED

# Start auto-fetch in background
if AUTO_FETCH_ENABLED:
    timer = threading.Timer(5.0, lambda: (start_fetch(), _auto_fetch_loop()))
    timer.daemon = True
    timer.start()

# PythonAnywhere looks for an 'application' object
application = app
