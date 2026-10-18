"""Ambient World & Celestial Sync: Tiered Location Resolver (WinRT/Win32 GPS -> Manual City -> Passive IP) + Open-Meteo ECMWF/NOAA Weather & Solar/Lunar Ephemeris."""
from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from PySide6.QtCore import QObject, QRunnable, QThreadPool, Signal

from core.settings_manager import SETTINGS_MANAGER

WEATHER_CACHE_TTL_SECONDS = 2400.0  # 40 minutes (within 30-45 min window)
LOCATION_CACHE_TTL_SECONDS = 3600.0  # 60 minutes for GPS / IP location cache
HTTP_TIMEOUT_SECONDS = 3.5
SYNODIC_MONTH_DAYS = 29.530588853
# Reference New Moon: 2000-01-06 18:14 UTC
NEW_MOON_EPOCH_UNIX = 947182440.0


@dataclass(frozen=True)
class LocationInfo:
    latitude: float
    longitude: float
    city_name: str
    source: str = "manual"  # "gps", "manual", "ip", or "default"
    region: str = ""
    country: str = ""


@dataclass(frozen=True)
class WeatherData:
    temp_c: int
    humidity: int
    weather_code: int
    is_day: bool
    weather_desc: str
    weather_emoji: str
    weather_icon: str
    fetched_at: float


@dataclass(frozen=True)
class CelestialSnapshot:
    solar_phase: str       # "Dawn", "Day", "Golden Hour", "Dusk", or "Night"
    lunar_phase: str       # e.g. "Full Moon", "Waxing Crescent"
    lunar_emoji: str       # e.g. "🌕"
    ambient_vibe: str      # {ambient_vibe}
    temp_c: str            # {temp_c} numeric string e.g. "24" (so "{temp_c}°C" -> "24°C")
    weather_desc: str      # {weather_desc} e.g. "Partly Cloudy", "Clear Night"
    city_name: str         # {city_name} e.g. "Batujajar", "Bandung"
    celestial_icon: str    # {celestial_icon} e.g. "celestial_night", "weather_rain", "weather_storm"
    utc_offset_str: str    # e.g. "UTC+07:00"
    latitude: float = -6.2088
    longitude: float = 106.8456
    location_source: str = "default"
    humidity: int = 0

    def as_variables(self) -> dict[str, str]:
        return {
            "ambient_vibe": self.ambient_vibe,
            "temp_c": self.temp_c,
            "weather_desc": self.weather_desc,
            "city_name": self.city_name,
            "celestial_icon": self.celestial_icon,
            "solar_phase": self.solar_phase,
            "lunar_phase": self.lunar_phase,
        }


def map_wmo_weather(code: int, is_day: bool = True) -> tuple[str, str, str]:
    """Map WMO weather_code to (human_description, emoji, discord_icon_key).

    Mapping per specification:
    - 0: Clear / Cerah (celestial_day / celestial_night)
    - 1-3: Partly Cloudy / Berawan
    - 51-67: Drizzle / Rain (weather_rain)
    - 95-99: Thunderstorm (weather_storm)
    """
    code = int(code)
    day_icon = "celestial_day" if is_day else "celestial_night"

    if code == 0:
        desc = "Clear Sky" if is_day else "Clear Night"
        emoji = "\u2600\ufe0f" if is_day else "\U0001f319"
        return desc, emoji, day_icon
    if code == 1:
        desc = "Mainly Clear" if is_day else "Mostly Clear Night"
        emoji = "\U0001f324\ufe0f" if is_day else "\U0001f319"
        return desc, emoji, day_icon
    if code == 2:
        return "Partly Cloudy", "\u26c5", day_icon
    if code == 3:
        return "Overcast", "\u2601\ufe0f", day_icon
    if code in (45, 48):
        return "Misty Fog", "\U0001f32b\ufe0f", "celestial_dusk"
    if 51 <= code <= 57:
        return "Drizzle", "\U0001f326\ufe0f", "weather_rain"
    if 61 <= code <= 67:
        if code in (65, 67):
            return "Heavy Rain", "\U0001f327\ufe0f", "weather_rain"
        return "Rain", "\U0001f327\ufe0f", "weather_rain"
    if 71 <= code <= 77 or code in (85, 86):
        return "Snowfall", "\u2744\ufe0f", "weather_rain"
    if 80 <= code <= 82:
        return "Rain Showers", "\U0001f327\ufe0f", "weather_rain"
    if 95 <= code <= 99:
        return "Thunderstorm", "\u26c8\ufe0f", "weather_storm"
    return ("Clear Sky" if is_day else "Clear Night"), ("\u2600\ufe0f" if is_day else "\U0001f319"), day_icon


def compute_solar_phase(dt: datetime | None = None) -> tuple[str, str, str]:
    """Return (solar_phase, celestial_icon, base_vibe) from local clock hour/minute."""
    local_dt = dt or datetime.now().astimezone()
    decimal_hour = local_dt.hour + (local_dt.minute / 60.0)

    if 5.0 <= decimal_hour < 7.0:
        return "Dawn", "celestial_dawn", "\U0001f305 Dawn Horizon"
    if 7.0 <= decimal_hour < 16.5:
        return "Day", "celestial_day", "\u2600\ufe0f Clear Daylight"
    if 16.5 <= decimal_hour < 18.0:
        return "Golden Hour", "celestial_golden", "\U0001f307 Golden Hour"
    if 18.0 <= decimal_hour < 19.25:
        return "Dusk", "celestial_dusk", "\U0001f306 Twilight Dusk"
    return "Night", "celestial_night", "\U0001f319 Starlit Night"


def compute_lunar_phase(dt: datetime | None = None) -> tuple[str, str]:
    """Return (lunar_phase_name, lunar_emoji) using astronomical synodic cycle."""
    target = dt or datetime.now(timezone.utc)
    ts = target.timestamp()
    age_days = ((ts - NEW_MOON_EPOCH_UNIX) / 86400.0) % SYNODIC_MONTH_DAYS
    fraction = age_days / SYNODIC_MONTH_DAYS  # 0.0 .. 1.0

    phases = (
        (0.0625, "New Moon", "\U0001f311"),
        (0.1875, "Waxing Crescent", "\U0001f312"),
        (0.3125, "First Quarter", "\U0001f313"),
        (0.4375, "Waxing Gibbous", "\U0001f314"),
        (0.5625, "Full Moon", "\U0001f315"),
        (0.6875, "Waning Gibbous", "\U0001f316"),
        (0.8125, "Last Quarter", "\U0001f317"),
        (0.9375, "Waning Crescent", "\U0001f318"),
        (1.0001, "New Moon", "\U0001f311"),
    )
    for limit, name, emoji in phases:
        if fraction <= limit:
            return name, emoji
    return "New Moon", "\U0001f311"


def format_utc_offset(dt: datetime | None = None) -> str:
    local_dt = dt or datetime.now().astimezone()
    offset = local_dt.utcoffset()
    if offset is None:
        return "UTC+00:00"
    total_minutes = int(offset.total_seconds() // 60)
    sign = "+" if total_minutes >= 0 else "-"
    total_minutes = abs(total_minutes)
    hours, minutes = divmod(total_minutes, 60)
    return f"UTC{sign}{hours:02d}:{minutes:02d}"


def _reverse_geocode_city(lat: float, lon: float, timeout: float = 2.5) -> str:
    """Resolve human-readable city/locality name from coordinates without API key."""
    url = (
        f"https://api.bigdatacloud.net/data/reverse-geocode-client"
        f"?latitude={lat:.4f}&longitude={lon:.4f}&localityLanguage=id"
    )
    try:
        req = urllib.request.Request(url, headers={"User-Agent": "OmniPresence/2.3.0"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read(32768).decode("utf-8", errors="replace"))
        city = (
            data.get("locality")
            or data.get("city")
            or data.get("principalSubdivision")
            or ""
        ).strip()
        if city:
            return city
    except Exception:
        pass
    return ""


def _try_windows_geolocation(timeout: float = 3.0) -> LocationInfo | None:
    """Tier 1: Attempt Windows Geolocation API via WinRT (winsdk/winrt), COM LatLongReportFactory, or System.Device.Location."""
    if sys.platform != "win32":
        return None

    # 1A: Try WinRT Python bindings (winsdk or winrt) if installed
    for mod_name in ("winsdk.windows.devices.geolocation", "winrt.windows.devices.geolocation"):
        try:
            mod = __import__(mod_name, fromlist=["Geolocator"])
            Geolocator = getattr(mod, "Geolocator")

            async def _get_pos():
                locator = Geolocator()
                pos = await asyncio.wait_for(locator.get_geoposition_async(), timeout=timeout)
                point = pos.coordinate.point.position
                return float(point.latitude), float(point.longitude)

            lat, lon = asyncio.run(_get_pos())
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and (abs(lat) > 0.0001 or abs(lon) > 0.0001):
                city = _reverse_geocode_city(lat, lon, timeout=2.0) or "GPS Location"
                return LocationInfo(latitude=lat, longitude=lon, city_name=city, source="gps")
        except Exception:
            pass

    # 1B: Try COM LocationDisp.LatLongReportFactory via win32com / comtypes if available
    try:
        import win32com.client  # type: ignore

        factory = win32com.client.Dispatch("LocationDisp.LatLongReportFactory")
        factory.ListenForReports(0)
        status = int(getattr(factory, "Status", 0))
        # Status 4 = Report Running
        if status == 4:
            report = factory.LatLongReport
            lat = float(report.Latitude)
            lon = float(report.Longitude)
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and (abs(lat) > 0.0001 or abs(lon) > 0.0001):
                city = _reverse_geocode_city(lat, lon, timeout=2.0) or "GPS Location"
                return LocationInfo(latitude=lat, longitude=lon, city_name=city, source="gps")
    except Exception:
        pass

    # 1C: Try .NET System.Device.Location.GeoCoordinateWatcher via hidden PowerShell probe (fast 2.3s timeout)
    try:
        ps_cmd = (
            "Add-Type -AssemblyName System.Device; "
            "$w = New-Object System.Device.Location.GeoCoordinateWatcher; "
            "$ok = $w.TryStart($false, [TimeSpan]::FromMilliseconds(1500)); "
            "$p = $w.Position.Location; "
            "if (-not $p.IsUnknown) { Write-Output \"$($p.Latitude),$($p.Longitude)\" }; "
            "$w.Stop()"
        )
        creationflags = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
        proc = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_cmd],
            capture_output=True,
            text=True,
            timeout=timeout,
            creationflags=creationflags,
        )
        out = (proc.stdout or "").strip()
        if "," in out:
            lat_s, lon_s = out.split(",", 1)
            lat = float(lat_s.strip().replace(",", "."))
            lon = float(lon_s.strip().replace(",", "."))
            if -90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0 and (abs(lat) > 0.0001 or abs(lon) > 0.0001):
                city = _reverse_geocode_city(lat, lon, timeout=2.0) or "GPS Location"
                return LocationInfo(latitude=lat, longitude=lon, city_name=city, source="gps")
    except Exception:
        pass

    return None


def _try_passive_ip_geolocation(timeout: float = 2.8) -> LocationInfo | None:
    """Fallback passive IP geolocation (ip-api.com -> ipwho.is) with zero API key."""
    endpoints = (
        ("http://ip-api.com/json/?fields=status,country,regionName,city,lat,lon", "ip-api"),
        ("https://ipwho.is/", "ipwhois"),
    )
    for url, kind in endpoints:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "OmniPresence/2.3.0"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read(32768).decode("utf-8", errors="replace"))
            if kind == "ip-api":
                if data.get("status") != "success":
                    continue
                lat = float(data["lat"])
                lon = float(data["lon"])
                city = str(data.get("city") or data.get("regionName") or "Local Area").strip()
                region = str(data.get("regionName") or "").strip()
                country = str(data.get("country") or "").strip()
                return LocationInfo(lat, lon, city, source="ip", region=region, country=country)
            else:
                if not data.get("success", True):
                    continue
                lat = float(data["latitude"])
                lon = float(data["longitude"])
                city = str(data.get("city") or data.get("region") or "Local Area").strip()
                region = str(data.get("region") or "").strip()
                country = str(data.get("country") or "").strip()
                return LocationInfo(lat, lon, city, source="ip", region=region, country=country)
        except Exception:
            continue
    return None


def geocode_city(query: str, timeout: float = HTTP_TIMEOUT_SECONDS) -> LocationInfo | None:
    """Tier 2 Helper: Search city/district coordinates via Open-Meteo Geocoding API."""
    cleaned = (query or "").strip()
    if not cleaned:
        return None
    encoded = urllib.parse.quote(cleaned)
    url = f"https://geocoding-api.open-meteo.com/v1/search?name={encoded}&count=1&language=id&format=json"
    req = urllib.request.Request(url, headers={"User-Agent": "OmniPresence/2.3.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read(32768).decode("utf-8", errors="replace"))
    results = payload.get("results") or []
    if not results:
        return None
    first = results[0]
    lat = float(first["latitude"])
    lon = float(first["longitude"])
    name = str(first.get("name") or cleaned).strip()
    admin1 = str(first.get("admin1") or "").strip()
    country = str(first.get("country") or "").strip()
    return LocationInfo(
        latitude=lat,
        longitude=lon,
        city_name=name,
        source="manual",
        region=admin1,
        country=country,
    )


_LOCATION_CACHE_LOCK = threading.Lock()
_CACHED_GPS_LOCATION: tuple[float, LocationInfo] | None = None
_CACHED_IP_LOCATION: tuple[float, LocationInfo] | None = None


def resolve_device_location(*, force_gps_probe: bool = False, allow_network: bool = True) -> LocationInfo:
    """Resolve user location following the 3-tier hierarchy:
    1. Tier 1: Windows Location Service (WinRT / Win32 GPS sensor) when location_mode == 'gps' or force_gps_probe.
    2. Tier 2: Manual setting from SettingsManager (manual_location_name, manual_lat, manual_lon).
    3. Fallback: Passive IP Geolocation (ip-api.com / ipwho.is) with in-memory caching.
    """
    global _CACHED_GPS_LOCATION, _CACHED_IP_LOCATION

    s = SETTINGS_MANAGER.settings
    mode = getattr(s, "location_mode", "gps").strip().lower()
    manual_name = getattr(s, "manual_location_name", "").strip()
    manual_lat = float(getattr(s, "manual_lat", s.celestial_latitude))
    manual_lon = float(getattr(s, "manual_lon", s.celestial_longitude))

    # If user explicitly selected Manual Mode and configured a city/coordinates:
    if not force_gps_probe and mode == "manual" and manual_name:
        return LocationInfo(
            latitude=manual_lat,
            longitude=manual_lon,
            city_name=manual_name,
            source="manual",
        )

    now_mono = time.monotonic()

    # Tier 1: Try Windows Geolocation API (GPS / Location Sensor)
    if mode == "gps" or force_gps_probe:
        with _LOCATION_CACHE_LOCK:
            if (
                not force_gps_probe
                and _CACHED_GPS_LOCATION is not None
                and (now_mono - _CACHED_GPS_LOCATION[0]) < LOCATION_CACHE_TTL_SECONDS
            ):
                return _CACHED_GPS_LOCATION[1]

        if allow_network or force_gps_probe:
            gps_loc = _try_windows_geolocation(timeout=2.5 if force_gps_probe else 1.8)
            if gps_loc is not None:
                with _LOCATION_CACHE_LOCK:
                    _CACHED_GPS_LOCATION = (now_mono, gps_loc)
                return gps_loc

    # Tier 2 fallback: Check if user has manual location saved in SettingsManager
    if manual_name:
        return LocationInfo(
            latitude=manual_lat,
            longitude=manual_lon,
            city_name=manual_name,
            source="manual",
        )

    # Tier 3 fallback: Passive IP Geolocation with caching
    with _LOCATION_CACHE_LOCK:
        if (
            _CACHED_IP_LOCATION is not None
            and (now_mono - _CACHED_IP_LOCATION[0]) < LOCATION_CACHE_TTL_SECONDS
        ):
            return _CACHED_IP_LOCATION[1]

    if allow_network:
        ip_loc = _try_passive_ip_geolocation(timeout=2.5)
        if ip_loc is not None:
            with _LOCATION_CACHE_LOCK:
                _CACHED_IP_LOCATION = (now_mono, ip_loc)
            return ip_loc

    return LocationInfo(
        latitude=float(s.celestial_latitude),
        longitude=float(s.celestial_longitude),
        city_name=manual_name or "Jakarta",
        source="default",
    )


_WEATHER_MEM_CACHE: dict[tuple[float, float], WeatherData] = {}
_WEATHER_MEM_LOCK = threading.Lock()


def fetch_weather(
    lat: float,
    lon: float,
    *,
    force_refresh: bool = False,
    timeout: float = HTTP_TIMEOUT_SECONDS,
) -> WeatherData:
    """Fetch current weather from Open-Meteo (ECMWF/NOAA) and cache in memory for 30-45 minutes."""
    cache_key = (round(float(lat), 2), round(float(lon), 2))
    now_mono = time.monotonic()

    if not force_refresh:
        with _WEATHER_MEM_LOCK:
            cached = _WEATHER_MEM_CACHE.get(cache_key)
            if cached is not None and (now_mono - cached.fetched_at) < WEATHER_CACHE_TTL_SECONDS:
                return cached

    url = (
        f"https://api.open-meteo.com/v1/forecast"
        f"?latitude={float(lat):.4f}&longitude={float(lon):.4f}"
        f"&current=temperature_2m,relative_humidity_2m,weather_code,is_day"
        f"&timezone=auto"
    )
    req = urllib.request.Request(url, headers={"User-Agent": "OmniPresence/2.3.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        payload = json.loads(resp.read(32768).decode("utf-8", errors="replace"))

    current: dict[str, Any] = payload.get("current") or payload.get("current_weather") or {}
    temp_raw = current.get("temperature_2m", current.get("temperature", 24.0))
    humidity_raw = current.get("relative_humidity_2m", 70)
    code_raw = current.get("weather_code", current.get("weathercode", 0))
    is_day_raw = current.get("is_day", 1)

    temp_c = int(round(float(temp_raw)))
    humidity = int(round(float(humidity_raw)))
    code = int(code_raw)
    is_day = bool(int(is_day_raw))

    desc, emoji, icon = map_wmo_weather(code, is_day=is_day)
    result = WeatherData(
        temp_c=temp_c,
        humidity=humidity,
        weather_code=code,
        is_day=is_day,
        weather_desc=desc,
        weather_emoji=emoji,
        weather_icon=icon,
        fetched_at=now_mono,
    )
    with _WEATHER_MEM_LOCK:
        _WEATHER_MEM_CACHE[cache_key] = result
    return result


class _CelestialSignals(QObject):
    weather_updated = Signal(object)   # CelestialSnapshot
    location_resolved = Signal(object)  # LocationInfo


class _WeatherRunnable(QRunnable):
    def __init__(self, engine: "CelestialSyncEngine", force_refresh: bool = False) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._engine = engine
        self._force_refresh = force_refresh

    def run(self) -> None:
        try:
            self._engine._sync_location_and_weather(force_refresh=self._force_refresh)
        except Exception:
            pass
        finally:
            self._engine._on_weather_finished(self)


class CelestialSyncEngine:
    """Computes local solar/lunar ephemeris, resolves tiered device location, and enriches with Open-Meteo weather."""

    def __init__(
        self,
        enabled: bool = True,
        weather_enabled: bool = True,
        latitude: float = -6.2088,
        longitude: float = 106.8456,
    ) -> None:
        self._lock = threading.Lock()
        self.enabled = bool(enabled)
        self.weather_enabled = bool(weather_enabled)
        self.latitude = float(latitude)
        self.longitude = float(longitude)
        self.location_mode = "gps"
        self.manual_location_name = ""

        self._cached_city: str = ""
        self._cached_source: str = "default"
        self._cached_weather: WeatherData | None = None
        self._weather_pending = False
        self._active_runnables: set[_WeatherRunnable] = set()

        self.signals = _CelestialSignals()
        self._pool = QThreadPool.globalInstance()

    def configure(
        self,
        *,
        enabled: bool | None = None,
        weather_enabled: bool | None = None,
        latitude: float | None = None,
        longitude: float | None = None,
        location_mode: str | None = None,
        manual_location_name: str | None = None,
    ) -> None:
        changed = False
        with self._lock:
            if enabled is not None:
                self.enabled = bool(enabled)
            if weather_enabled is not None:
                self.weather_enabled = bool(weather_enabled)
            if location_mode is not None and location_mode != self.location_mode:
                self.location_mode = str(location_mode)
                changed = True
            if manual_location_name is not None and manual_location_name != self.manual_location_name:
                self.manual_location_name = str(manual_location_name).strip()
                if self.manual_location_name:
                    self._cached_city = self.manual_location_name
                changed = True
            if latitude is not None and abs(float(latitude) - self.latitude) > 0.005:
                self.latitude = float(latitude)
                changed = True
            if longitude is not None and abs(float(longitude) - self.longitude) > 0.005:
                self.longitude = float(longitude)
                changed = True
            if changed:
                self._cached_weather = None

    def trigger_refresh(self, *, force_refresh: bool = True) -> None:
        """Queue an asynchronous location + weather sync on the global QThreadPool."""
        with self._lock:
            if self._weather_pending:
                return
            self._weather_pending = True
            runnable = _WeatherRunnable(self, force_refresh=force_refresh)
            self._active_runnables.add(runnable)
            self._pool.start(runnable)

    def set_manual_city(self, query: str) -> LocationInfo | None:
        """Geocode a city name via Open-Meteo, persist to SettingsManager, and fetch weather immediately."""
        loc = geocode_city(query)
        if loc is None:
            return None
        SETTINGS_MANAGER.update(
            location_mode="manual",
            manual_location_name=loc.city_name,
            manual_lat=round(loc.latitude, 4),
            manual_lon=round(loc.longitude, 4),
            celestial_latitude=round(loc.latitude, 4),
            celestial_longitude=round(loc.longitude, 4),
            celestial_weather_enabled=True,
        )
        with self._lock:
            self.location_mode = "manual"
            self.manual_location_name = loc.city_name
            self.latitude = loc.latitude
            self.longitude = loc.longitude
            self._cached_city = loc.city_name
            self._cached_source = "manual"
        try:
            weather = fetch_weather(loc.latitude, loc.longitude, force_refresh=True)
            with self._lock:
                self._cached_weather = weather
        except Exception:
            pass
        try:
            self.signals.location_resolved.emit(loc)
            self.signals.weather_updated.emit(self.snapshot(allow_network=False))
        except RuntimeError:
            pass
        return loc

    def detect_gps_now(self) -> LocationInfo:
        """Actively probe Tier 1 GPS (with IP fallback), update state, and refresh weather."""
        loc = resolve_device_location(force_gps_probe=True, allow_network=True)
        with self._lock:
            self.latitude = loc.latitude
            self.longitude = loc.longitude
            self._cached_city = loc.city_name
            self._cached_source = loc.source
        SETTINGS_MANAGER.update(
            celestial_latitude=round(loc.latitude, 4),
            celestial_longitude=round(loc.longitude, 4),
        )
        try:
            weather = fetch_weather(loc.latitude, loc.longitude, force_refresh=True)
            with self._lock:
                self._cached_weather = weather
        except Exception:
            pass
        try:
            self.signals.location_resolved.emit(loc)
            self.signals.weather_updated.emit(self.snapshot(allow_network=False))
        except RuntimeError:
            pass
        return loc

    def snapshot(self, dt: datetime | None = None, *, allow_network: bool = True) -> CelestialSnapshot:
        """Compute current CelestialSnapshot from local clock and cached/async weather."""
        local_dt = dt or datetime.now().astimezone()
        solar_phase, solar_icon, base_vibe = compute_solar_phase(local_dt)
        lunar_phase, lunar_emoji = compute_lunar_phase(local_dt)
        utc_offset = format_utc_offset(local_dt)

        if solar_phase == "Night":
            vibe = f"{lunar_emoji} {lunar_phase} Night"
        else:
            vibe = base_vibe

        s = SETTINGS_MANAGER.settings
        fallback_city = (
            getattr(s, "manual_location_name", "").strip()
            or self._cached_city
            or "Jakarta"
        )

        temp_c = "24"
        weather_desc = "Clear Night" if solar_phase == "Night" else "Partly Cloudy"
        icon = solar_icon
        city_name = fallback_city
        lat = self.latitude
        lon = self.longitude
        source = self._cached_source
        humidity = 0

        with self._lock:
            w_enabled = self.weather_enabled and self.enabled
            now_mono = time.monotonic()
            cached_w = self._cached_weather
            cache_valid = (
                cached_w is not None
                and (now_mono - cached_w.fetched_at) < WEATHER_CACHE_TTL_SECONDS
            )
            if cached_w is not None:
                temp_c = str(cached_w.temp_c)
                weather_desc = cached_w.weather_desc
                icon = cached_w.weather_icon or solar_icon
                humidity = cached_w.humidity
                if w_enabled:
                    vibe = f"{vibe} \u2022 {weather_desc} ({temp_c}\u00b0C)"

            if w_enabled and not cache_valid and allow_network and not self._weather_pending:
                self._weather_pending = True
                runnable = _WeatherRunnable(self, force_refresh=False)
                self._active_runnables.add(runnable)
                self._pool.start(runnable)

        return CelestialSnapshot(
            solar_phase=solar_phase,
            lunar_phase=lunar_phase,
            lunar_emoji=lunar_emoji,
            ambient_vibe=vibe,
            temp_c=temp_c,
            weather_desc=weather_desc,
            city_name=city_name,
            celestial_icon=icon,
            utc_offset_str=utc_offset,
            latitude=lat,
            longitude=lon,
            location_source=source,
            humidity=humidity,
        )

    def _sync_location_and_weather(self, *, force_refresh: bool = False) -> None:
        loc = resolve_device_location(force_gps_probe=False, allow_network=True)
        weather = fetch_weather(loc.latitude, loc.longitude, force_refresh=force_refresh)
        with self._lock:
            self.latitude = loc.latitude
            self.longitude = loc.longitude
            self._cached_city = loc.city_name
            self._cached_source = loc.source
            self._cached_weather = weather

    def _on_weather_finished(self, runnable: _WeatherRunnable) -> None:
        with self._lock:
            self._weather_pending = False
            self._active_runnables.discard(runnable)
        try:
            self.signals.weather_updated.emit(self.snapshot(allow_network=False))
        except RuntimeError:
            pass


CELESTIAL_SYNC = CelestialSyncEngine()
