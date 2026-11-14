#!/usr/bin/env python3
# Marlin-style status screen for the Tronxy D01 stock TFT (lerdge_tft).
#
# Recreates Rob's Marlin TFT_COLOR_UI 480x320 status screen (BLACK_MARLIN
# theme, STATUS_TEXT_TEMPS_HOME, Favorites, one-tap preheat/stop) with
# PIL and streams changed 16x16 tiles to the panel through lerdge_tft's
# "lerdge_tft/draw" API (encoder and flow control from lerdge_mirror.py).
# Touch comes back from "lerdge_tft/subscribe_touch".  No X server.
#
# Settings icon hands the panel to Klipper's built-in menu; the back
# button on the built-in status screen hands it back.
#
#   python3 d01_screen.py -s ~/printer_data/comms/klippy.sock
#   python3 d01_screen.py --preview out.png      (render a sample frame)
#
# Copyright (C) 2026  Rob Vella
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import argparse, base64, configparser, json, logging, os, queue, sys
import threading, time
import numpy as np
from PIL import Image, ImageDraw, ImageFont

######################################################################
# Paths
######################################################################

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
ASSETS = os.path.join(SCRIPT_DIR, "d01_screen_assets")
ICONS_JSON = os.path.join(ASSETS, "icons.json")
FONT_TTF = os.path.join(ASSETS, "NotoSans-Medium.ttf")
PRINTER_DATA = os.path.expanduser("~/printer_data")
DEFAULT_SOCKET = os.path.join(PRINTER_DATA, "comms", "klippy.sock")
DEFAULT_CONFIG_FILE = os.path.join(PRINTER_DATA, "config", "d01_screen.cfg")
DEFAULT_STATE_FILE = os.path.join(PRINTER_DATA, "d01_screen_state.json")

sys.path.insert(0, SCRIPT_DIR)
import lerdge_mirror as lm

# The D01 MCU is a 72 MHz STM32F103: every tft_fsmc_rle command runs in its
# command loop, ahead of queue_step/trsync.  Keep each command short (256
# pixels, ~60 us) and the display's share of the link small while gcode
# runs (see lerdge_tft._is_printing), or homing dies with "Timer too close".
lm.MAX_CHUNK_PIXELS = 256
BUSY_LINK_SHARE = 0.05
BUSY_MIN_INTERVAL = 1.0

W, H = 480, 320

######################################################################
# Marlin theme (tft_color.h, themes/theme_default.h + BLACK_MARLIN)
######################################################################

def c565(v):
    return ((v >> 11 & 0x1f) * 255 // 31, (v >> 5 & 0x3f) * 255 // 63,
            (v & 0x1f) * 255 // 31)

BLACK, WHITE, GREY = c565(0x0000), c565(0xFFFF), c565(0x7BEF)
SCARLET, DARK_ORANGE = c565(0xF904), c565(0xFC40)
YELLOW, CYAN, BLUE = c565(0xFFE0), c565(0x07FF), c565(0x001F)
VIVID_GREEN = c565(0x7FE0)
DARK_PURPLE = c565(0x9012)

COLOR_BACKGROUND = BLACK
COLOR_INACTIVE = GREY
COLOR_COLD = WHITE                  # BLACK_MARLIN
COLOR_HOTEND = SCARLET
COLOR_HEATED_BED = DARK_ORANGE
COLOR_FAN = WHITE                   # BLACK_MARLIN
COLOR_AXIS = WHITE
COLOR_RATE_100 = VIVID_GREEN
COLOR_RATE_ALTERED = YELLOW
COLOR_PRINT_TIME = CYAN
COLOR_PROGRESS_FRAME = WHITE
COLOR_PROGRESS_BAR = BLUE
COLOR_PROGRESS_BG = BLACK
COLOR_STATUS_MESSAGE = YELLOW
COLOR_ENABLED = WHITE
COLOR_DISABLED = GREY
COLOR_CANCEL = SCARLET
COLOR_MENU_TEXT = YELLOW
COLOR_SELECTION_BG = DARK_PURPLE

######################################################################
# Layout (ui_480x320.h with STATUS_TEXT_TEMPS_HOME, CUSTOM_MENU_MAIN,
# HOST_CONNECTED_INDICATOR, SHOW_ELAPSED_TIME + SHOW_REMAINING_TIME)
######################################################################

FONT_LINE_HEIGHT = 34
CELL_W, CELL_H = 104, 120           # TEMP_FAN_CONTROL_W/H
ITEM_Y = 2
def ITEM_X(n):                      # 4 slots: hotend, bed, favorites, home
    return (W // 4 - CELL_W) // 2 + W * n // 4
COORD = (4, 130, W - 8, FONT_LINE_HEIGHT)
X_MARK, X_VALUE_R = 16, 102
Y_MARK, Y_VALUE_R = 192, 280
Z_MARK, Z_VALUE_R, Z_VALUE_OFFSET = 330, 455, 32
ROW_Y = 178
FEED = (120, ROW_Y, 92, 32)
FAN = (214, ROW_Y, 92, 32)
FLOW = (308, ROW_Y, 92, 32)
HOST_ICON = (86, ROW_Y)
MENU_ICON = (404, ROW_Y)
SDCARD_ICON = (12, ROW_Y)           # preheat when idle, stop when printing
ELAPSED = (W // 2 - 164, 222, 164, 29)
REMAINING = (W // 2, 222, 164, 29)
PROGRESS = (4, 260, W - 8, 12)
STATUS_MSG = (0, 278, W, 36)
BUTTON_CANCEL = (88, H - 64)
BUTTON_CONFIRM = (328, H - 64)
BUTTON_DECREASE = (64, H - 64)
BUTTON_INCREASE = (352, H - 64)
MENU_ITEM_H = 43
NAV_Y = H - 34
NAV_UP, NAV_BACK, NAV_DOWN = (104, NAV_Y), (224, NAV_Y), (344, NAV_Y)

######################################################################
# Assets: Marlin GREYSCALE4 icons, NotoSans Medium (Marlin's TFT_FONT)
######################################################################

class Assets:
    def __init__(self):
        raw = json.load(open(ICONS_JSON))
        self.icons = {}
        for name, ic in raw.items():
            data = np.frombuffer(base64.b64decode(ic['data']), np.uint8)
            w, h = ic['w'], ic['h']
            data = data.reshape(h, (w + 1) // 2)
            idx = np.empty((h, data.shape[1] * 2), np.uint8)
            idx[:, 0::2] = data >> 4        # high nibble first (canvas.cpp)
            idx[:, 1::2] = data & 0x0f
            self.icons[name] = idx[:, :w]
        ttf = FONT_TTF
        # 26 px NotoSans Medium = capital 'A' height 19 px (Marlin FONT_SIZE 19)
        self.font = ImageFont.truetype(ttf, 26)
        self.big = ImageFont.truetype(ttf, 56)
        self.sized = {n: ImageFont.truetype(ttf, n) for n in range(16, 27)}
    def fit(self, draw, s, width):
        # Largest font (<= the 26 px menu font) that fits in width
        for n in range(26, 15, -1):
            if draw.textlength(s, font=self.sized[n]) <= width:
                return self.sized[n]
        return self.sized[16]

def gradient(a, b, factor):
    return tuple((ca * factor + cb * (256 - factor)) >> 8
                 for ca, cb in zip(a, b))

def icon_palette(main, bg=COLOR_BACKGROUND, shadow=BLACK):
    # TFT_Queue::add_image(): 1-7 bg->shadow, 8-15 bg->main, 0 transparent
    pal = [bg] * 16
    for i in range(1, 8):
        pal[i] = gradient(bg, shadow, i << 5)
    for i in range(8, 16):
        pal[i] = gradient(main, bg, ((i - 8) << 8) // 7)
    return np.array(pal, np.uint8)

######################################################################
# Drawing helpers
######################################################################

class Canvas:
    def __init__(self, assets):
        self.a = assets
        self.img = Image.new("RGB", (W, H), COLOR_BACKGROUND)
        self.d = ImageDraw.Draw(self.img)
        self.controls = []          # (x, y, w, h, action)
    def clear(self):
        self.d.rectangle((0, 0, W - 1, H - 1), fill=COLOR_BACKGROUND)
        self.controls = []
    def control(self, x, y, w, h, action):
        if action is not None:
            self.controls.append((x, y, w, h, action))
    def text_w(self, s, font=None):
        return self.d.textlength(s, font=font or self.a.font)
    def text(self, x, y, s, color, font=None, anchor="lm"):
        self.d.text((x, y), s, fill=color, font=font or self.a.font,
                    anchor=anchor)
    def text_center(self, x, y, w, h, s, color, font=None):
        self.text(x + w / 2, y + h / 2, s, color, font, "mm")
    def icon(self, x, y, name, color):
        idx = self.a.icons[name]
        h, w = idx.shape
        pal = icon_palette(color)
        region = np.array(self.img.crop((x, y, x + w, y + h)))
        mask = idx != 0
        region[mask] = pal[idx[mask]]
        self.img.paste(Image.fromarray(region), (x, y))
    def button(self, x, y, name, color, action, enabled=True):
        self.icon(x, y, name, color if enabled else COLOR_DISABLED)
        size = self.a.icons[name].shape[0]
        self.control(x, y, size, size, action if enabled else None)
    def rect(self, x, y, w, h, outline=None, fill=None):
        self.d.rectangle((x, y, x + w - 1, y + h - 1), outline=outline,
                         fill=fill)
    def text_button(self, x, y, w, h, s, color, action, selected=False):
        self.rect(x, y, w, h, outline=color,
                  fill=COLOR_SELECTION_BG if selected else None)
        self.text_center(x, y, w, h, s, color)
        self.control(x, y, w, h, action)
    def hit(self, px, py):
        for x, y, w, h, action in reversed(self.controls):
            if x <= px < x + w and y <= py < y + h:
                return action
        return None
    def rgb565(self):
        a = np.asarray(self.img, np.uint16)
        return (((a[:, :, 0] >> 3) << 11) | ((a[:, :, 1] >> 2) << 5)
                | (a[:, :, 2] >> 3)).astype(np.uint16)

def fmt_temp(t):
    return "%d°" % (int(round(t)),)

def fmt_duration(sec):
    # duration_t::toDigital(): "hh:mm", with days "dd:hh:mm"
    sec = int(max(0, sec))
    d, rem = divmod(sec, 86400)
    h, rem = divmod(rem, 3600)
    m = rem // 60
    if d:
        return "%02d:%02d:%02d" % (d, h, m)
    return "%02d:%02d" % (h, m)

######################################################################
# Printer state
######################################################################

QUERY = {
    'extruder': ['temperature', 'target'],
    'heater_bed': ['temperature', 'target'],
    'fan': ['speed'],
    'gcode_move': ['gcode_position', 'speed_factor', 'extrude_factor'],
    'toolhead': ['homed_axes'],
    'print_stats': ['state', 'print_duration', 'filename', 'message'],
    'virtual_sdcard': ['progress', 'is_active'],
    'display_status': ['message', 'progress'],
    'webhooks': ['state'],
    'idle_timeout': ['state'],
}

class State:
    def __init__(self, status=None):
        self.s = status or {}
        # Set while our own touch-beep M300 is the only gcode we sent: its
        # G4 dwell flips idle_timeout to "Printing" for a few hundred ms,
        # which must not lock the screen (and pop the menu just opened)
        self.beep_only = False
    def g(self, obj, key, default=None):
        return self.s.get(obj, {}).get(key, default)
    @property
    def printing(self):
        return self.g('print_stats', 'state') in ('printing', 'paused')
    @property
    def busy(self):
        # Any gcode running (homing, macros, prints): MCU link is critical
        if self.printing:
            return True
        return (self.g('idle_timeout', 'state') == 'Printing'
                and not self.beep_only)
    @property
    def paused(self):
        return self.g('print_stats', 'state') == 'paused'

######################################################################
# Screens
######################################################################

class Screen:
    def __init__(self, app):
        self.app = app
    def draw(self, c, st):
        raise NotImplementedError

class StatusScreen(Screen):
    def draw(self, c, st):
        app = self.app
        blink = int(time.time()) % 2 == 0
        # While gcode runs (print, homing, macros) only STOP and PAUSE act
        locked = st.busy
        # Heaters (text-only cells: label / current / target)
        for n, (obj, label, hot) in enumerate((
                ('extruder', "Hotend", COLOR_HOTEND),
                ('heater_bed', "Bed", COLOR_HEATED_BED))):
            x = ITEM_X(n)
            cur = st.g(obj, 'temperature')
            tgt = st.g(obj, 'target')
            if not locked:
                c.control(x, ITEM_Y, CELL_W, CELL_H,
                          lambda o=obj, l=label: app.edit_heater(o, l))
            if cur is None:
                color = COLOR_INACTIVE
            else:
                color = hot if cur >= 50 else COLOR_COLD
            def line(off, s, col):
                c.text_center(x + 2, ITEM_Y + off, CELL_W,
                              FONT_LINE_HEIGHT, s, col)
            line(6, label, COLOR_INACTIVE)
            line(43, fmt_temp(cur) if cur is not None else "---", color)
            if tgt is not None:
                line(80, fmt_temp(tgt), COLOR_INACTIVE if tgt == 0 else color)
        # Favorites and Home only while idle
        idle = not locked
        x = ITEM_X(2)
        c.button(x + (CELL_W - 64) // 2, ITEM_Y + 18, 'star',
                 COLOR_ENABLED, app.open_favorites, idle)
        c.text_center(x, ITEM_Y + 86, CELL_W, FONT_LINE_HEIGHT, "Favorites",
                      COLOR_ENABLED if idle else COLOR_DISABLED)
        x = ITEM_X(3)
        c.button(x + (CELL_W - 64) // 2, ITEM_Y + 18, 'home',
                 COLOR_ENABLED, lambda: app.gcode("G28"), idle)
        c.text_center(x, ITEM_Y + 86, CELL_W, FONT_LINE_HEIGHT, "Home",
                      COLOR_ENABLED if idle else COLOR_DISABLED)
        # Coordinates
        cx, cy, cw, ch = COORD
        c.rect(cx, cy, cw, ch, outline=COLOR_AXIS)
        mid = cy + ch / 2
        pos = st.g('gcode_move', 'gcode_position') or [0., 0., 0., 0.]
        homed = st.g('toolhead', 'homed_axes') or ""
        def axis(mark_x, value_r, name, value):
            c.text(cx + mark_x, mid, name, COLOR_AXIS)
            if name.lower() not in homed and blink:
                s = "?"
            else:
                s = "%d" % (int(round(value)),)
            c.text(cx + value_r, mid, s, COLOR_AXIS, anchor="rm")
        axis(X_MARK, X_VALUE_R, "X", pos[0])
        axis(Y_MARK, Y_VALUE_R, "Y", pos[1])
        c.text(cx + Z_MARK, mid, "Z", COLOR_AXIS)
        if 'z' not in homed and blink:
            c.text(cx + Z_VALUE_R - Z_VALUE_OFFSET, mid, "?", COLOR_AXIS,
                   anchor="rm")
        else:
            # Integer part right-aligned at a fixed point, like Marlin
            z = pos[2]
            ip, fp = ("%.2f" % (z,)).split(".")
            c.text(cx + Z_VALUE_R - Z_VALUE_OFFSET, mid, ip, COLOR_AXIS,
                   anchor="rm")
            c.text(cx + Z_VALUE_R - Z_VALUE_OFFSET, mid, "." + fp,
                   COLOR_AXIS, anchor="lm")
        # Preheat (idle) or STOP (busy; two taps)
        if locked:
            c.button(SDCARD_ICON[0], SDCARD_ICON[1], 'cancel',
                     YELLOW if app.stop_armed() else COLOR_CANCEL,
                     app.tap_stop)
        else:
            c.button(SDCARD_ICON[0], SDCARD_ICON[1], 'fire', COLOR_HOTEND,
                     app.preheat_selected)
        # Host link (klippy ready)
        if st.g('webhooks', 'state') == 'ready':
            c.icon(HOST_ICON[0], HOST_ICON[1], 'usb', WHITE)
        # Feed rate / fan / flow
        def rate(box, icon, value, color, action):
            x, y, w, h = box
            c.icon(x, y, icon, color)
            s = "%d%%" % (value,)
            c.text(x + 36, y + 15, s, color, c.a.fit(c.d, s, w - 36 - 2))
            if not locked:
                c.control(x, y, w, h, action)
        feed = int(round((st.g('gcode_move', 'speed_factor') or 1.) * 100))
        flow = int(round((st.g('gcode_move', 'extrude_factor') or 1.) * 100))
        fan = int(round((st.g('fan', 'speed') or 0.) * 100))
        rate(FEED, 'feedrate', feed,
             COLOR_RATE_100 if feed == 100 else COLOR_RATE_ALTERED,
             app.edit_feed)
        rate(FAN, 'fan', fan, COLOR_FAN, app.edit_fan)
        rate(FLOW, 'flowrate', flow,
             COLOR_RATE_100 if flow == 100 else COLOR_RATE_ALTERED,
             app.edit_flow)
        if locked:
            # PAUSE / RESUME (green while paused) in the settings slot
            c.button(MENU_ICON[0], MENU_ICON[1], 'pause',
                     COLOR_RATE_100 if st.paused else COLOR_ENABLED,
                     app.tap_pause, st.printing)
        else:
            c.button(MENU_ICON[0], MENU_ICON[1], 'settings', COLOR_ENABLED,
                     app.open_settings)
        # Elapsed / remaining
        elapsed = st.g('print_stats', 'print_duration') or 0.
        progress = st.g('display_status', 'progress')
        if not progress:
            progress = st.g('virtual_sdcard', 'progress') or 0.
        def timebox(box, icon, s):
            x, y, w, h = box
            tw = c.text_w(s)
            tx = x + (w - tw) / 2
            c.icon(int(tx - 18), y, icon, COLOR_PRINT_TIME)
            c.text(tx + 18, y + h / 2, s, COLOR_PRINT_TIME)
        timebox(ELAPSED, 'time_elapsed', fmt_duration(elapsed))
        if st.printing and progress > 0.01:
            remain = fmt_duration(elapsed * (1. - progress) / progress)
        else:
            remain = "--:--"
        timebox(REMAINING, 'time_remaining', remain)
        # Progress bar
        x, y, w, h = PROGRESS
        c.rect(x, y, w, h, outline=COLOR_PROGRESS_FRAME, fill=COLOR_PROGRESS_BG)
        bar = int((w - 2) * min(1., max(0., progress if st.printing else 0.)))
        if bar:
            c.rect(x + 1, y + 1, bar, h - 2, fill=COLOR_PROGRESS_BAR)
        # Status message
        msg = st.g('display_status', 'message')
        if locked and app.stop_armed():
            msg = ("Tap STOP again: cancel print" if st.printing
                   else "Tap STOP again: emergency stop")
        elif st.paused and not msg:
            msg = "Paused - tap pause to resume"
        if not msg:
            if st.paused:
                msg = "Print paused"
            elif st.printing:
                msg = st.g('print_stats', 'filename') or "Printing"
            elif st.g('webhooks', 'state') not in (None, 'ready'):
                msg = "Klipper %s" % (st.g('webhooks', 'state'),)
            else:
                msg = app.welcome
        x, y, w, h = STATUS_MSG
        c.text_center(x, y, w, h, msg, COLOR_STATUS_MESSAGE)

class EditScreen(Screen):
    """Value edit: big value, -/+ steps, optional presets, cancel/confirm."""
    def __init__(self, app, title, value, vmin, vmax, apply, steps=(1, 10),
                 presets=(), unit=""):
        Screen.__init__(self, app)
        self.title, self.value = title, int(round(value))
        self.vmin, self.vmax, self.apply = vmin, vmax, apply
        self.steps, self.step = steps, steps[0]
        self.presets, self.unit = presets, unit
    def add(self, d):
        self.value = max(self.vmin, min(self.vmax, self.value + d))
    def draw(self, c, st):
        c.text_center(0, 4, W, FONT_LINE_HEIGHT, self.title, COLOR_MENU_TEXT)
        c.text_center(0, 48, W, 70, "%d%s" % (self.value, self.unit),
                      COLOR_ENABLED, c.a.big)
        # Step size toggles
        bw = 88
        x0 = (W - bw * len(self.steps) - 12 * (len(self.steps) - 1)) // 2
        for i, s in enumerate(self.steps):
            c.text_button(x0 + i * (bw + 12), 128, bw, 40, "x%d" % (s,),
                          COLOR_ENABLED, lambda s=s: setattr(self, 'step', s),
                          selected=self.step == s)
        # Presets
        if self.presets:
            bw = 100
            x0 = (W - bw * len(self.presets) - 10 * (len(self.presets) - 1)) // 2
            for i, (label, v) in enumerate(self.presets):
                c.text_button(x0 + i * (bw + 10), 180, bw, 40, label,
                              COLOR_ENABLED,
                              lambda v=v: setattr(self, 'value', v))
        c.button(BUTTON_DECREASE[0], BUTTON_DECREASE[1], 'decrease',
                 COLOR_ENABLED, lambda: self.add(-self.step))
        c.button(BUTTON_INCREASE[0], BUTTON_INCREASE[1], 'increase',
                 COLOR_ENABLED, lambda: self.add(self.step))
        c.button(BUTTON_CANCEL[0] + 58, BUTTON_CANCEL[1], 'cancel',
                 COLOR_CANCEL, self.app.back)
        c.button(BUTTON_CONFIRM[0] - 58, BUTTON_CONFIRM[1], 'confirm',
                 COLOR_RATE_100, self.done)
    def done(self):
        self.apply(self.value)
        self.app.back()

class ListScreen(Screen):
    """Marlin-style menu list: 43 px rows, page up / back / page down."""
    ROWS = (NAV_Y - 4) // MENU_ITEM_H
    def __init__(self, app, title, items):
        Screen.__init__(self, app)
        self.title, self.items, self.top = title, items, 0
    def draw(self, c, st):
        items = self.items(st) if callable(self.items) else self.items
        c.text_center(0, 0, W, MENU_ITEM_H, self.title, COLOR_MENU_TEXT)
        rows = items[self.top:self.top + self.ROWS - 1]
        for i, (label, action) in enumerate(rows):
            y = MENU_ITEM_H * (i + 1)
            c.d.line((0, y, W, y), fill=COLOR_INACTIVE)
            c.text(16, y + MENU_ITEM_H / 2, label,
                   COLOR_MENU_TEXT if action else COLOR_DISABLED)
            c.control(0, y, W, MENU_ITEM_H, action)
        if self.top > 0:
            c.button(NAV_UP[0], NAV_UP[1], 'up', COLOR_ENABLED, self.page_up)
        c.button(NAV_BACK[0], NAV_BACK[1], 'back', COLOR_ENABLED, self.app.back)
        if self.top + self.ROWS - 1 < len(items):
            c.button(NAV_DOWN[0], NAV_DOWN[1], 'down', COLOR_ENABLED,
                     self.page_down)
    def page_up(self):
        self.top = max(0, self.top - (self.ROWS - 1))
    def page_down(self):
        self.top += self.ROWS - 1

class ConfirmScreen(Screen):
    def __init__(self, app, message, action):
        Screen.__init__(self, app)
        self.message, self.action = message, action
    def draw(self, c, st):
        c.text_center(0, 90, W, 60, self.message, COLOR_MENU_TEXT)
        c.button(BUTTON_CANCEL[0], BUTTON_CANCEL[1], 'cancel', COLOR_CANCEL,
                 self.app.back)
        c.button(BUTTON_CONFIRM[0], BUTTON_CONFIRM[1], 'confirm',
                 COLOR_RATE_100, self.yes)
    def yes(self):
        self.app.back()
        self.action()

######################################################################
# Application
######################################################################

DEFAULT_CONFIG = """
[screen]
welcome: Tronxy D01 Ready!
hotend_max: 260
bed_max: 110
# Marlin LCD_FEEDBACK tone on each button tap (M300 S<hz> P<ms>); 0 = off
touch_beep_hz: 5000
touch_beep_ms: 2

# Preheat presets (Marlin PREHEAT_1..3)
[preheat PLA]
hotend: 180
bed: 70
[preheat PETG]
hotend: 235
bed: 80

# Favorites list (Marlin CUSTOM_MENU_MAIN), in order. gcode may use \\n.
[favorites]
Preheat PLA: M140 S70\\nM104 S160
Preheat PETG: M140 S80\\nM104 S235
Auto Home: G28
Bed Mesh: G28\\nBED_MESH_CALIBRATE
Load Mesh: BED_MESH_PROFILE LOAD=default
Cooldown: TURN_OFF_HEATERS\\nM107
Disable Steppers: M84
"""

class App:
    def __init__(self, args):
        self.args = args
        self.assets = Assets()
        self.canvas = Canvas(self.assets)
        self.cfg = configparser.ConfigParser(delimiters=(':',),
                                             interpolation=None)
        self.cfg.optionxform = str
        self.cfg.read_string(DEFAULT_CONFIG)
        if args.config and os.path.exists(args.config):
            user = configparser.ConfigParser(delimiters=(':',),
                                             interpolation=None)
            user.optionxform = str
            user.read(args.config)
            for sec in user.sections():   # user sections replace defaults
                if self.cfg.has_section(sec):
                    self.cfg.remove_section(sec)
                self.cfg.add_section(sec)
                for k, v in user.items(sec):
                    self.cfg.set(sec, k, v)
        self.welcome = self.cfg.get('screen', 'welcome')
        self.touch_beep = (self.cfg.getint('screen', 'touch_beep_hz', fallback=0),
                           self.cfg.getint('screen', 'touch_beep_ms', fallback=0))
        self.presets = [(s.split(None, 1)[1],
                         self.cfg.getint(s, 'hotend'), self.cfg.getint(s, 'bed'))
                        for s in self.cfg.sections() if s.startswith('preheat ')]
        self.favorites = [(k, v.replace('\\n', '\n'))
                          for k, v in self.cfg.items('favorites')]
        self.state_path = args.state
        self.material = self._load_state().get('material', self.presets[0][0])
        self.stack = [StatusScreen(self)]
        self.stop_armed_at = 0.
        self.beep_mask_until = 0.
        self.locked_visible = False
        self.touches = queue.Queue()
        self.client = None
        self.released = False
        self.dirty = True
        self.last_key = None

    # --- persisted UI state (selected material) ---
    def _load_state(self):
        try:
            return json.load(open(self.state_path))
        except Exception:
            return {}
    def _save_state(self):
        try:
            json.dump({'material': self.material}, open(self.state_path, 'w'))
        except Exception:
            logging.exception("saving state")

    # --- navigation ---
    def push(self, screen):
        self.stack.append(screen)
    def back(self):
        if len(self.stack) > 1:
            self.stack.pop()

    # --- actions ---
    BEEP_MASK_TIME = 1.0
    def gcode(self, script, beep=False):
        # Any real gcode ends the beep mask so homing/macros lock at once
        if not beep:
            self.beep_mask_until = 0.
        # gcode/script blocks until done (G28 etc.), so run it off-thread
        def run():
            try:
                self.client.request("gcode/script", {'script': script},
                                    timeout=1800.)
            except Exception as e:
                logging.info("gcode %r: %s", script, e)
        threading.Thread(target=run, daemon=True).start()
    def beep_touch(self):
        # Idle-screen taps only: while gcode runs, touches never reach here
        # (locked screen), so no M300 dwell lands in a print.
        hz, ms = self.touch_beep
        if hz > 0 and ms > 0 and not getattr(getattr(self, "state", None), "busy", False):
            self.beep_mask_until = time.time() + self.BEEP_MASK_TIME
            self.gcode("M300 S%d P%d" % (hz, ms), beep=True)
    STOP_ARM_TIME = 3.
    def stop_armed(self):
        return time.time() - self.stop_armed_at < self.STOP_ARM_TIME
    def tap_stop(self, direct=False):
        # First tap arms, second (within 3 s) stops
        if not self.stop_armed():
            self.stop_armed_at = time.time()
            return
        self.stop_armed_at = 0.
        if self.state.printing:
            self.gcode("CANCEL_PRINT")
        elif direct:
            # From the socket reader thread: a blocking request there would
            # wait on its own reply, so send it from a new thread
            threading.Thread(target=self.emergency_stop, daemon=True).start()
        else:
            self.emergency_stop()
    def tap_pause(self):
        if self.state.printing:
            self.gcode("RESUME" if self.state.paused else "PAUSE")
    def emergency_stop(self):
        try:
            self.client.request("emergency_stop", {})
        except Exception as e:
            logging.info("emergency_stop: %s", e)
    def preset(self, name):
        for n, he, bed in self.presets:
            if n == name:
                return he, bed
        return self.presets[0][1:]
    def preheat_selected(self):
        he, bed = self.preset(self.material)
        self.gcode("M140 S%d\nM104 S%d" % (bed, he))   # D01: no fan
    def edit_heater(self, obj, label):
        st = self.state
        is_bed = obj == 'heater_bed'
        presets = [("Off", 0)] + [(n, bed if is_bed else he)
                                  for n, he, bed in self.presets]
        vmax = self.cfg.getint('screen', 'bed_max' if is_bed else 'hotend_max')
        heater = 'heater_bed' if is_bed else 'extruder'
        self.push(EditScreen(
            self, label, st.g(obj, 'target') or 0, 0, vmax,
            lambda v: self.gcode("SET_HEATER_TEMPERATURE HEATER=%s TARGET=%d"
                                 % (heater, v)),
            steps=(1, 5, 10), presets=presets, unit="°"))
    def edit_fan(self):
        self.push(EditScreen(
            self, "Fan speed", (self.state.g('fan', 'speed') or 0.) * 100,
            0, 100, lambda v: self.gcode("M106 S%d" % (round(v * 2.55),)),
            steps=(1, 10), presets=[("Off", 0), ("50%", 50), ("100%", 100)],
            unit="%"))
    def edit_feed(self):
        self.push(EditScreen(
            self, "Print speed",
            (self.state.g('gcode_move', 'speed_factor') or 1.) * 100, 10, 999,
            lambda v: self.gcode("M220 S%d" % (v,)), steps=(1, 10),
            presets=[("100%", 100)], unit="%"))
    def edit_flow(self):
        self.push(EditScreen(
            self, "Flow",
            (self.state.g('gcode_move', 'extrude_factor') or 1.) * 100, 10,
            999, lambda v: self.gcode("M221 S%d" % (v,)), steps=(1, 10),
            presets=[("100%", 100)], unit="%"))
    def confirm_stop(self):
        self.push(ConfirmScreen(self, "Stop print?",
                                lambda: self.gcode("CANCEL_PRINT")))
    def open_favorites(self):
        self.push(ListScreen(self, "Favorites", [
            (label, lambda g=g: (self.gcode(g), self.back()))
            for label, g in self.favorites]))
    def open_settings(self):
        def items(st):
            out = []
            if st.paused:
                out.append(("Resume print", lambda: (self.gcode("RESUME"),
                                                     self.back())))
            elif st.printing:
                out.append(("Pause print", lambda: (self.gcode("PAUSE"),
                                                    self.back())))
            if st.printing:
                out.append(("Stop print", self.confirm_stop))
            out.append(("Material: %s" % (self.material,),
                        self.cycle_material))
            out.append(("Klipper menu", self.release))
            out.append(("Disable steppers",
                        None if st.printing else lambda: self.gcode("M84")))
            out.append(("Firmware restart", lambda: self.push(ConfirmScreen(
                self, "Firmware restart?",
                lambda: self.gcode("FIRMWARE_RESTART")))))
            return out
        self.push(ListScreen(self, "Settings", items))
    def cycle_material(self):
        names = [p[0] for p in self.presets]
        i = names.index(self.material) if self.material in names else -1
        self.material = names[(i + 1) % len(names)]
        self._save_state()
    def release(self):
        # Hand the panel to Klipper's built-in menu until "resume"
        self.stack = self.stack[:1]
        self.released = True
        try:
            self.client.request("lerdge_tft/release", {})
        except Exception as e:
            logging.info("release: %s", e)

    # --- touch (called from the socket reader thread) ---
    def on_touch(self, params):
        # STOP on the busy screen is handled right here in the socket
        # reader thread: the draw loop may be sleeping in the link throttle
        # and must not delay an emergency stop.
        if self.locked_visible and params.get('action') == 'down':
            x, y = params.get('x', -1), params.get('y', -1)
            def inside(pos):
                return pos[0] <= x < pos[0] + 64 and pos[1] <= y < pos[1] + 64
            if inside(SDCARD_ICON):
                self.tap_stop(direct=True)
            elif inside(MENU_ICON):
                self.tap_pause()
            self.dirty = True
            return          # everything else is inert while gcode runs
        self.touches.put(params)
    def handle_touches(self):
        while True:
            try:
                p = self.touches.get_nowait()
            except queue.Empty:
                return
            act = p.get('action')
            if act == 'resume':
                self.released = False
                self.dirty = True
            elif act == 'down' and not self.released:
                action = self.canvas.hit(p.get('x', -1), p.get('y', -1))
                if action is not None:
                    self.beep_touch()
                    try:
                        action()
                    except Exception:
                        logging.exception("touch action")
                    self.dirty = True

    # --- frame ---
    def render(self, st):
        self.state = st
        c = self.canvas
        c.clear()
        self.locked_visible = st.busy
        if st.busy:
            self.stack = self.stack[:1]     # locked status screen only
        self.stack[-1].draw(c, st)
        return c.rgb565()

    def poll_state(self):
        res = self.client.request("objects/query", {'objects': QUERY})
        st = State(res.get('status', {}))
        st.beep_only = time.time() < getattr(self, 'beep_mask_until', 0.)
        return st

    def run_once(self):
        mirror = lm.Mirror(self.args)
        res = self.client.request("lerdge_tft/draw", {'rects': []})
        need_full, prev, last_send = True, None, 0.
        while True:
            self.handle_touches()
            if self.released:
                time.sleep(0.1)
                need_full = True
                continue
            st = self.poll_state()
            mirror.printing = mirror.printing or st.busy
            # Only re-render when something visible can have changed: the
            # rounded status, the 1 s blink, a touch, or a server full-redraw
            homed = st.g('toolhead', 'homed_axes') or ""
            blink = (int(time.time()) % 2
                     if not all(a in homed for a in "xyz") else 0)
            key = (self.display_key(st), blink, len(self.stack),
                   id(self.stack[-1]), st.busy, self.stop_armed())
            now = time.time()
            busy_wait = (mirror.printing and not self.dirty and not need_full
                         and now - last_send < BUSY_MIN_INTERVAL)
            if busy_wait or (key == self.last_key and not self.dirty
                             and not need_full):
                if now - last_send > lm.KEEPALIVE_TIME:
                    res = mirror.draw(self.client, [])
                    last_send = now
                    need_full = res.get('need_full', False)
                self.wait(mirror)
                continue
            self.last_key, self.dirty = key, False
            frame = self.render(st)
            if need_full:
                res = mirror.send_rects(self.client, frame, [(0, 0, W, H)],
                                        True)
                last_send = now
            else:
                rects = lm.dirty_rects(frame, prev)
                if rects:
                    res = mirror.send_rects(self.client, frame, rects, False)
                    last_send = now
                elif now - last_send > lm.KEEPALIVE_TIME:
                    res = mirror.draw(self.client, [])
                    last_send = now
            prev = frame
            need_full = res.get('need_full', False)
            self.wait(mirror)

    def wait(self, mirror):
        # Wait for the next poll, waking early on touch
        deadline = time.time() + (lm.PRINTING_POLL_TIME
                                  if mirror.printing else 0.25)
        while time.time() < deadline and self.touches.empty():
            time.sleep(0.02)

    @staticmethod
    def display_key(st):
        # Status rounded to what the screen shows (temps to 1 degree,
        # factors/progress to 0.1 %), so sensor noise doesn't force redraws
        def r(v):
            if isinstance(v, float):
                return round(v) if abs(v) > 2. else round(v, 3)
            if isinstance(v, list):       # positions: Z shows 0.01 mm
                return [round(x, 2) if isinstance(x, float) else x for x in v]
            return v
        return json.dumps({o: {k: r(v) for k, v in d.items()}
                           for o, d in st.s.items()}, sort_keys=True)

    def run(self):
        while True:
            try:
                self.client = lm.KlippyClient(self.args.socket, self.on_touch)
                self.client.connect()
                self.client.request("lerdge_tft/subscribe_touch", {
                    'response_template': {'method': 'lerdge_touch'}})
                logging.info("Connected to %s", self.args.socket)
                self.released = False
                self.run_once()
            except Exception as e:
                logging.info("Screen error: %s", e)
            finally:
                if self.client is not None:
                    self.client.close()
            time.sleep(3.)

def preview(args, path):
    app = App(args)
    st = State({
        'extruder': {'temperature': 204.6, 'target': 205.},
        'heater_bed': {'temperature': 60.2, 'target': 60.},
        'fan': {'speed': 1.},
        'gcode_move': {'gcode_position': [112.4, 98.0, 12.28, 0.],
                       'speed_factor': 1., 'extrude_factor': 0.95},
        'toolhead': {'homed_axes': 'xyz'},
        'print_stats': {'state': 'printing', 'print_duration': 3725.,
                        'filename': 'pzem_pzv3_frame.gcode'},
        'virtual_sdcard': {'progress': 0.42, 'is_active': True},
        'display_status': {'message': None, 'progress': 0.42},
        'webhooks': {'state': 'ready'}})
    idle = State(dict(st.s, print_stats={'state': 'standby'},
                      virtual_sdcard={'progress': 0.}))
    app.render(idle)
    app.canvas.img.save(path)
    act = app.canvas.hit(SDCARD_ICON[0] + 10, SDCARD_ICON[1] + 10)
    assert act.__name__ == 'preheat_selected', act
    assert app.canvas.hit(ITEM_X(3) + 40, ITEM_Y + 40) is not None  # home
    base, ext = os.path.splitext(path)
    app.canvas.img.save(base + "-idle" + ext)
    for name, screen in (("edit", lambda: app.edit_heater('extruder', "Hotend")),
                         ("settings", app.open_settings)):
        app.stack = app.stack[:1]
        screen()
        app.render(idle)
        app.canvas.img.save("%s-%s%s" % (base, name, ext))
    # Printing: status screen, locked to STOP + PAUSE (even from a submenu)
    app.open_settings()
    app.render(st)
    app.canvas.img.save(base + "-printing" + ext)
    assert len(app.stack) == 1
    assert app.canvas.hit(SDCARD_ICON[0] + 10, SDCARD_ICON[1] + 10).__name__ == 'tap_stop'
    assert app.canvas.hit(MENU_ICON[0] + 10, MENU_ICON[1] + 10).__name__ == 'tap_pause'
    for x, y in ((ITEM_X(0) + 40, 40), (ITEM_X(3) + 40, 40), (FEED[0] + 5, FEED[1] + 5)):
        assert app.canvas.hit(x, y) is None, (x, y)    # heaters/home/rates inert
    # Touch thread: STOP needs two taps, other taps are dropped
    calls = []
    app.gcode = calls.append
    app.on_touch({'action': 'down', 'x': ITEM_X(0) + 40, 'y': 40})
    assert app.touches.empty() and not calls
    app.on_touch({'action': 'down', 'x': SDCARD_ICON[0] + 10, 'y': SDCARD_ICON[1] + 10})
    assert not calls and app.stop_armed()
    app.render(st)
    app.canvas.img.save(base + "-armed" + ext)
    app.on_touch({'action': 'down', 'x': SDCARD_ICON[0] + 10, 'y': SDCARD_ICON[1] + 10})
    assert calls == ["CANCEL_PRINT"], calls
    app.on_touch({'action': 'down', 'x': MENU_ICON[0] + 10, 'y': MENU_ICON[1] + 10})
    assert calls[-1] == "PAUSE", calls
    # Touch beep: its M300 dwell blip must not pop a just-opened menu
    app.stack = app.stack[:1]
    app.render(idle)
    app.open_settings()
    blip = State(dict(idle.s, idle_timeout={'state': 'Printing'}))
    blip.beep_only = True
    app.render(blip)
    assert len(app.stack) == 2 and not app.locked_visible, app.stack
    app.stack = app.stack[:1]
    # Homing (busy, not printing): STOP is an emergency stop, PAUSE disabled
    homing = State(dict(idle.s, idle_timeout={'state': 'Printing'}))
    app.render(homing)
    app.canvas.img.save(base + "-homing" + ext)
    assert app.canvas.hit(MENU_ICON[0] + 10, MENU_ICON[1] + 10) is None
    estops = []
    app.emergency_stop = lambda: estops.append(1)
    app.tap_stop(); app.tap_stop()
    assert estops == [1]
    print("preview ok:", path)

def main():
    p = argparse.ArgumentParser(description="Marlin-style D01 TFT screen")
    p.add_argument("-s", "--socket",
                   default=DEFAULT_SOCKET)
    p.add_argument("-c", "--config",
                   default=DEFAULT_CONFIG_FILE)
    p.add_argument("--state", default=DEFAULT_STATE_FILE)
    p.add_argument("--print-share", type=float, default=BUSY_LINK_SHARE,
                   help="link share for display data while gcode runs")
    p.add_argument("--preview", metavar="PNG")
    p.add_argument("-v", "--verbose", action="store_true")
    args = p.parse_args()
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(message)s")
    if args.preview:
        preview(args, args.preview)
        return
    App(args).run()

if __name__ == '__main__':
    main()
