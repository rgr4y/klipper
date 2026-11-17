# Expose an MCU UART (src/stm32/uart_bridge.c) as a host pseudo-terminal
#
# Built for the Ender-3 V2 DWIN panel on an SKR Mini E3 V3: the panel's
# UART is on EXP1 (USART1), the host only talks to the board, so panel
# bytes ride the Klipper link. Optional input_pins exposes raw button /
# encoder pin levels on a second pty for a host UI (e.g. Petrel) to decode.
# The UI can write "B<freq> <duty>\n" back on that pty to sound the panel
# beeper through a [pwm_cycle_time] pin (option beeper).
#
# Copyright (C) 2026  Rob Vella
#
# This file may be distributed under the terms of the GNU GPLv3 license.
import logging, os, tty, errno
import mcu

CHUNK = 48              # data bytes per uart_bridge_write (fits a msg block)
MCU_TX_BUFFER = 1024    # src/stm32/uart_bridge.c TX_SIZE
IDLE_WINDOW = 512       # bytes allowed in flight (estimated) while idle
BUSY_WINDOW = 96        # ... and while printing: small bursts only
INPUT_HEARTBEAT = 2.    # resend current pin state so a fresh reader syncs
MAX_PENDING = 65536     # drop host writes beyond this (MCU not connected)

class UartBridge:
    def __init__(self, config):
        self.printer = printer = config.get_printer()
        self.reactor = printer.get_reactor()
        self.name = config.get_name().split()[-1]
        self.baud = config.getint('baud', 115200, minval=2400)
        # Bytes/s ceiling while a print runs (the panel shares the host
        # link with step commands; see D01 "Timer too close" history).
        self.busy_rate = config.getfloat('busy_rate', 1000., minval=100.)
        self.pty_path = config.get('pty', '/tmp/%s_uart' % (self.name,))
        self.mcu = mcu.get_printer_mcu(printer, config.get('mcu', 'mcu'))
        self.oid = self.mcu.create_oid()
        self.mcu.add_config_cmd("config_uart_bridge oid=%d baud=%d"
                                % (self.oid, self.baud))
        self.mcu.register_config_callback(self._build_config)
        self.write_cmd = None
        self.idle_timeout = None
        # Host side tx pacing
        self.pending = bytearray()
        self.in_flight = 0.
        self.last_drain = 0.
        self.flush_timer = self.reactor.register_timer(self._flush)
        self.overflows = 0
        # Ptys
        self.master_fd, self.slave_fd, self.fd_handle = self._open_pty(
            self.pty_path)
        self.input_fds = None
        pins = config.getlist('input_pins', None)
        if pins:
            buttons = printer.load_object(config, 'buttons')
            buttons.register_buttons(pins, self._input_changed)
            path = config.get('input_pty', '/tmp/%s_input' % (self.name,))
            self.input_fds = self._open_pty(path, readable=False)
            self.input_buf = b""
            self.beeper_name = config.get('beeper', None)
            # pwm_cycle_time spaces pin changes >= min_schedule_time (100ms)
            # apart, so beeps sent faster than that schedule ever later and
            # keep playing after input stops. Drop a new tone once earlier
            # changes are scheduled this far ahead of now.
            self.beeper_max_lag = config.getfloat('beeper_max_lag', .3,
                                                  minval=0.)
            self.beeper = None
            if self.beeper_name is not None:
                self.reactor.register_fd(self.input_fds[0], self._beeper_readable)
            self.input_state = None
            self.reactor.register_timer(self._input_heartbeat,
                                        self.reactor.NOW)
        printer.register_event_handler("klippy:ready", self._handle_ready)
        printer.register_event_handler("klippy:disconnect",
                                       self._handle_disconnect)
    def _open_pty(self, path, readable=True):
        master_fd, slave_fd = os.openpty()
        tty.setraw(slave_fd)
        os.set_blocking(master_fd, False)
        try:
            os.unlink(path)
        except OSError:
            pass
        os.symlink(os.ttyname(slave_fd), path)
        handle = None
        if readable:
            handle = self.reactor.register_fd(master_fd, self._pty_readable)
        # slave_fd stays open so the master never sees EIO between readers
        return master_fd, slave_fd, handle
    def _build_config(self):
        cq = self.mcu.alloc_command_queue()
        self.write_cmd = self.mcu.lookup_command(
            "uart_bridge_write oid=%c data=%*s", cq=cq)
        self.mcu.register_serial_response(
            self._handle_rx, "uart_bridge_rx oid=%c overflow=%c data=%*s",
            self.oid)
    def _handle_ready(self):
        self.idle_timeout = self.printer.lookup_object('idle_timeout', None)
        if self.input_fds is not None and self.beeper_name is not None:
            self.beeper = self.printer.lookup_object(
                'pwm_cycle_time ' + self.beeper_name)
        self.last_drain = self.reactor.monotonic()
        if self.pending:
            self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
    def _handle_disconnect(self):
        if self.fd_handle is not None:
            self.reactor.unregister_fd(self.fd_handle)
            self.fd_handle = None
        fds = [self.master_fd, self.slave_fd]
        if self.input_fds is not None:
            fds += self.input_fds[:2]
        for fd in fds:
            try:
                os.close(fd)
            except OSError:
                pass
    # Panel -> host
    def _handle_rx(self, params):
        if params['overflow'] and self.overflows < 20:
            self.overflows += 1
            logging.warning("uart_bridge %s: overflow flags=%d",
                            self.name, params['overflow'])
        data = params['data']
        if data:
            try:
                os.write(self.master_fd, data)
            except OSError:
                pass  # nobody reading and pty buffer full: drop
    # Host -> panel
    def _pty_readable(self, eventtime):
        try:
            data = os.read(self.master_fd, 4096)
        except OSError as e:
            if e.errno in (errno.EAGAIN, errno.EIO):
                return
            raise
        if len(self.pending) + len(data) > MAX_PENDING:
            logging.warning("uart_bridge %s: dropping %d pending bytes",
                            self.name, len(self.pending))
            del self.pending[:]
        self.pending.extend(data)
        self.reactor.update_timer(self.flush_timer, self.reactor.NOW)
    def _is_busy(self, eventtime):
        if self.idle_timeout is None:
            return False
        return self.idle_timeout.get_status(eventtime)['state'] == "Printing"
    def _flush(self, eventtime):
        if self.write_cmd is None or not self.pending:
            return self.reactor.NEVER
        busy = self._is_busy(eventtime)
        # Estimated bytes still queued on the MCU. Drain is credited at 90%
        # of the panel baud (or busy_rate), so the estimate runs high and the
        # MCU buffer (1024) keeps ~500 bytes of slack for link jitter.
        # ponytail: open-loop pacing; add MCU tx_free reports if overflows
        # ever show up in klippy.log.
        rate = self.baud / 10. * .9
        if busy:
            rate = min(rate, self.busy_rate)
        self.in_flight = max(0., self.in_flight
                             - (eventtime - self.last_drain) * rate)
        self.last_drain = eventtime
        window = BUSY_WINDOW if busy else IDLE_WINDOW
        while self.pending and self.in_flight + 1 <= window:
            n = min(CHUNK, len(self.pending), int(window - self.in_flight))
            chunk = bytes(self.pending[:n])
            del self.pending[:n]
            self.write_cmd.send([self.oid, chunk])
            self.in_flight += n
        if not self.pending:
            return self.reactor.NEVER
        return eventtime + max(.005, (self.in_flight + CHUNK - window) / rate)
    # Encoder / button pins -> input pty, one ASCII digit per state change
    # (bit0 = first pin; 1 = pin high)
    def _input_changed(self, eventtime, state):
        self.input_state = state
        self._write_input(state)
    def _input_heartbeat(self, eventtime):
        if self.input_state is not None:
            self._write_input(self.input_state)
        return eventtime + INPUT_HEARTBEAT
    def _write_input(self, state):
        try:
            os.write(self.input_fds[0], b"%d" % (state,))
        except OSError:
            pass
    # Beeper commands from the UI on the input pty: "B<freq> <duty>\n"
    def _beeper_readable(self, eventtime):
        try:
            data = os.read(self.input_fds[0], 256)
        except OSError:
            return
        lines = (self.input_buf + data).split(b"\n")
        self.input_buf = lines.pop()[-64:]
        for line in lines:
            tone = parse_beeper(line)
            if tone is not None and self.beeper is not None:
                self._beep(eventtime, *tone)
    def _beep(self, eventtime, freq, duty):
        mcu = self.beeper.mcu_pin.get_mcu()
        print_time = mcu.estimated_print_time(
            eventtime + mcu.min_schedule_time())
        if duty and (self.beeper.last_print_time - print_time
                     > self.beeper_max_lag):
            return  # stale tone; "off" always passes so it never sticks on
        cycle = 1. / freq if duty else self.beeper.default_cycle_time
        self.beeper._set_pin(print_time, duty, cycle)
    def get_status(self, eventtime):
        return {'pending': len(self.pending), 'overflows': self.overflows}

def parse_beeper(line):
    """b"B2000 0.25" -> (2000., 0.25); malformed or out of range -> None."""
    try:
        freq, duty = line.strip()[1:].split()
        freq, duty = float(freq), float(duty)
    except ValueError:
        return None
    if not line.startswith(b"B") or not 0. <= duty <= 1.:
        return None
    if duty == 0.:
        return (1., 0.)
    if not 20. <= freq <= 10000.:
        return None
    return (freq, duty)

def load_config_prefix(config):
    return UartBridge(config)
