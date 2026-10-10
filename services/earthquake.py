import json
import logging
import math
import urllib.request
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any, List

logger = logging.getLogger(__name__)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calculate distance in kilometers between two GPS coordinates."""
    R = 6371.0  # Earth radius in kilometers
    phi1, phi2 = math.radians(lat1), math.radians(lat2)
    delta_phi = math.radians(lat2 - lat1)
    delta_lambda = math.radians(lon2 - lon1)

    a = math.sin(delta_phi / 2.0) ** 2 + math.cos(phi1) * math.cos(phi2) * (math.sin(delta_lambda / 2.0) ** 2)
    c = 2.0 * math.atan2(math.sqrt(a), math.sqrt(max(0.0, 1.0 - a)))
    return R * c


def get_recent_earthquakes(
    latitude: Optional[float],
    longitude: Optional[float],
    max_radius_km: float = 300.0,
    days: int = 30,
    min_magnitude: float = 2.5,
    timeout_sec: float = 4.0,
) -> Dict[str, Any]:
    """
    Query the public USGS Earthquake Catalog API for events near the given coordinates.
    Gracefully handles missing coordinates, timeouts, or API unavailability.
    """
    if latitude is None or longitude is None:
        return {
            "queried": False,
            "found": False,
            "message": "GPS coordinates not available for earthquake catalog lookup.",
            "disclaimer": "GPS coordinates not available; USGS regional catalog was not queried.",
            "events": [],
        }

    start_date = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%d")

    url = (
        f"https://earthquake.usgs.gov/fdsnws/event/1/query?format=geojson"
        f"&latitude={latitude:.4f}&longitude={longitude:.4f}"
        f"&maxradiuskm={int(max_radius_km)}&minmagnitude={min_magnitude}"
        f"&starttime={start_date}&orderby=time&limit=5"
    )

    req = urllib.request.Request(
        url,
        headers={"User-Agent": "SeismicWatch-StructuralReporting/2.0"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        features = data.get("features", [])
        if not features:
            return {
                "queried": True,
                "found": False,
                "message": (
                    f"No matching earthquake events found in the USGS catalog within "
                    f"{int(max_radius_km)} km and past {days} days (magnitude ≥ {min_magnitude}). "
                    "Note: This does not conclude that no earthquake occurred."
                ),
                "events": [],
                "radius_km": max_radius_km,
                "days": days,
            }

        parsed_events: List[Dict[str, Any]] = []
        for feat in features:
            props = feat.get("properties", {})
            geom = feat.get("geometry", {})
            coords = geom.get("coordinates", [0, 0, 0])

            eq_lon, eq_lat = coords[0], coords[1]
            depth_km = coords[2] if len(coords) > 2 else 0

            dist_km = haversine_km(latitude, longitude, eq_lat, eq_lon)
            time_epoch = props.get("time", 0) / 1000.0
            time_iso = datetime.fromtimestamp(time_epoch, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

            parsed_events.append({
                "id": feat.get("id"),
                "place": props.get("place", "Unknown location"),
                "magnitude": float(props.get("mag") or 0.0),
                "time": time_iso,
                "time_iso": time_iso,
                "distance_km": round(dist_km, 1),
                "depth_km": round(float(depth_km), 1),
                "url": props.get("url"),
            })

        # Sort by distance
        parsed_events.sort(key=lambda x: x["distance_km"])
        closest = parsed_events[0]

        summary_text = (
            f"Nearest recorded event: M {closest['magnitude']} - {closest['place']} "
            f"({closest['distance_km']} km away, {closest['time_iso']})."
        )

        return {
            "queried": True,
            "found": True,
            "message": summary_text,
            "events": parsed_events,
            "closest": closest,
            "radius_km": max_radius_km,
            "days": days,
        }

    except Exception as e:
        logger.warning(f"USGS Earthquake API request failed or timed out: {e}")
        return {
            "queried": True,
            "found": False,
            "unavailable": True,
            "message": (
                "USGS Earthquake data source is temporarily unavailable or timed out. "
                "Report submission proceeded without external seismic verification."
            ),
            "events": [],
        }
