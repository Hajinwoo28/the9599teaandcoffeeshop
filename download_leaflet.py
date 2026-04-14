"""
Run this script ONCE from your project root to download Leaflet 1.9.4
into your static folder so it's served from your own domain.

Usage:
    python download_leaflet.py
"""

import os
import urllib.request

FILES = {
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.css":               "static/leaflet/leaflet.min.css",
    "https://unpkg.com/leaflet@1.9.4/dist/leaflet.js":                "static/leaflet/leaflet.min.js",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon.png":    "static/leaflet/images/marker-icon.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-icon-2x.png": "static/leaflet/images/marker-icon-2x.png",
    "https://unpkg.com/leaflet@1.9.4/dist/images/marker-shadow.png":  "static/leaflet/images/marker-shadow.png",
}

for url, dest in FILES.items():
    os.makedirs(os.path.dirname(dest), exist_ok=True)
    print(f"Downloading {url} -> {dest} ...", end=" ")
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req) as response, open(dest, "wb") as f:
            f.write(response.read())
        print(f"OK ({os.path.getsize(dest):,} bytes)")
    except Exception as e:
        print(f"FAILED: {e}")

print("\nDone! Now run:")
print("  git add static/leaflet/")
print('  git commit -m "Add self-hosted Leaflet"')
print("  git push")
