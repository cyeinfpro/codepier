"""Headless terminal query replies; never depends on a browser being attached.

Rendering remains the original byte stream in xterm. This bounded parser tracks
cursor/margins/wrap for VT cursor/device/keyboard queries; it executes no OSC
clipboard, URL, or operating-system actions and advertises no image protocol.
"""
from __future__ import annotations
import codecs
import re
import unicodedata


class TerminalQueries:
    def __init__(self, rows=30, cols=120):
        self.rows, self.cols = rows, cols
        self.decoder = codecs.getincrementaldecoder('utf-8')('replace')
        self.state = 'text'
        self.buffer = ''
        self.overflow = False
        self.responses = bytearray()
        self.reset()

    def reset(self):
        self.x = self.y = 0
        self.top, self.bottom = 0, self.rows - 1
        self.wrap = False
        self.modes = {7, 25}
        self.saved = (0, 0, False)
        self.alternate_saved = None
        self.tabs = set(range(8, self.cols, 8))
        self.last_width = 1
        self.colors = {'10': 'dcdc/e5e5/f2f2', '11': '0c0c/1111/1b1b'}

    def resize(self, rows, cols):
        self.rows, self.cols = rows, cols
        self.x, self.y = min(self.x, cols - 1), min(self.y, rows - 1)
        self.top, self.bottom = 0, rows - 1
        self.tabs = set(range(8, cols, 8))
        self.wrap = False

    def reply(self, text):
        # Bound response memory even when a program prints a flood of queries.
        if len(self.responses) + len(text) <= 32768:
            self.responses.extend(text.encode('ascii'))

    def feed(self, data):
        self.responses.clear()
        for char in self.decoder.decode(data):
            self.consume(char)
        return bytes(self.responses)

    def linefeed(self):
        if self.top <= self.y <= self.bottom:
            self.y = min(self.y + 1, self.bottom)
        else:
            self.y = min(self.y + 1, self.rows - 1)
        self.wrap = False

    def draw(self, width):
        if not width:
            return
        self.last_width = width
        if self.wrap and 7 in self.modes:
            self.linefeed()
            self.x = 0
        if width == 2 and self.x == self.cols - 1 and 7 in self.modes:
            self.linefeed()
            self.x = 0
        if self.x + width >= self.cols:
            self.x = self.cols - 1
            self.wrap = 7 in self.modes
        else:
            self.x += width
            self.wrap = False

    def consume(self, char):
        if self.state in ('osc', 'dcs'):
            if char == '\x1b':
                self.state += '-esc'
            elif char == '\x07' and self.state == 'osc':
                if not self.overflow:
                    self.osc(self.buffer)
                self.state = 'text'
            elif self.state == 'osc' and len(self.buffer) < 8192:
                self.buffer += char
            else:
                self.overflow = True
            return
        if self.state in ('osc-esc', 'dcs-esc'):
            previous = self.state[:-4]
            if char == '\\':
                if previous == 'osc' and not self.overflow:
                    self.osc(self.buffer)
                self.state = 'text'
            else:
                self.state = previous
                if previous == 'osc' and len(self.buffer) < 8190:
                    self.buffer += '\x1b' + char
            return
        if self.state == 'csi':
            if '@' <= char <= '~':
                if not self.overflow:
                    self.csi(self.buffer, char)
                self.state = 'text'
            elif char == '\x1b':
                self.state = 'esc'
            elif len(self.buffer) < 128:
                self.buffer += char
            else:
                self.overflow = True
            return
        if self.state == 'charset':
            self.state = 'text'
            return
        if self.state == 'esc':
            self.state = 'text'
            if char == '[':
                self.state, self.buffer, self.overflow = 'csi', '', False
            elif char in ']PX^_':
                self.state, self.buffer, self.overflow = ('osc' if char == ']' else 'dcs'), '', False
            elif char in '()%*+-.#':
                self.state = 'charset'
            elif char == '7':
                self.saved = (self.x, self.y, self.wrap)
            elif char == '8':
                self.x, self.y, self.wrap = self.saved
            elif char == 'D':
                self.linefeed()
            elif char == 'E':
                self.linefeed()
                self.x = 0
            elif char == 'M':
                self.y = max(self.top if self.top <= self.y <= self.bottom else 0, self.y - 1)
                self.wrap = False
            elif char == 'H':
                self.tabs.add(self.x)
            elif char == 'c':
                self.reset()
            return
        if char == '\x1b':
            self.state = 'esc'
        elif char == '\r':
            self.x, self.wrap = 0, False
        elif char in '\n\v\f':
            self.linefeed()
        elif char == '\b':
            self.x, self.wrap = max(0, self.x - 1), False
        elif char == '\t':
            self.x = next((x for x in sorted(self.tabs) if x > self.x), self.cols - 1)
            self.wrap = False
        elif ord(char) >= 32 and char != '\x7f':
            code = ord(char)
            if unicodedata.combining(char) or unicodedata.category(char) in ('Cf', 'Mn', 'Me') or 0x1F3FB <= code <= 0x1F3FF:
                width = 0
            else:
                width = 2 if unicodedata.east_asian_width(char) in ('W', 'F') else 1
            self.draw(width)

    def csi(self, raw, final):
        match = re.fullmatch(r'([?<=>]?)([\d;:]*)([ -/]*)', raw)
        if not match:
            return
        prefix, body, intermediate = match.groups()
        values = [int(v.split(':')[0] or '0') for v in body.split(';')]
        first = values[0] if values else 0
        amount = max(1, first)
        if final == 'n':
            if first == 5:
                self.reply('\x1b[0n')
            elif first == 6:
                y = self.y - self.top if 6 in self.modes else self.y
                self.reply('\x1b[' + ('?' if prefix == '?' else '') + f'{y+1};{self.x+1}R')
            return
        if final == 'c' and first == 0:
            self.reply('\x1b[>0;0;0c' if prefix == '>' else '\x1b[?1;2c')
            return
        if final == 'u' and prefix == '?':
            # xterm.js 5 does not implement Kitty progressive keyboard modes.
            # A ?0u reply means SUPPORTED with zero currently-active flags; it
            # must not be sent as a false 'unsupported' response. Primary DA
            # below lets native clients detect the legacy keyboard correctly.
            return
        if final == 't' and first == 18:
            self.reply(f'\x1b[8;{self.rows};{self.cols}t')
            return
        if final == 'p' and prefix == '?' and intermediate == '$':
            result = (1 if first in self.modes else 2) if first in {6, 7, 25, 2004} else 0
            self.reply(f'\x1b[?{first};{result}$y')
            return
        if final in ('h', 'l') and prefix == '?':
            for mode in values:
                if mode in (47, 1047, 1049):
                    if final == 'h' and self.alternate_saved is None:
                        self.alternate_saved = (self.x, self.y, self.wrap)
                        self.x = self.y = 0
                        self.wrap = False
                    elif final == 'l' and self.alternate_saved is not None:
                        self.x, self.y, self.wrap = self.alternate_saved
                        self.alternate_saved = None
                if final == 'h':
                    self.modes.add(mode)
                else:
                    self.modes.discard(mode)
                if mode == 6:
                    self.x, self.y = 0, self.top if final == 'h' else 0
            return
        if prefix:
            return
        low = self.top if 6 in self.modes else 0
        high = self.bottom if 6 in self.modes else self.rows - 1
        if final in ('H', 'f'):
            self.y = min(high, low + max(1, first) - 1)
            self.x = min(self.cols - 1, max(1, values[1] if len(values) > 1 else 1) - 1)
        elif final in ('A', 'F'):
            self.y = max(low, self.y - amount)
            if final == 'F': self.x = 0
        elif final in ('B', 'e', 'E'):
            self.y = min(high, self.y + amount)
            if final == 'E': self.x = 0
        elif final in ('C', 'a'):
            self.x = min(self.cols - 1, self.x + amount)
        elif final == 'D':
            self.x = max(0, self.x - amount)
        elif final in ('G', '`'):
            self.x = min(self.cols - 1, amount - 1)
        elif final == 'd':
            self.y = min(high, low + amount - 1)
        elif final == 'r':
            top = max(1, first) - 1
            bottom = (values[1] if len(values) > 1 and values[1] else self.rows) - 1
            if 0 <= top < bottom < self.rows:
                self.top, self.bottom = top, bottom
                self.x, self.y = 0, self.top if 6 in self.modes else 0
        elif final == 's':
            self.saved = (self.x, self.y, self.wrap)
        elif final == 'u':
            self.x, self.y, self.wrap = self.saved
        elif final == 'g':
            self.tabs.clear() if first == 3 else self.tabs.discard(self.x)
        elif final == 'b':
            for _ in range(min(amount, self.rows * self.cols)):
                self.draw(self.last_width)
            return
        else:
            return
        self.wrap = False

    def osc(self, raw):
        code, _, value = raw.partition(';')
        if code in self.colors and value == '?':
            self.reply(f'\x1b]{code};rgb:{self.colors[code]}\x1b\\')
        elif code in self.colors and re.fullmatch(r'rgb:[0-9a-fA-F]{2,4}/[0-9a-fA-F]{2,4}/[0-9a-fA-F]{2,4}', value):
            self.colors[code] = value[4:]
