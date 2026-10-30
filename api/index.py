import os
import sys
from pathlib import Path

# Add project root directory to sys.path so app and its packages can be imported
project_root = Path(__file__).resolve().parent.parent
if str(project_root) not in sys.path:
    sys.path.insert(0, str(project_root))

from app import app

# Expose WSGI callable for Vercel Serverless Functions
app = app
