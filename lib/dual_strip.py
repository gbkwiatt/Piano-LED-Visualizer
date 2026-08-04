"""Support for driving a second LED strip alongside the main one.

rpi_ws281x's PixelStrip owns an entire ws2811_t, and two of those cannot
coexist: each one claims the PWM peripheral during ws2811_init, so the second
init fails or corrupts the first. Two strips therefore have to share a single
ws2811_t with both of its channels configured - channel 0 drives a PWM0 pin
(12/18) and channel 1 a PWM1 pin (13/19). DualChannelController builds that,
and Ws2811Channel exposes each channel with the same surface PixelStrip has so
the rest of the codebase does not care which one it is talking to.
"""

import atexit
import math

from lib.log_setup import logger
from lib.rpi_drivers import ws

PWM_CHANNEL_BY_PIN = {12: 0, 18: 0, 13: 1, 19: 1, 41: 1, 45: 1, 53: 1}


def channel_for_pin(pin):
    return PWM_CHANNEL_BY_PIN.get(int(pin))


class Ws2811Channel:
    """One channel of a shared ws2811 controller, shaped like a PixelStrip."""

    def __init__(self, controller, channel_index, count):
        self._controller = controller
        self._channel = ws.ws2811_channel_get(controller.leds, channel_index)
        self.size = int(count)

    def numPixels(self):
        return self.size

    def setPixelColor(self, n, color):
        if 0 <= n < self.size:
            ws.ws2811_led_set(self._channel, int(n), int(color))

    def setPixelColorRGB(self, n, red, green, blue, white=0):
        self.setPixelColor(n, (white << 24) | (red << 16) | (green << 8) | blue)

    def getPixelColor(self, n):
        return ws.ws2811_led_get(self._channel, int(n))

    def getPixels(self):
        return [ws.ws2811_led_get(self._channel, i) for i in range(self.size)]

    def setBrightness(self, brightness):
        ws.ws2811_channel_t_brightness_set(self._channel, int(brightness))

    def show(self):
        self._controller.show()


class DualChannelController:
    """A single ws2811_t with both PWM channels configured."""

    def __init__(self, specs, freq_hz, dma, invert, strip_type):
        """specs: {channel_index: (count, pin, brightness)} for channels 0 and 1."""
        self._initialized = False
        self.leds = ws.new_ws2811_t()
        ws.ws2811_t_freq_set(self.leds, int(freq_hz))
        ws.ws2811_t_dmanum_set(self.leds, int(dma))

        for channel_index in (0, 1):
            count, pin, brightness = specs[channel_index]
            channel = ws.ws2811_channel_get(self.leds, channel_index)
            ws.ws2811_channel_t_count_set(channel, int(count))
            ws.ws2811_channel_t_gpionum_set(channel, int(pin))
            ws.ws2811_channel_t_invert_set(channel, 1 if invert else 0)
            ws.ws2811_channel_t_brightness_set(channel, int(brightness))
            ws.ws2811_channel_t_strip_type_set(channel, strip_type)

        self.channels = {i: Ws2811Channel(self, i, specs[i][0]) for i in (0, 1)}
        # PixelStrip does the same, so DMA stops even on an abrupt exit.
        atexit.register(self.cleanup)

    def begin(self):
        response = ws.ws2811_init(self.leds)
        if response != 0:
            raise RuntimeError(f"ws2811_init failed with code {response} "
                               f"({ws.ws2811_get_return_t_str(response)})")
        self._initialized = True

    def show(self):
        response = ws.ws2811_render(self.leds)
        if response != 0:
            raise RuntimeError(f"ws2811_render failed with code {response} "
                               f"({ws.ws2811_get_return_t_str(response)})")

    def set_gamma_factor(self, gamma):
        ws.ws2811_set_custom_gamma_factor(self.leds, float(gamma))

    def cleanup(self):
        if self.leds is None:
            return
        # ws2811_fini segfaults on a controller that never came up.
        if self._initialized:
            try:
                ws.ws2811_fini(self.leds)
            except Exception as e:
                logger.warning(f"ws2811_fini failed: {e}")
            self._initialized = False
        try:
            ws.delete_ws2811_t(self.leds)
        except Exception as e:
            logger.warning(f"delete_ws2811_t failed: {e}")
        self.leds = None
        atexit.unregister(self.cleanup)


def build_index_map(count1, density1, shift1, reverse1,
                    count2, density2, shift2, reverse2):
    """Map each pixel index of strip 1 onto a (start, end) range on strip 2.

    get_note_position() turns a note into `density * (note - 20) - offsets + shift`,
    optionally mirrored. Undoing that for strip 1 and re-applying it with strip 2's
    geometry keeps both strips pointing at the same note even when they differ in
    density, shift or direction. The shared per-note `offsets` term cancels out only
    when both densities match; otherwise it leaves an error of at most a couple of
    LEDs, which is the same order as the offsets themselves.
    """
    density1 = float(density1) or 1.0
    scale = float(density2) / density1

    def project(n):
        raw = (count1 - n) if reverse1 else n
        raw2 = scale * (raw - shift1) + shift2
        return (count2 - raw2) if reverse2 else raw2

    index_map = []
    for n in range(count1):
        low, high = project(n), project(n + 1)
        if low > high:
            low, high = high, low
        start = max(0, min(count2, int(math.floor(low))))
        end = max(0, min(count2, int(math.ceil(high))))
        if end <= start:
            end = min(count2, start + 1)
        index_map.append((start, end))
    return index_map


class MirroredStrip:
    """PixelStrip-compatible facade that writes to a primary and a secondary strip.

    Indices addressed by the rest of the app always belong to the primary strip;
    each one is remapped onto the secondary strip's own geometry.
    """

    def __init__(self, primary, secondary, index_map):
        self.primary = primary
        self.secondary = secondary
        self.set_index_map(index_map)
        # Two channels of one ws2811 controller are rendered by a single call.
        shared = getattr(primary, "_controller", None)
        self._show_secondary = shared is None or shared is not getattr(secondary, "_controller", None)

    def set_index_map(self, index_map):
        self._map = index_map
        self._map_len = len(index_map)

    def numPixels(self):
        return self.primary.numPixels()

    def setPixelColor(self, n, color):
        self.primary.setPixelColor(n, color)
        if 0 <= n < self._map_len:
            start, end = self._map[n]
            set_secondary = self.secondary.setPixelColor
            for i in range(start, end):
                set_secondary(i, color)

    def setPixelColorRGB(self, n, red, green, blue, white=0):
        self.setPixelColor(n, (white << 24) | (red << 16) | (green << 8) | blue)

    def getPixelColor(self, n):
        return self.primary.getPixelColor(n)

    def getPixels(self):
        return self.primary.getPixels()

    def setBrightness(self, brightness):
        self.primary.setBrightness(brightness)

    def show(self):
        self.primary.show()
        if self._show_secondary:
            self.secondary.show()
