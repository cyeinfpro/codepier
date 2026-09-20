"""Redact exact credential values before streaming or persisting process output."""
from __future__ import annotations


class SecretOutput:
    def __init__(self, secret: str = ''):
        self.secret = secret
        self.pending = ''

    def feed(self, text: str, *, final: bool = False) -> str:
        if not self.secret:
            return text
        self.pending += text
        parts = []
        # Keep an incomplete suffix in memory so a credential split over reads
        # never leaks through a journal/output snapshot.
        while self.pending:
            index = self.pending.find(self.secret)
            if index >= 0:
                parts.extend((self.pending[:index], '<redacted>'))
                self.pending = self.pending[index + len(self.secret):]
                continue
            keep = 0 if final else min(len(self.secret) - 1, len(self.pending))
            while keep and not self.secret.startswith(self.pending[-keep:]):
                keep -= 1
            end = len(self.pending) - keep
            parts.append(self.pending[:end])
            self.pending = self.pending[end:]
            break
        return ''.join(parts)

    def redact(self, text: str) -> str:
        return text.replace(self.secret, '<redacted>') if self.secret else text
