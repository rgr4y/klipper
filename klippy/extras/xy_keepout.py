# Boxie (Tronxy D01): Marlin-style XY keep-out corner.
# Port of Rob's Marlin xy-keepout.patch: once X and Y are homed, any move
# whose target Y <= y_max gets its X pushed up to at least x_min, so the
# CR Touch can't hit the front-left frame. Acts like a soft endstop clamp.
#
# [xy_keepout]
# x_min: 21
# y_max: 48

class XYKeepout:
    def __init__(self, config):
        self.printer = config.get_printer()
        self.x_min = config.getfloat('x_min')
        self.y_max = config.getfloat('y_max')
        self.enabled = True
        self.next_transform = None
        self.toolhead = None
        self.printer.register_event_handler("klippy:connect",
                                            self._handle_connect)
        gcode = self.printer.lookup_object('gcode')
        gcode.register_command('SET_XY_KEEPOUT', self.cmd_SET_XY_KEEPOUT,
                               desc="Enable/disable the XY keep-out corner")
    def _handle_connect(self):
        self.toolhead = self.printer.lookup_object('toolhead')
        gcode_move = self.printer.lookup_object('gcode_move')
        self.next_transform = gcode_move.set_move_transform(self, force=True)
    def get_position(self):
        return self.next_transform.get_position()
    def move(self, newpos, speed):
        if self.enabled and newpos[1] <= self.y_max and newpos[0] < self.x_min:
            homed = self.toolhead.get_kinematics().get_status(
                self.printer.get_reactor().monotonic())['homed_axes']
            if 'x' in homed and 'y' in homed:
                newpos = list(newpos)
                newpos[0] = self.x_min
        self.next_transform.move(newpos, speed)
    def cmd_SET_XY_KEEPOUT(self, gcmd):
        self.enabled = bool(gcmd.get_int('ENABLE', 1, minval=0, maxval=1))
        gcmd.respond_info("XY keep-out %s (X>=%.1f when Y<=%.1f)" % (
            "enabled" if self.enabled else "disabled", self.x_min, self.y_max))
    def get_status(self, eventtime):
        return {'enabled': self.enabled, 'x_min': self.x_min,
                'y_max': self.y_max}

def load_config(config):
    return XYKeepout(config)
