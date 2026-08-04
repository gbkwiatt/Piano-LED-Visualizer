"""Backlight effects.

The backlight is whatever a pixel shows when no note is holding it. These effects
shape its brightness along the length of the strip instead of leaving it flat.

An effect is a pure function of the pixel index, so it is applied where the
backlight is already painted rather than by repainting the strip. Note lighting
keeps precedence for free: a lit key, its fade and its adjacent colours overwrite
the backlight as before, and the effect reappears once the pixel goes idle.
"""

import math

from lib.rpi_drivers import Color

NONE = "None"
WAVE = "Wave"
EFFECTS = [NONE, WAVE]

_CACHE = {}
_CACHE_LIMIT = 8


def _profile(ledsettings, count):
    """Per-pixel brightness multipliers and colours for a strip of `count` pixels.

    Both strips query this alternately with their own settings, hence a dict
    rather than a single slot.
    """
    effect = getattr(ledsettings, "backlight_effect", NONE)
    cycles = max(0.1, float(getattr(ledsettings, "backlight_effect_cycles", 2)))
    minimum = min(max(int(getattr(ledsettings, "backlight_effect_min_percent", 20)), 0), 100) / 100.0
    brightness = ledsettings.backlight_brightness_percent / 100.0
    red, green, blue = ledsettings.backlight_red, ledsettings.backlight_green, ledsettings.backlight_blue

    key = (effect, count, cycles, minimum, brightness, red, green, blue)
    cached = _CACHE.get(key)
    if cached is not None:
        return cached

    if effect == WAVE and count > 0:
        span = 1.0 - minimum
        levels = [minimum + span * (math.sin(2 * math.pi * cycles * i / count) + 1) / 2
                  for i in range(count)]
    else:
        levels = [1.0] * count

    colors = [Color(int(red * brightness * level),
                    int(green * brightness * level),
                    int(blue * brightness * level)) for level in levels]

    if len(_CACHE) >= _CACHE_LIMIT:
        _CACHE.clear()
    _CACHE[key] = (levels, colors)
    return levels, colors


def brightness_at(ledsettings, index, count):
    """Backlight brightness multiplier for one pixel. 1.0 when no effect is set."""
    levels, _ = _profile(ledsettings, count)
    return levels[index] if 0 <= index < len(levels) else 1.0


def backlight_color(ledsettings, index, count):
    """The backlight colour for one pixel, with brightness and effect applied."""
    _, colors = _profile(ledsettings, count)
    return colors[index] if 0 <= index < len(colors) else Color(0, 0, 0)


def backlight_colors(ledsettings, count):
    """The backlight colour for every pixel, for painting a whole strip at once."""
    return _profile(ledsettings, count)[1]
