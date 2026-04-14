"""
Run this script ONCE from your project root to download Leaflet 1.9.4
into your static folder so it's served from your own domain.

Usage:
    python download_leaflet.py
"""

import os
import urllib.request

FILES = {
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.min.css": "static/leaflet/leaflet.min.css",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.min.js":  "static/leaflet/leaflet.min.js",
    # Leaflet marker icons (needed for map pins to display correctly)
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png":    "static/leaflet/images/marker-icon.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png": "static/leaflet/images/marker-icon-2x.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png":  "static/leaflet/images/marker-shadow.png",
}

for url, dest in FILES.items():
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"Downloading {url} → {dest} ...", end=" ")
    try:
        urllib.request.urlretrieve(url, dest)
        print(f"✅ ({os.path.getsize(dest):,} bytes)")
    except Exception as e:
        print(f"❌ FAILED: {e}")

print("\nDone! Now redeploy to Vercel.")
print("Folder structure created:")
print("  static/")
print("  └── leaflet/")
print("      ├── leaflet.min.css")
print("      ├── leaflet.min.js")
print("      └── images/")
print("          ├── marker-icon.png")
print("          ├── marker-icon-2x.png")
print("          └── marker-shadow.png")
