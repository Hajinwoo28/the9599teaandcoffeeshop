import sys
import os

# Add the project root to path so app.py can be imported
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Import the Flask app — Vercel calls this as a serverless function
from app import app

# Vercel needs the handler to be named 'app'
# This file serves as the WSGI entrypoint
