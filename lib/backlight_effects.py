"""Backlight effects.

The backlight is whatever a pixel shows when no note is holding it. These effects
shape its brightness along the length of the strip instead of leaving it flat.

The effect is a pure function of the pixel index, so it is applied at the points
that already paint the backlight rather than by repainting the strip. Note
lighting therefore keeps precedence for free: a lit key, its fade, and its
adjacent colours all overwrite the backlight exactly as before, and the effect
only reappears once the pixel returns to idle.
"""

import math

NONE = "None"
WAVE = "Wave"
EFFECTS = [NONE, WAVE]

# Per-pixel brightness multipliers, cached per (effect, count, cycles, minimum).
# The profile is static, so it is only computed once per distinct settings tuple.
# A dict rather than one slot: the two strips have their own settings and are
# queried alternately, which would thrash a single-entry cache.
_LEVEL_CACHE = {}
_CACHE_LIMIT = 8


def _levels(ledsettings, count):
    """Brightness multipliers for a strip of `count` pixels, 0.0 to 1.0."""
    effect = getattr(ledsettings, "backlight_effect", NONE)
    cycles = max(0.1, float(getattr(ledsettings, "backlight_effect_cycles", 2)))
    minimum = min(max(int(getattr(ledsettings, "backlight_effect_min_percent", 20)), 0), 100) / 100.0

    key = (effect, count, cycles, minimum)
    if key in _LEVEL_CACHE:
        return _LEVEL_CACHE[key]

    if effect == WAVE and count > 0:
        span = 1.0 - minimum
        levels = [minimum + span * (math.sin(2 * math.pi * cycles * i / count) + 1) / 2
                  for i in range(count)]
    else:
        levels = None  # flat: callers skip the lookup entirely

    if len(_LEVEL_CACHE) >= _CACHE_LIMIT:
        _LEVEL_CACHE.clear()
    _LEVEL_CACHE[key] = levels
    return levels


def brightness_at(ledsettings, index, count):
    """Backlight brightness multiplier for one pixel. 1.0 when no effect is set."""
    levels = _levels(ledsettings, count)
    if levels is None or not 0 <= index < len(levels):
        return 1.0
    return levels[index]


def scaled_backlight_rgb(ledsettings, index, count):
    """The backlight colour for one pixel, with brightness and effect applied."""
    scale = (ledsettings.backlight_brightness_percent / 100.0) * brightness_at(ledsettings, index, count)
    return (int(ledsettings.backlight_red * scale),
            int(ledsettings.backlight_green * scale),
            int(ledsettings.backlight_blue * scale))
