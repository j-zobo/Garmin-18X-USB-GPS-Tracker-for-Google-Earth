# Garmin 18X USB GPS Tracker for Google Earth

Provides real-time location in Google Earth. Does not create a path of where the GPS has been; only acts as a bridge between a Garmin 18X USB GPS and Google Earth

The application uses GPSBabel in the background to safely stream live Garmin PVT (Position, Velocity, Time) data, parses it, and launches an internal HTTP server. This server dynamically hosts a KML network link that allows Google Earth to actively track and "fly to" your live position seamlessly without any lag or port conflicts.

## Features
* **Single-Process Architecture:** Built entirely using native Python threading (no clunky background command boxes).
* **Self-Healing Connection:** Automatically clears port locks and handles rapid application restarts gracefully.
* **Persistent Settings:** Features a built-in GUI configuration menu to customize ports, vehicle names, and locate dependencies easily.
* **Antivirus Friendly:** Optimized codebase designed to safely run on Windows environments without triggering false-positive alerts.

## Prerequisites
To run this project from the source code, you will need:
1. **Python 3.x**
2. **GPSBabel** (Installed on your system)
3. **Google Earth Pro**

## How to Run
Simply run the main tracking script:

python gps_tracker.py

## GUI Notes
The Open in Google Earth button will only become avalaible once tracking is active. Clicking this will place a .kml file on the users Desktop with the network link preconfigured. Ensure Google Earth is the default app for .kml files and it will automatically launch. 
