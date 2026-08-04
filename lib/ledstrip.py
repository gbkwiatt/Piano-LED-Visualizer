from lib.functions import *
import lib.colormaps as cmap
from lib.rpi_drivers import PixelStrip, ws
from lib.LED_drivers import PixelStrip_Emu
from lib.dual_strip import DualChannelController, channel_for_pin
from lib.log_setup import logger


def _setting_int(usersettings, key, default):
    """Read an int setting. `key` is a flat name or a (section, name) tuple."""
    try:
        return int(usersettings.get_setting_value(key))
    except (ValueError, TypeError):
        return default


STRIP2 = "strip2"


class LedStrip:
    """One LED strip: its geometry, its per-pixel note state, and its pixel target.

    `namespace` selects which settings section the geometry comes from, matching
    LedSettings. `strip` supplies a ready-made pixel target instead of creating
    hardware, which is how the second strip is handed a channel of the shared
    ws2811 controller that the first strip owns.
    """

    def __init__(self, usersettings, ledsettings, driver="rpi_ws281x", namespace=None, strip=None):
        self.usersettings = usersettings
        self.ledsettings = ledsettings
        self.driver = driver
        self.namespace = namespace

        self.brightness_percent = _setting_int(usersettings, self._key("brightness_percent"), 50)
        self.led_number = max(1, _setting_int(usersettings, self._key("led_count"), 176))
        self.leds_per_meter = max(1, _setting_int(usersettings, self._key("leds_per_meter"), 144))
        self.shift = _setting_int(usersettings, self._key("shift"), 0)
        self.reverse = _setting_int(usersettings, self._key("reverse"), 0)

        self.brightness = 255 * self.brightness_percent / 100
        # Gamma is a property of the shared controller, so it is never per-strip.
        self.led_gamma = float(usersettings.get_setting_value("led_gamma"))

        self.strip2_enabled = bool(_setting_int(usersettings, "led_strip2_enabled", 0))
        self.brightness_percent2 = _setting_int(usersettings, (STRIP2, "brightness_percent"), 50)
        self.led_number2 = max(1, _setting_int(usersettings, (STRIP2, "led_count"), 176))
        self.brightness2 = 255 * self.brightness_percent2 / 100
        self.LED_PIN2 = _setting_int(usersettings, (STRIP2, "led_pin"), 13)

        self.controller = None
        self.strip_secondary = None
        self.external_strip = strip

        # Hold individual led state information, initialized in init_strip()
        self.keylist = None
        self.keylist_status = None
        self.keylist_color = None

        self.current_fps = 0

        # LED strip configuration:
        #self.LED_COUNT = int(self.led_number)  # Number of LED pixels.
        # Read LED pin and channel from settings, with fallback to defaults
        self.LED_PIN = _setting_int(self.usersettings, "led_pin", 18)  # 18 uses PWM!
        # LED_PIN        = 10      # GPIO pin connected to the pixels (10 uses SPI /dev/spidev0.0).
        self.LED_FREQ_HZ = 800000  # LED signal frequency in hertz (usually 800khz)
        self.LED_DMA = 10  # DMA channel to use for generating signal (try 10)
        #self.LED_BRIGHTNESS = int(self.brightness)  # Set to 0 for darkest and 255 for brightest
        self.LED_INVERT = False  # True to invert the signal (when using NPN transistor level shift)
        self.LED_CHANNEL = _setting_int(self.usersettings, "led_channel", 0)  # '1' for GPIO 13/19/41/45/53

        self.WEBEMU_FPS = 10

        self.init_strip()

    def _key(self, name):
        return name if self.namespace is None else (self.namespace, name)

    def init_strip(self):
        self.keylist = [0] * self.led_number
        self.keylist_status = [0] * self.led_number
        self.keylist_color = [0] * self.led_number
        self.keylist_sustained = [0] * self.led_number  # Track notes sustained by pedal
        self.keylist_external_software = [0] * self.led_number  # Track LEDs lit by external software (channels 11/12)
        self.active_pulses = [] # For Pulse mode

        if self.external_strip is not None:
            self.strip = self.external_strip
            return

        self._release_controller()
        self.strip_secondary = None

        if self.driver == "rpi_ws281x":
            if self.strip2_enabled and self._init_dual_ws281x():
                return
            try:
                # Create NeoPixel object with appropriate configuration.
                self.strip = PixelStrip(int(self.led_number), self.LED_PIN, self.LED_FREQ_HZ, self.LED_DMA, self.LED_INVERT,
                                            int(self.brightness), self.LED_CHANNEL, ws.WS2811_STRIP_GRB)
                # Intialize the library (must be called once before other functions).
                self.strip.begin()
                if "releaseGIL" in dir(self.strip):
                    self.strip.releaseGIL()
                self.change_gamma(self.led_gamma)
            except Exception as e:
                logger.warning(e)

                if isinstance(e, RuntimeError):
                    # rpi_ws281x registers _cleanup() atexit, but if it's not initialized ws2811_fini will segfault.
                    # Manually clean up memory, then bypass _cleanup() using knowledge that _cleanup() checks _leds first
                    logger.info("Cleaning up ws281x instance.")
                    ws.delete_ws2811_t(self.strip._leds)
                    self.strip._leds = None

                logger.info("Failed to load LED strip.  Using emu driver.")
                self.strip = PixelStrip_Emu(int(self.led_number))
                self.driver = "emu"
                if self.strip2_enabled:
                    self._attach_emu_secondary()
        elif self.driver == "emu":
            self.strip = PixelStrip_Emu(int(self.led_number))
            if self.strip2_enabled:
                self._attach_emu_secondary()

    def _init_dual_ws281x(self):
        """Bring both strips up on one controller. Returns False to fall back to a single strip."""
        primary_channel = channel_for_pin(self.LED_PIN)
        secondary_channel = channel_for_pin(self.LED_PIN2)

        if primary_channel is None or secondary_channel is None:
            logger.warning(f"Second LED strip disabled: pin {self.LED_PIN} or {self.LED_PIN2} "
                           "is not a supported PWM pin.")
            return False
        if primary_channel == secondary_channel:
            logger.warning(f"Second LED strip disabled: pins {self.LED_PIN} and {self.LED_PIN2} "
                           "share PWM channel " + str(primary_channel) +
                           ". Use one pin from 12/18 and one from 13/19.")
            return False

        specs = {
            primary_channel: (int(self.led_number), self.LED_PIN, int(self.brightness)),
            secondary_channel: (int(self.led_number2), self.LED_PIN2, int(self.brightness2)),
        }
        controller = None
        try:
            controller = DualChannelController(specs, self.LED_FREQ_HZ, self.LED_DMA,
                                               self.LED_INVERT, ws.WS2811_STRIP_GRB)
            controller.begin()
        except Exception as e:
            logger.warning(f"Failed to start dual LED strip: {e}")
            if controller is not None:
                controller.cleanup()
            return False

        self.controller = controller
        self.strip = controller.channels[primary_channel]
        self.strip_secondary = controller.channels[secondary_channel]
        self.change_gamma(self.led_gamma)
        logger.info(f"Second LED strip active on pin {self.LED_PIN2} "
                    f"({self.led_number2} LEDs, PWM channel {secondary_channel}).")
        return True

    def _attach_emu_secondary(self):
        self.strip_secondary = PixelStrip_Emu(int(self.led_number2))

    def _release_controller(self):
        if self.controller is not None:
            self.controller.cleanup()
            self.controller = None

    def cleanup(self):
        """Free the ws2811 hardware. Call before replacing this LedStrip with a new one,
        otherwise the next controller cannot claim the DMA channel."""
        self._release_controller()
        self.strip_secondary = None

    def change_gamma(self, value):
        self.led_gamma = float(value)
        if 0.01 <= self.led_gamma <= 10.0:
            if self.driver == "rpi_ws281x":
                # rpi_ws281x.py interface has no ported method to set gamma by factor, using direct ws
                if self.controller is not None:
                    self.controller.set_gamma_factor(self.led_gamma)
                else:
                    ws.ws2811_set_custom_gamma_factor(self.strip._leds, self.led_gamma)

            # Rebuild colormaps
            cmap.generate_colormaps(cmap.gradients, self.led_gamma)

    def change_brightness(self, value, ispercent=False):
        if ispercent:
            self.brightness_percent = value
        else:
            self.brightness_percent += value
        self.brightness_percent = clamp(self.brightness_percent, 1, 100)
        self.brightness = 255 * self.brightness_percent / 100

        self.usersettings.change_setting_value(self._key("brightness_percent"), self.brightness_percent)

        self.strip.setBrightness(int(self.brightness))

    def change_led_count(self, value, fixed_number=False):
        if fixed_number:
            self.led_number = value
        else:
            self.led_number += value
        self.led_number = max(1, self.led_number)

        self.usersettings.change_setting_value(self._key("led_count"), self.led_number)

        self.init_strip()

    def change_shift(self, value, fixed_number=False):
        if fixed_number:
            self.shift = value
        else:
            self.shift += value
        self.usersettings.change_setting_value(self._key("shift"), self.shift)

    def change_reverse(self, value, fixed_number=False):
        if fixed_number:
            self.reverse = value
        else:
            self.reverse += value
        self.reverse = clamp(self.reverse, 0, 1)
        self.usersettings.change_setting_value(self._key("reverse"), self.reverse)

    def change_leds_per_meter(self, value):
        self.leds_per_meter = max(1, int(value))
        self.usersettings.change_setting_value(self._key("leds_per_meter"), self.leds_per_meter)

    def set_adjacent_colors(self, note, color, led_turn_off, fading=1):
        if self.ledsettings.adjacent_mode == "RGB" and color != 0 and led_turn_off is not True:
            color = Color(int(self.ledsettings.adjacent_red * fading), int(self.ledsettings.adjacent_green * fading),
                          int(self.ledsettings.adjacent_blue * fading))
        if self.ledsettings.adjacent_mode != "Off":

            if 1 < note < (self.led_number - 2):
                if self.keylist_status[int(note) + 2] == 0:
                    self.strip.setPixelColor(int(note) + 1, color)

                if self.keylist_status[int(note) - 2] == 0:
                    self.strip.setPixelColor(int(note) - 1, color)
