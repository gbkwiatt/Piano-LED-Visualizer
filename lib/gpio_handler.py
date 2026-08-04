import time

from lib.log_setup import logger
from lib.rpi_drivers import GPIO

from lib.functions import fastColorWipe


class GPIOHandler:
    def __init__(self, args, midiports, menu, ledstrip, ledsettings, usersettings, state_manager=None):
        self.args = args
        self.midiports = midiports
        self.menu = menu
        self.ledstrip = ledstrip
        self.ledsettings = ledsettings
        self.usersettings = usersettings
        self.state_manager = state_manager
        self.disable_hat = str(usersettings.get_setting_value("disable_hat")) == "1"
        self.disabled_buttons = set()
        self.setup_gpio()

    def setup_gpio(self):
        if self.disable_hat:
            # LCD control HAT disabled: leave the joystick/button pins (incl. GPIO5
            # and GPIO13) free for another HAT. See the `disable_hat` setting.
            return
        if self.args.rotatescreen != "true":
            self.KEYRIGHT = 26
            self.KEYLEFT = 5
            self.KEYUP = 6
            self.KEYDOWN = 19
            self.KEY1 = 21
            self.KEY3 = 16
        else:
            self.KEYRIGHT = 5
            self.KEYLEFT = 26
            self.KEYUP = 19
            self.KEYDOWN = 6
            self.KEY1 = 16
            self.KEY3 = 21

        self.KEY2 = 20
        self.JPRESS = 13
        self.BACKLIGHT = 24

        # A pin driving an LED strip must not also be polled as a button: the data
        # stream would read as a stuck keypress. The HAT's joystick press (13) and
        # one direction key (19) are the only PWM channel 1 pins on the header, so
        # a second strip always takes one of them.
        led_pins = {self.ledstrip.LED_PIN}
        if self.ledstrip.strip_secondary is not None:
            led_pins.add(self.ledstrip.LED_PIN2)
        self.disabled_buttons = led_pins

        GPIO.setmode(GPIO.BCM)
        for pin in (self.KEYRIGHT, self.KEYLEFT, self.KEYUP, self.KEYDOWN,
                    self.KEY1, self.KEY2, self.KEY3, self.JPRESS):
            if pin in led_pins:
                logger.warning(f"GPIO {pin} drives an LED strip, so its HAT button is disabled.")
                continue
            GPIO.setup(pin, GPIO.IN, GPIO.PUD_UP)

    def _pressed(self, pin):
        return pin not in self.disabled_buttons and GPIO.input(pin) == 0

    def process_gpio_keys(self):
        if self.disable_hat:
            return
        if self._pressed(self.KEYUP):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.change_pointer(0)
            while self._pressed(self.KEYUP):
                time.sleep(0.001)

        if self._pressed(self.KEYDOWN):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.change_pointer(1)
            while self._pressed(self.KEYDOWN):
                time.sleep(0.001)

        if self._pressed(self.KEY1):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.enter_menu()
            while self._pressed(self.KEY1):
                time.sleep(0.001)

        if self._pressed(self.KEY2):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.go_back()
            if not self.menu.screensaver_is_running:
                fastColorWipe(self.ledstrip.strip, True, self.ledsettings)
            while self._pressed(self.KEY2):
                time.sleep(0.01)

        if self._pressed(self.KEY3):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            if self.ledsettings.sequence_active:
                self.ledsettings.set_sequence(0, 1)
            else:
                current = self.usersettings.get_setting_value("midi_mode") or "light_show"
                new_mode = "learning" if current != "learning" else "light_show"
                self.midiports.set_midi_mode(new_mode)
                label = "Learning" if new_mode == "learning" else "Light show"
                self.menu.render_message("MIDI Mode", label, 1000)
                fastColorWipe(self.ledstrip.strip, True, self.ledsettings)
            while self._pressed(self.KEY3):
                time.sleep(0.01)

        if self._pressed(self.KEYLEFT):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.change_value("LEFT")
            time.sleep(0.1)

        if self._pressed(self.KEYRIGHT):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.change_value("RIGHT")
            time.sleep(0.1)

        if self._pressed(self.JPRESS):
            self.midiports.last_activity = time.time()
            if self.state_manager:
                self.state_manager.update_user_activity()
            self.menu.speed_change()
            while self._pressed(self.JPRESS):
                time.sleep(0.01)
