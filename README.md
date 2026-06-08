# Garmin 18X USB GPS Tracker for Google Earth

Provides real-time location in Google Earth. Does not create a trail/path history; only acts as a bridge between a Garmin 18X USB GPS and Google Earth

The application uses GPSBabel in the background to stream live Garmin PVT (Position, Velocity, Time) data, parses it, and launches an internal HTTP server. This server dynamically hosts a KML network link that allows Google Earth to actively track and "fly to" your live position seamlessly without any lag or port conflicts.

## Features
* **Live Position Tracking:** Streams real-time GPS data directly into Google Earth, which actively follows and flies to your current location.
* **Clean, Clutter-Free View:** Displays your current position only; no trail or accumulated path history.
* **KML Network Link:** Hosts an internal HTTP server that dynamically serves a KML network link.
* **No Lag, No Port Conflicts:** Handles rapid restarts and stale connections automatically so your feed stays live without manual intervention.
*  **Simple Configuration:** A built-in GUI settings menu lets you customize ports, vehicle names, and dependency paths, all saved between sessions.

## Prerequisites
To run this project, you will need:
1. **Garmin 18X USB GPS:** This application is purpose built for this device and may not work with other GPS hardware.
2. **Python 3.x**
3. **GPSBabel** (PATH can be updated in the GUI settings menu.)
4. **Google Earth Pro**

## How to Run
Simply run the main tracking script:

python gps_tracker.py

## GUI Notes
The Open in Google Earth button will only become avalaible once tracking is active. Clicking this will place a .kml file on the users Desktop with the network link preconfigured. Ensure Google Earth is the default app for .kml files and it will automatically launch. 
