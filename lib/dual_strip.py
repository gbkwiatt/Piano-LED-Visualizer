"""Support for driving a second LED strip alongside the main one.

rpi_ws281x's PixelStrip owns an entire ws2811_t, and two of those cannot
coexist: each one claims the PWM peripheral during ws2811_init, so the second
init fails or corrupts the first. Two strips therefore have to share a single
ws2811_t with both of its channels configured - channel 0 drives a PWM0 pin
(12/18) and channel 1 a PWM1 pin (13/19). DualChannelController builds that,
and Ws2811Channel exposes each channel with the same surface PixelStrip has, so
each strip drives its own channel as if it were an ordinary PixelStrip.
"""

import atexit

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
