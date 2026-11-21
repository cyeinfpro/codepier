"""Redact exact credential values before streaming or persisting process output."""

from __future__ import annotations


class SecretOutputs:
    """Aho-Corasick exact matches, including overlaps spanning read boundaries."""

    def __init__(self, secrets):
        from collections import deque

        self.edges = [{}]
        self.fallback = [0]
        self.depth = [0]
        self.match = [0]
        for value in secrets:
            if not isinstance(value, str) or not value:
                continue
            node = 0
            for char in value:
                if char not in self.edges[node]:
                    self.edges[node][char] = len(self.edges)
                    self.edges.append({})
                    self.fallback.append(0)
                    self.depth.append(self.depth[node] + 1)
                    self.match.append(0)
                node = self.edges[node][char]
            self.match[node] = len(value)
        queue = deque(self.edges[0].values())
        while queue:
            node = queue.popleft()
            for char, child in self.edges[node].items():
                suffix = self.advance(self.fallback[node], char)
                self.fallback[child] = suffix
                self.match[child] = max(self.match[child], self.match[suffix])
                queue.append(child)
        self._reset()

    def _reset(self):
        from collections import deque

        self.node = self.position = self.sent = 0
        self.pending = deque()
        self.intervals = deque()

    def advance(self, node, char):
        while node and char not in self.edges[node]:
            node = self.fallback[node]
        return self.edges[node].get(char, 0)

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self.edges[0]:
            return text
        parts = []
        for char in text:
            self.position += 1
            self.pending.append(char)
            self.node = self.advance(self.node, char)
            length = self.match[self.node]
            if length:
                start = self.position - length
                while self.intervals and self.intervals[-1][1] > start:
                    start = min(start, self.intervals.pop()[0])
                self.intervals.append((start, self.position))
            prefix = self.node if self.edges[self.node] else self.fallback[self.node]
            self._drain(self.position - self.depth[prefix], parts)
        if final:
            self._drain(self.position, parts)
            self._reset()
        return "".join(parts)

    def _drain(self, end, parts):
        while self.sent < end:
            char = self.pending.popleft()
            if self.intervals and self.intervals[0][0] <= self.sent:
                first, last = self.intervals[0]
                if first == self.sent:
                    parts.append("<redacted>")
                if self.sent + 1 == last:
                    self.intervals.popleft()
            else:
                parts.append(char)
            self.sent += 1

    def redact(self, text: str) -> str:
        from copy import copy

        one = copy(self)
        one._reset()
        return one.feed(text, final=True)


class ProcessOutput:
    """Filter exact values first, then credentials and multi-line private keys."""

    LINE_LIMIT = 32 * 1024
    KEEP = 512

    def __init__(self, values):
        self.exact = SecretOutputs(values)
        self.line = ""
        self.in_pem = False
        self.skip_line = False

    def redact(self, text):
        from shared.audit_redaction import redact_text

        return redact_text(self.exact.redact(text))

    def _render(self, value):
        from shared.audit_redaction import PEM_BEGIN, PEM_END, REDACTED, redact_text

        parts = []
        while value:
            if self.in_pem:
                end = PEM_END.search(value)
                if end is None:
                    break
                value = value[end.end() :]
                self.in_pem = False
            else:
                begin = PEM_BEGIN.search(value)
                if begin is None:
                    parts.append(redact_text(value))
                    break
                parts.extend((redact_text(value[: begin.start()]), REDACTED))
                value = value[begin.end() :]
                self.in_pem = True
        return "".join(parts)

    def feed(self, text, *, final=False):
        from shared.audit_redaction import PEM_BEGIN, REDACTED, sensitive_key

        result = []
        text = self.exact.feed(text, final=final)
        for piece in text.splitlines(keepends=True):
            self.line += piece
            # Process real line separators only; vertical controls in arbitrary
            # command output must not split a credential into independent lines.
            complete = self.line.endswith(("\n", "\r"))
            if not complete and len(self.line) <= self.LINE_LIMIT:
                continue
            if self.skip_line:
                if PEM_BEGIN.search(self.line):
                    self.in_pem = True
                if complete:
                    self.line = ""
                    self.skip_line = False
                else:
                    self.line = self.line[-self.KEEP :]
                continue
            if complete:
                result.append(self._render(self.line))
                self.line = ""
            elif self.in_pem:
                # Retain enough of a split END delimiter to resume ordinary output.
                tail = self.line[-self.KEEP :]
                result.append(self._render(self.line[: -self.KEEP]))
                self.line = tail
            elif (
                PEM_BEGIN.search(self.line)
                or sensitive_key(self.line)
                or "://" in self.line
                or "bearer" in self.line.lower()
                or "basic" in self.line.lower()
            ):
                self.in_pem = bool(PEM_BEGIN.search(self.line))
                self.skip_line = True
                result.append(REDACTED)
                self.line = self.line[-self.KEEP :]
            else:
                result.append(self._render(self.line[: -self.KEEP]))
                self.line = self.line[-self.KEEP :]
        if final:
            if not self.skip_line:
                result.append(self._render(self.line))
            self.line = ""
            self.skip_line = self.in_pem = False
        return "".join(result)


def credential_output_values(environment):
    """Redact injected credential values without reading credential stores."""
    from shared.audit_redaction import sensitive_key

    from urllib.parse import unquote, urlsplit

    values = []
    for name, value in environment.items():
        if not isinstance(value, str) or not value:
            continue
        # POSIX shells use PWD and OLDPWD for directory metadata.
        if name in {"PWD", "OLDPWD"} and value.startswith("/"):
            continue
        if name == "SSHPASS" or sensitive_key(name):
            values.append(value)
        if "://" in value:
            try:
                password = urlsplit(value).password
            except ValueError:
                continue
            if password:
                values.extend((value, password, unquote(password)))
    return values
