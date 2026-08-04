import time

from rpi_ws281x import Color

from lib.backlight_effects import backlight_color
from lib.functions import get_note_position, find_between
from lib.log_setup import logger

# Import app_state to check practice_active flag
try:
    from webinterface import app_state
except ImportError:
    # If webinterface is not available, create a dummy app_state
    class DummyAppState:
        practice_active = False
    app_state = DummyAppState()

OFF_COLOR = Color(0, 0, 0)


class MIDIEventProcessor:
    """
    Processes MIDI events and translates them into LED strip visualizations.
    """
    def __init__(self, midiports, ledstrip, ledsettings, usersettings, saving, learning, menu, color_mode,
                 state_manager=None, notes_only=False):
        # The second strip renders notes and the pedal only; recording, sequences
        # and lessons stay with the first.
        self.notes_only = notes_only
        self.midiports = midiports
        self.ledstrip = ledstrip
        self.ledsettings = ledsettings
        self.usersettings = usersettings
        self.saving = saving
        self.learning = learning
        self.menu = menu
        self.color_mode = color_mode
        self.state_manager = state_manager
        self.last_sustain = 0  # Track sustain pedal state
        # Time tracking for sequence advancement to prevent rapid triggering
        self.last_sequence_advance = 0

    def collect_events(self):
        """Take a bounded slice of pending MIDI messages off the queue.

        Kept separate from rendering because the queue can only be drained once:
        with two strips, both render the same batch rather than each popping a
        different half of the stream.
        """
        # Determine which MIDI queue to process based on playback state and practice mode
        if not self.saving.is_playing_midi and not self.learning.is_started_midi:
            # Check if practice mode is active (websocket MIDI takes priority)
            if hasattr(app_state, 'practice_active') and app_state.practice_active:
                # Process websocket MIDI input (from practice tool)
                self.midiports.midipending = self.midiports.websocket_midi_queue
            else:
                # Process regular live MIDI input
                self.midiports.midipending = self.midiports.midi_queue
        else:
            # Process MIDI file playback
            self.midiports.midipending = self.midiports.midifile_queue

        # Process a bounded slice per frame to avoid jitter and keep FPS stable
        # group near-identical timestamps and process notes first
        t0 = time.perf_counter()
        collected = 0

        # Bounded drain with bursts grouped by timestamp (~1.5ms window)
        BURST_WINDOW = 0.0015  # 1.5 ms
        BURST_LIMIT = 64       # avoid starving under continuous streams

        def _unpack_queue_item(item):
            # (msg, ts) or (msg, ts, source)
            if len(item) >= 3:
                return item[0], item[1], item[2]
            return item[0], item[1], None

        events = []
        midipending = self.midiports.midipending
        while midipending and collected < 512 and (time.perf_counter() - t0) < 0.003:
            head_msg, head_ts, head_source = _unpack_queue_item(midipending.popleft())
            burst = [(head_msg, head_ts, head_source)]
            # Coalesce a small burst of messages with almost the same timestamp
            while midipending and len(burst) < BURST_LIMIT:
                nxt_msg, nxt_ts, nxt_source = _unpack_queue_item(midipending[0])
                if abs(nxt_ts - head_ts) <= BURST_WINDOW:
                    midipending.popleft()
                    burst.append((nxt_msg, nxt_ts, nxt_source))
                else:
                    break

            # Notes first (reduce visual latency for chords), then others
            for notes_pass in (True, False):
                for m, ts, src in burst:
                    if (getattr(m, "type", None) in ("note_on", "note_off")) is not notes_pass:
                        continue
                    events.append((m, ts, src))
                    collected += 1
                    if collected >= 512:
                        break
                if collected >= 512:
                    break

        return events

    def render_events(self, events):
        """Render an already-drained batch of MIDI messages onto this strip."""
        if not events:
            return False

        midi_logging_enabled = int(self.usersettings.get_setting_value("midi_logging")) == 1
        log_sink = self.learning.socket_send if midi_logging_enabled else None
        midiports = self.midiports
        ledstrip = self.ledstrip
        ledsettings = self.ledsettings
        color_mode = self.color_mode
        saving = self.saving
        handle_note_off = self.handle_note_off
        handle_note_on = self.handle_note_on
        handle_control_change = self.handle_control_change
        get_position = get_note_position
        led_count = ledstrip.led_number
        notes_only = self.notes_only

        midi_mode = "light_show"
        try:
            midi_mode = midiports.get_midi_mode()
        except Exception:
            midi_mode = self.usersettings.get_setting_value("midi_mode") or "light_show"

        for msg, msg_timestamp, source in events:
            # piano/computer already logged in MidiPorts
            if (
                midi_logging_enabled
                and log_sink is not None
                and not notes_only
                and not getattr(msg, "is_meta", False)
                and source not in ("piano", "computer")
            ):
                try:
                    tag = source if source else "other"
                    log_sink.append("midi_event[{}] {}".format(tag, msg))
                except Exception as e:
                    logger.warning(f"[process midi events] Unexpected exception occurred: {e}")

            if not notes_only:
                midiports.last_activity = time.time()
                # Update state manager for MIDI activity
                if self.state_manager:
                    self.state_manager.update_midi_activity()

            msg_type = getattr(msg, "type", None)
            velocity = getattr(msg, "velocity", 0)

            # in learning, piano note_ons don't light LEDs (computer guide notes do).
            # The second strip sits outside lessons, so it always lights them.
            skip_note_on_lighting = (
                not notes_only
                and midi_mode == "learning"
                and source == "piano"
                and msg_type == "note_on"
                and velocity > 0
            )

            if ledsettings.mode != "Disabled" and msg_type in ("note_on", "note_off"):
                note_position = get_position(msg.note, ledstrip, ledsettings)
                if 0 <= note_position < led_count:
                    if msg_type == "note_off" or velocity == 0:
                        handle_note_off(msg, msg_timestamp, note_position)
                    elif velocity > 0:
                        if skip_note_on_lighting:
                            # still record, just don't light
                            if saving.is_recording:
                                saving.add_track("note_on", msg.note, velocity, msg_timestamp)
                        else:
                            handle_note_on(msg, msg_timestamp, note_position)
            elif msg_type == "control_change":
                handle_control_change(msg, msg_timestamp)

            if not skip_note_on_lighting:
                color_mode.MidiEvent(msg, None, ledstrip)
            if not notes_only:
                saving.restart_time()

        return True
    
    def handle_note_off(self, msg, msg_timestamp, note_position):
        """
        Handle note-off MIDI events.
        
        Turns off the corresponding LED or applies fading effects based on the current mode.
        
        Args:
            msg: The MIDI message object
            msg_timestamp: Timestamp when the message was received
            note_position: Position on the LED strip corresponding to the note
        """
        # Extract channel from message to check if it's from external software
        channel = find_between(str(msg), "channel=", " ")
        # Strip trailing commas (mido message format: "channel=12, note=60...")
        channel = channel.rstrip(',') if channel else False
        
        # Clear external software tracking flag if external software turns off the LED
        # Allow local piano input to also turn off LEDs even if they were lit by external software
        # This is essential for learning mode where Synthesia lights the LED (channels 11/12)
        # but the user's piano (channel 0) should be able to turn it off
        if self.ledstrip.keylist_external_software[note_position] == 1:
            if channel == "12" or channel == "11":
                # External software is turning off the LED - clear tracking
                self.ledstrip.keylist_external_software[note_position] = 0
        
        velocity = 0
        self.ledstrip.keylist_status[note_position] = 0

        # Check if sustain pedal is active for Velocity and Pedal modes
        pedal_deadzone = 10  # Standard MIDI deadzone for sustain pedal
        sustain_active = (self.ledsettings.mode in ["Velocity", "Pedal"] and 
                         self.last_sustain >= pedal_deadzone)

        if sustain_active:
            # Mark note as sustained instead of turning off
            self.ledstrip.keylist_sustained[note_position] = 1
        else:
            # Apply different effects based on the current LED mode
            if self.ledsettings.mode == "Fading":
                # Set to fading state (1000+ indicates fading)
                self.ledstrip.keylist[note_position] = 1000
            elif self.ledsettings.mode == "Normal":
                # Standard mode - full brightness while key is pressed
                self.ledstrip.keylist[note_position] = 0
            elif self.ledsettings.mode == "Pulse":
                # Find the active pulse for this note and trigger release
                for pulse in self.ledstrip.active_pulses:
                    if pulse["position"] == note_position and pulse.get("state") != "release":
                        pulse["state"] = "release"
                        pulse["release_time"] = time.perf_counter()
                        # Keep the pulse active in the list so led_effects_processor handles the animation
                        # We don't set keylist to 0 because the pulse effect handles the LED status
            elif self.ledsettings.mode == "Pedal":
                # Gradually reduce brightness based on pedal settings
                self.ledstrip.keylist[note_position] *= (100 - self.ledsettings.fadepedal_notedrop) / 100

        # If LED is completely off, set appropriate color
        if self.ledstrip.keylist[note_position] <= 0:
            self._apply_idle_color(note_position, self._backlight_is_visible())

        # Record the note-off event if recording is active
        if self.saving.is_recording and not self.notes_only:
            self.saving.add_track("note_off", msg.note, velocity, msg_timestamp)

    def handle_note_on(self, msg, msg_timestamp, note_position):
        """
        Handle note-on MIDI events.
        
        Illuminates the corresponding LED with appropriate color based on current settings,
        velocity sensitivity, and various modes.
        
        Args:
            msg: The MIDI message object
            msg_timestamp: Timestamp when the message was received
            note_position: Position on the LED strip corresponding to the note
        """
        velocity = msg.velocity

        # Get color from color mode handler
        color = self.color_mode.NoteOn(msg, msg_timestamp, None, note_position)
        if color is not None:
            red, green, blue = color
        else:
            red, green, blue = (0, 0, 0)

        # Store the note color
        self.ledstrip.keylist_color[note_position] = [red, green, blue]

        # Set this key as active and clear sustained status
        self.ledstrip.keylist_status[note_position] = 1
        self.ledstrip.keylist_sustained[note_position] = 0
        
        # Calculate brightness based on velocity if in velocity mode
        if self.ledsettings.mode == "Velocity":
            brightness = velocity / 127.0  # Linear mapping: 0-127 velocity -> 0-1 brightness
        else:
            brightness = 1

        # Apply different effects based on the current LED mode
        if self.ledsettings.mode == "Fading":
            # 1001 indicates the key is active and will start fading when released
            self.ledstrip.keylist[note_position] = 1001
        elif self.ledsettings.mode == "Velocity":
            # Brightness varies with velocity (999 * brightness for linear scaling)
            self.ledstrip.keylist[note_position] = 999 * brightness
        elif self.ledsettings.mode == "Normal":
            # Standard mode - full brightness while key is pressed
            self.ledstrip.keylist[note_position] = 1000
        elif self.ledsettings.mode == "Pedal":
            # For pedal mode, start at 999 (will be affected by pedal status)
            self.ledstrip.keylist[note_position] = 999
        elif self.ledsettings.mode == "Pulse":
            # Create a new pulse effect
            self.ledstrip.active_pulses.append({
                "position": note_position,
                "color": (red, green, blue),
                "start_time": time.perf_counter(),
                "velocity": velocity / 127.0,
                "state": "attack",
                "release_time": None
            })
            self.ledstrip.keylist[note_position] = 0  # Pulse handles lighting

        # Handle special channels for hand coloring (channels 11 and 12)
        channel = find_between(str(msg), "channel=", " ")
        # Strip trailing commas (mido message format: "channel=12, note=60...")
        channel = channel.rstrip(',') if channel else False
        if channel == "12" or channel == "11":
            # Mark this LED as externally controlled by external software
            self.ledstrip.keylist_external_software[note_position] = 1
            if self.ledsettings.skipped_notes != "Finger-based":
                # Apply right hand or left hand color
                if channel == "12":
                    hand_color = self.learning.hand_colorR
                else:
                    hand_color = self.learning.hand_colorL

                red, green, blue = map(int, self.learning.hand_colorList[hand_color])
                s_color = Color(red, green, blue)
                self.ledstrip.strip.setPixelColor(note_position, s_color)
                self.ledstrip.set_adjacent_colors(note_position, s_color, False)
        else:
            # Normal channel is taking control - clear external software flag
            if self.ledstrip.keylist_external_software[note_position] == 1:
                self.ledstrip.keylist_external_software[note_position] = 0
            
            if self.ledsettings.skipped_notes != "Normal":
                # Apply standard note color with velocity-based brightness
                s_color = Color(int(int(red) / float(brightness)), int(int(green) / float(brightness)),
                                int(int(blue) / float(brightness)))
                self.ledstrip.strip.setPixelColor(note_position, s_color)
                self.ledstrip.set_adjacent_colors(note_position, s_color, False)

        # Record the note-on event if recording is active
        if self.saving.is_recording and not self.notes_only:
            if self.ledsettings.color_mode == "Multicolor":
                import webcolors as wc
                # Include color information in multicolor mode
                self.saving.add_track("note_on", msg.note, velocity, msg_timestamp,
                                      wc.rgb_to_hex((red, green, blue)))
            else:
                self.saving.add_track("note_on", msg.note, velocity, msg_timestamp)

    def handle_control_change(self, msg, msg_timestamp):
        """
        Handle control change MIDI events.
        
        Processes pedal events, sequence advancement triggers, and other control messages.
        
        Args:
            msg: The MIDI message object
            msg_timestamp: Timestamp when the message was received
        """
        control = msg.control
        value = msg.value

        # all notes off (synthesia sends this when leaving a song)
        if control == 123:
            self.clear_all_note_leds()

        # Track sustain pedal state (MIDI CC 64)
        if control == 64:  # Sustain pedal
            self.last_sustain = value
            
            # Handle sustain pedal release - clear all sustained notes
            pedal_deadzone = 10  # Standard MIDI deadzone for sustain pedal
            if value < pedal_deadzone and self.ledsettings.mode in ["Velocity", "Pedal"]:
                show_backlight = self._backlight_is_visible()
                for i, sustained in enumerate(self.ledstrip.keylist_sustained):
                    if sustained == 1:
                        # Clear sustained status
                        self.ledstrip.keylist_sustained[i] = 0
                        # If key is not currently pressed, turn it off
                        if self.ledstrip.keylist_status[i] == 0:
                            self.ledstrip.keylist[i] = 0  # Turn off immediately
                            self._apply_idle_color(i, show_backlight)

        current_time = time.time()
        # Handle sequence advancement based on control values
        if not self.notes_only and self.ledsettings.sequence_active and self.ledsettings.next_step is not None:
            try:
                # Check if the incoming control matches the configured control for sequence advancement
                if int(control) == int(self.ledsettings.control_number):
                    # Sequence advancement logic:
                    # - If next_step > 0: advance when control value exceeds threshold
                    # - If next_step = -1: advance when control value = 0 (released)
                    if (int(self.ledsettings.next_step) > 0 and int(value) > int(self.ledsettings.next_step)) or \
                       (int(self.ledsettings.next_step) == -1 and int(value) == 0):
                        # Limit advancement frequency to prevent rapid triggering
                        if (current_time - self.last_sequence_advance) > 1:
                            self.ledsettings.set_sequence(0, 1)
                            self.last_sequence_advance = current_time
            except TypeError:
                logger.warning("TypeError encountered in sequence logic")
            except Exception as e:
                logger.warning(f"[handle control change] Unexpected exception occurred: {e}")

        # Record the control change if recording is active
        if self.saving.is_recording and not self.notes_only:
            self.saving.add_control_change("control_change", 0, control, value, msg_timestamp)

    def clear_all_note_leds(self):
        show_backlight = self._backlight_is_visible()
        led_count = self.ledstrip.led_number
        self.ledstrip.keylist = [0] * led_count
        self.ledstrip.keylist_status = [0] * led_count
        self.ledstrip.keylist_sustained = [0] * led_count
        self.ledstrip.keylist_external_software = [0] * led_count
        if hasattr(self.ledstrip, "keylist_color") and self.ledstrip.keylist_color is not None:
            self.ledstrip.keylist_color = [0] * led_count
        if hasattr(self.ledstrip, "active_pulses") and self.ledstrip.active_pulses is not None:
            self.ledstrip.active_pulses.clear()
        for i in range(led_count):
            self._apply_idle_color(i, show_backlight)
        try:
            self.ledstrip.strip.show()
        except Exception:
            pass

    def _backlight_is_visible(self):
        screensaver = self.menu is not None and self.menu.screensaver_is_running
        return self.ledsettings.backlight_brightness > 0 and not screensaver

    def _apply_idle_color(self, note_position, show_backlight):
        """Return a key's LED to the backlight, or switch it off."""
        if show_backlight:
            color_value = backlight_color(self.ledsettings, note_position, self.ledstrip.led_number)
        else:
            color_value = OFF_COLOR
        self.ledstrip.strip.setPixelColor(note_position, color_value)
        self.ledstrip.set_adjacent_colors(note_position, color_value, show_backlight)
