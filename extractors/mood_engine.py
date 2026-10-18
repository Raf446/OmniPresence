"""Adaptive Mood Engine: 48x48 HSL color quantization of media covers & YouTube thumbnails via QThreadPool."""
from __future__ import annotations

import colorsys
import math
import threading
import urllib.request
from collections.abc import Sequence
from dataclasses import dataclass

from PySide6.QtCore import QObject, QRunnable, Qt, QThreadPool, Signal
from PySide6.QtGui import QImage

MOOD_SAMPLE_SIZE = 48
HTTP_TIMEOUT_SECONDS = 1.8

BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/124.0.0.0 Safari/537.36"
)


@dataclass(frozen=True)
class MoodResult:
    category: str       # "vibrant_neon", "tokyo_night", "warm_lofi", or "monochrome"
    mood_name: str      # Human-readable label for {mood_name}
    mood_badge: str     # Asset key / badge identifier for {mood_badge}
    dominant_hex: str   # Dominant hex color (#RRGGBB)
    avg_hue_deg: float
    avg_saturation: float
    avg_lightness: float


MOOD_CATALOG: dict[str, tuple[str, str, str]] = {
    "vibrant_neon": ("Vibrant Neon", "mood_vibrant_neon", "#00E5FF"),
    "tokyo_night": ("Tokyo Night", "mood_tokyo_night", "#7C4DFF"),
    "warm_lofi": ("Warm Lo-Fi", "mood_warm_lofi", "#FF8A65"),
    "monochrome": ("Monochrome", "mood_monochrome", "#94A3B8"),
}

DEFAULT_MOOD = MoodResult(
    category="tokyo_night",
    mood_name="Tokyo Night",
    mood_badge="mood_tokyo_night",
    dominant_hex="#7C4DFF",
    avg_hue_deg=255.0,
    avg_saturation=0.45,
    avg_lightness=0.35,
)


def classify_hsl(hue_deg: float, saturation: float, lightness: float, hex_color: str = "") -> MoodResult:
    """Classify (hue_deg [0..360], saturation [0..1], lightness [0..1]) into one of 4 OmniPresence moods."""
    h = hue_deg % 360.0
    s = min(max(saturation, 0.0), 1.0)
    l_val = min(max(lightness, 0.0), 1.0)

    if s < 0.22 or l_val < 0.07 or l_val > 0.94:
        cat = "monochrome"
    elif h <= 65.0 or h >= 330.0:
        cat = "warm_lofi"
    elif s >= 0.65 and 0.38 <= l_val <= 0.78:
        cat = "vibrant_neon"
    elif 185.0 <= h < 330.0:
        cat = "tokyo_night"
    elif s >= 0.50:
        cat = "vibrant_neon"
    else:
        cat = "warm_lofi"

    name, badge, fallback_hex = MOOD_CATALOG[cat]
    return MoodResult(
        category=cat,
        mood_name=name,
        mood_badge=badge,
        dominant_hex=hex_color or fallback_hex,
        avg_hue_deg=round(h, 1),
        avg_saturation=round(s, 3),
        avg_lightness=round(l_val, 3),
    )


def quantize_rgb_samples(samples: Sequence[tuple[int, int, int]]) -> MoodResult:
    """Compute circular mean hue, weighted saturation, and lightness from RGB tuples."""
    if not samples:
        return DEFAULT_MOOD

    sin_sum = 0.0
    cos_sum = 0.0
    sat_sum = 0.0
    light_sum = 0.0
    r_sum = 0.0
    g_sum = 0.0
    b_sum = 0.0
    weight_sum = 0.0
    count = 0

    for r, g, b in samples:
        rf, gf, bf = r / 255.0, g / 255.0, b / 255.0
        h, l_val, s = colorsys.rgb_to_hls(rf, gf, bf)
        count += 1
        sat_sum += s
        light_sum += l_val

        # Weight chroma-bearing pixels more strongly when computing dominant hue
        w = max(0.05, s * (1.0 - abs(2.0 * l_val - 1.0) * 0.65))
        angle = h * 2.0 * math.pi
        sin_sum += math.sin(angle) * w
        cos_sum += math.cos(angle) * w
        r_sum += r * w
        g_sum += g * w
        b_sum += b * w
        weight_sum += w

    avg_s = sat_sum / max(1, count)
    avg_l = light_sum / max(1, count)
    mean_angle = math.atan2(sin_sum, cos_sum)
    if mean_angle < 0:
        mean_angle += 2.0 * math.pi
    avg_h_deg = math.degrees(mean_angle)

    if weight_sum > 0:
        dom_r = min(255, max(0, int(round(r_sum / weight_sum))))
        dom_g = min(255, max(0, int(round(g_sum / weight_sum))))
        dom_b = min(255, max(0, int(round(b_sum / weight_sum))))
        dom_hex = f"#{dom_r:02X}{dom_g:02X}{dom_b:02X}"
    else:
        dom_hex = "#94A3B8"

    return classify_hsl(avg_h_deg, avg_s, avg_l, dom_hex)


def analyze_qimage(image: QImage) -> MoodResult:
    """Downscale a QImage to 48x48 px and quantize its HSL palette into a MoodResult."""
    if image.isNull():
        return DEFAULT_MOOD

    scaled = image. scaled(
        MOOD_SAMPLE_SIZE,
        MOOD_SAMPLE_SIZE,
        Qt.AspectRatioMode.IgnoreAspectRatio,
        Qt.TransformationMode.SmoothTransformation,
    ).convertToFormat(QImage.Format.Format_RGB32)

    w, h = scaled.width(), scaled.height()
    samples: list[tuple[int, int, int]] = []
    for y in range(h):
        for x in range(w):
            c = scaled.pixelColor(x, y)
            samples.append((c.red(), c.green(), c.blue()))
    return quantize_rgb_samples(samples)


class _MoodSignals(QObject):
    resolved = Signal(str, object)  # (key, MoodResult)


class _MoodRunnable(QRunnable):
    def __init__(self, engine: "AdaptiveMoodEngine", key: str, source: str | bytes | QImage) -> None:
        super().__init__()
        self.setAutoDelete(True)
        self._engine = engine
        self._key = key
        self._source = source

    def run(self) -> None:
        result = DEFAULT_MOOD
        try:
            result = self._engine.compute_sync(self._source)
        except Exception:
            result = DEFAULT_MOOD
        finally:
            self._engine._on_task_finished(self._key, result, self)


class AdaptiveMoodEngine:
    """Asynchronous 48x48 cover-art HSL mood classifier backed by QThreadPool and in-memory cache."""

    def __init__(self, enabled: bool = True) -> None:
        self._lock = threading.Lock()
        self.enabled = bool(enabled)
        self.cache: dict[str, MoodResult] = {}
        self._pending: set[str] = set()
        self._active_runnables: set[_MoodRunnable] = set()
        self.signals = _MoodSignals()
        self._pool = QThreadPool.globalInstance()

    def set_enabled(self, enabled: bool) -> None:
        with self._lock:
            self.enabled = bool(enabled)

    def analyze_image(self, image: QImage) -> MoodResult:
        return analyze_qimage(image)

    def analyze_rgb(self, samples: Sequence[tuple[int, int, int]]) -> MoodResult:
        return quantize_rgb_samples(samples)

    def compute_sync(self, source: str | bytes | QImage) -> MoodResult:
        if isinstance(source, QImage):
            return analyze_qimage(source)
        if isinstance(source, (bytes, bytearray)):
            img = QImage()
            img.loadFromData(bytes(source))
            return analyze_qimage(img)
        if isinstance(source, str):
            key = source.strip()
            with self._lock:
                if key in self.cache:
                    return self.cache[key]
            if key.lower().startswith(("http://", "https://")):
                req = urllib.request.Request(key, headers={"User-Agent": BROWSER_USER_AGENT})
                with urllib.request.urlopen(req, timeout=HTTP_TIMEOUT_SECONDS) as resp:
                    raw = resp.read(2_500_000)
                img = QImage()
                img.loadFromData(raw)
                res = analyze_qimage(img)
                with self._lock:
                    self.cache[key] = res
                return res
        return DEFAULT_MOOD

    def resolve_for_url(self, url: str, *, background: bool = True) -> MoodResult | None:
        """Return cached MoodResult for an artwork/thumbnail URL or dispatch a QThreadPool worker."""
        if not self.enabled:
            return None
        clean_url = (url or "").strip()
        if not clean_url or not clean_url.lower().startswith(("http://", "https://")):
            return DEFAULT_MOOD

        with self._lock:
            if clean_url in self.cache:
                return self.cache[clean_url]
            if not background:
                pass
            elif clean_url in self._pending:
                return DEFAULT_MOOD
            else:
                self._pending.add(clean_url)
                runnable = _MoodRunnable(self, clean_url, clean_url)
                self._active_runnables.add(runnable)
                self._pool.start(runnable)
                return DEFAULT_MOOD

        return self.compute_sync(clean_url)

    def _on_task_finished(self, key: str, result: MoodResult, runnable: _MoodRunnable) -> None:
        with self._lock:
            self.cache[key] = result
            self._pending.discard(key)
            self._active_runnables.discard(runnable)
        try:
            self.signals.resolved.emit(key, result)
        except RuntimeError:
            pass


MOOD_ENGINE = AdaptiveMoodEngine()
