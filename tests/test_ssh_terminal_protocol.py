"""Wire-compatibility and hostile-input tests; no network or project fixtures."""

from __future__ import annotations

import json
import struct
import unittest
from dataclasses import replace
from uuid import UUID

from shared.ssh_terminal_config import DEFAULT_CONFIG, TerminalConfig
from shared.ssh_terminal_protocol import (
    HEADER_SIZE,
    UINT64_MAX,
    FrameKind,
    TerminalFrame,
    TerminalProtocolError,
    canonical_uuid,
    decode_frame,
    encode_control,
    encode_frame,
    parse_control,
    parse_counter,
    validate_binding,
    validate_control,
)

SID = "00112233445566778899aabbccddeeff"
GEN = "102132435465768798a9bacbdcedfe0f"
ATTACHMENT = "ffeeddccbbaa99887766554433221100"


class ProtocolTests(unittest.TestCase):
    def assertCode(self, code, function, *args, **kwargs):
        with self.assertRaises(TerminalProtocolError) as caught:
            function(*args, **kwargs)
        self.assertEqual(code, caught.exception.code)

    def frame(self, **changes):
        base = TerminalFrame(FrameKind.OUTPUT, SID, GEN, 7, 2, 0, b"hello\r\n")
        return replace(base, **changes)

    def test_exact_browser_wire_layout_and_large_counter(self):
        frame = self.frame(sequence=2**53 + 123, payload=b"\x1b[31m\xe4\xb8\xad\x00")
        expected = struct.pack(
            "!BB16s16sQQQI", 1, 2, UUID(SID).bytes, UUID(GEN).bytes,
            7, 2, 2**53 + 123, len(frame.payload),
        ) + frame.payload
        self.assertEqual(62, HEADER_SIZE)
        self.assertEqual(expected, encode_frame(frame))
        self.assertEqual(frame, decode_frame(expected))

    def test_input_sequence_is_not_byte_offset(self):
        frame = self.frame(kind=FrameKind.INPUT, sequence=1, payload=b"whoami\r")
        decoded = decode_frame(encode_frame(frame))
        self.assertEqual(1, decoded.input_seq)
        self.assertEqual(b"whoami\r", decoded.payload)

    def test_uuid_normalization_and_fencing(self):
        frame = self.frame(session_id=str(UUID(SID)), runtime_generation=str(UUID(GEN)))
        self.assertEqual(SID, frame.session_id)
        validate_binding(frame, session_id=SID, runtime_generation=GEN, stream_epoch=7, writer_epoch=2)
        for kwargs, code in [
            ({"session_id": ATTACHMENT}, "SESSION_MISMATCH"),
            ({"runtime_generation": ATTACHMENT}, "STALE_GENERATION"),
            ({"stream_epoch": 8}, "STALE_STREAM"),
            ({"writer_epoch": 3}, "STALE_WRITER"),
        ]:
            expected = {"session_id": SID, "runtime_generation": GEN, "stream_epoch": 7, "writer_epoch": 2}
            expected.update(kwargs)
            with self.subTest(code=code):
                self.assertCode(code, validate_binding, frame, **expected)

    def test_uuid_rejects_aliases_and_non_strings(self):
        for value in [None, 1, "", "{" + str(UUID(SID)) + "}", "urn:uuid:" + str(UUID(SID)), SID + " "]:
            with self.subTest(value=value):
                self.assertCode("INVALID_BINDING", canonical_uuid, value)

    def test_binary_wrong_version_kind_and_lengths(self):
        wire = encode_frame(self.frame())
        for raw, code in [
            (wire[:61], "TRUNCATED_FRAME"),
            (b"\x02" + wire[1:], "PROTOCOL_MISMATCH"),
            (wire[:1] + b"\x03" + wire[2:], "INVALID_FRAME_KIND"),
            (wire[:-1], "FRAME_LENGTH_MISMATCH"),
            (wire + b"x", "FRAME_LENGTH_MISMATCH"),
            (wire[:58] + struct.pack("!I", 0) + wire[62:], "FRAME_TOO_LARGE"),
            (wire[:58] + struct.pack("!I", 2**32 - 1) + wire[62:], "FRAME_TOO_LARGE"),
        ]:
            with self.subTest(code=code):
                self.assertCode(code, decode_frame, raw)

    def test_binary_limits_and_no_zero_length_data(self):
        payload = b"x" * DEFAULT_CONFIG.frame_bytes
        self.assertEqual(payload, decode_frame(encode_frame(self.frame(payload=payload))).payload)
        self.assertCode("FRAME_TOO_LARGE", encode_frame, self.frame(payload=payload + b"x"))
        self.assertCode("INVALID_PAYLOAD", self.frame, payload=b"")
        self.assertCode("INVALID_PAYLOAD", self.frame, payload="password")
        self.assertCode("INVALID_COUNTER", self.frame, kind=FrameKind.INPUT, sequence=0)
        self.assertCode("INVALID_COUNTER", self.frame, sequence=UINT64_MAX)

    def test_binary_does_not_accept_boolean_or_float_counters(self):
        for name, value in [("stream_epoch", True), ("writer_epoch", -1), ("sequence", 1.0), ("kind", True), ("kind", 1.0)]:
            with self.subTest(name=name, value=value), self.assertRaises(TerminalProtocolError):
                self.frame(**{name: value})

    def test_partial_utf8_and_escape_bytes_are_not_decoded(self):
        for data in [b"\xe4", b"\xb8\xad", b"\x1b[", b"31m", b"\xff\x00"]:
            with self.subTest(data=data):
                self.assertEqual(data, decode_frame(encode_frame(self.frame(payload=data))).payload)

    def test_memoryview_byte_buffers_and_noncontiguous_rejection(self):
        encoded = encode_frame(self.frame())
        self.assertEqual(self.frame(), decode_frame(memoryview(encoded)))
        self.assertCode("INVALID_FRAME", decode_frame, memoryview(encoded)[::2])

    def test_json_counters_never_accept_floats_or_js_numbers(self):
        self.assertEqual(UINT64_MAX, parse_counter(str(UINT64_MAX)))
        for counter in [0, 1, True, "01", "-1", "+1", "1e3", " 1", "1.0", str(UINT64_MAX + 1), "9" * 1000]:
            with self.subTest(counter=counter):
                self.assertCode("INVALID_COUNTER", parse_counter, counter)

    def test_initial_authentication_omits_unknown_binding(self):
        message = {"type": "authenticate", "ticket": "t" * 32, "renderer_id": SID, "protocol_version": 1}
        self.assertEqual(message, parse_control(json.dumps(message), allowed_types={"authenticate"}))
        self.assertCode("INVALID_BINDING", parse_control, json.dumps({**message, "runtime_generation": None}))
        self.assertCode("UNEXPECTED_CONTROL", parse_control, json.dumps(message), allowed_types={"ping"})
        self.assertCode("INVALID_CONTROL", parse_control, json.dumps({**message, "protocol_version": 2}))

    def test_duplicate_keys_unknown_fields_and_types_rejected(self):
        cases = [
            '{"type":"ping","type":"close","nonce":"n"}',
            '{"type":"ping","nonce":"n","nonce":"other"}',
            '{"type":"ping","nonce":"n","command":"rm -rf /"}',
            '{"type":"ping"}',
            '{"type":null}', '[]', 'null', '"ping"',
        ]
        for raw in cases:
            with self.subTest(raw=raw), self.assertRaises(TerminalProtocolError):
                parse_control(raw)

    def test_hostile_json_size_encoding_depth_and_nonfinite_numbers(self):
        for raw in [
            b'\xff', '{"type":"ping","nonce":"n","timestamp":NaN}',
            '{"type":"ping","nonce":"n","timestamp":Infinity}',
            '{"type":"ping","nonce":"n","timestamp":' + '9' * 1000 + '}',
            '{"type":"ping","nonce":"\\ud800"}',
            '[' * 2000 + '0' + ']' * 2000,
            '{"type":"ping","nonce":"' + "中" * 3000 + '"}',
        ]:
            with self.subTest(length=len(raw)), self.assertRaises(TerminalProtocolError):
                parse_control(raw)

    def test_output_ack_and_resize_have_attachment_binding(self):
        ack = {"type": "output_ack", "attachment_id": ATTACHMENT, "render_epoch": "3", "stream_epoch": "7", "rendered_offset": str(2**53 + 12)}
        self.assertEqual(ack, parse_control(json.dumps(ack)))
        resize = {"type": "resize", "attachment_id": ATTACHMENT, "runtime_generation": GEN,
                  "stream_epoch": "7", "writer_epoch": "2", "resize_seq": "9", "cols": 120, "rows": 40}
        self.assertEqual(resize, parse_control(json.dumps(resize)))
        for cols in [0, True, 1001, "120", 120.0]:
            with self.subTest(cols=cols):
                self.assertCode("INVALID_CONTROL", parse_control, json.dumps({**resize, "cols": cols}))

    def test_server_attached_and_replay_barrier_round_trip(self):
        attached = {"type": "attached", "session_id": SID, "attachment_id": ATTACHMENT,
                    "runtime_generation": GEN, "render_epoch": "1", "stream_epoch": "7", "writer_epoch": "2",
                    "mode": "tmux", "resume_strategy": "redraw", "state": "ready", "start_offset": "0",
                    "next_input_seq": "1", "cols": 120, "rows": 40}
        self.assertEqual(attached, parse_control(encode_control(attached), direction="server"))
        barrier = {"type": "replay_complete", "attachment_id": ATTACHMENT, "render_epoch": "1", "stream_epoch": "7", "final_offset": "123"}
        self.assertEqual(barrier, parse_control(encode_control(barrier), direction="server"))
        self.assertCode("UNKNOWN_CONTROL", parse_control, encode_control(attached))

    def test_errors_never_echo_credentials_or_untrusted_values(self):
        secret = "private-ticket-must-not-appear"
        with self.assertRaises(TerminalProtocolError) as caught:
            parse_control(json.dumps({"type": "authenticate", "ticket": secret, "renderer_id": "bad"}))
        self.assertNotIn(secret, str(caught.exception))
        self.assertNotIn("bad", str(caught.exception))

    def test_keyboard_interactive_challenge_and_secret_answer(self):
        challenge = {"type": "auth_challenge", "challenge_id": ATTACHMENT,
                     "prompts": [{"text": "One-time code:\n", "echo": False}],
                     "name": "Verification", "instruction": "Use your current code", "deadline_seconds": 15}
        self.assertEqual(challenge, parse_control(encode_control(challenge), direction="server"))
        answer = {"type": "auth_answer", "challenge_id": ATTACHMENT, "responses": ["123456", ""]}
        self.assertEqual(answer, parse_control(json.dumps(answer)))
        for changed in [
            {**challenge, "prompts": []},
            {**challenge, "prompts": [{"text": "x", "echo": "false"}]},
            {**challenge, "prompts": [{"text": "x", "echo": False, "html": "injected"}]},
            {**challenge, "deadline_seconds": 121},
        ]:
            with self.assertRaises(TerminalProtocolError):
                validate_control(changed, direction="server")
        for responses in [[], ["x"] * 9, ["secret" * 1000], [None]]:
            with self.subTest(responses_type=type(responses[0]).__name__ if responses else "empty"), self.assertRaises(TerminalProtocolError):
                parse_control(json.dumps({**answer, "responses": responses}))

    def test_authentication_arrays_still_obey_total_byte_limit(self):
        answer = {"type": "auth_answer", "challenge_id": ATTACHMENT, "responses": ["x" * 4000] * 3}
        self.assertCode("CONTROL_TOO_LARGE", parse_control, json.dumps(answer))

    def test_invalid_control_direction_is_programmer_error(self):
        with self.assertRaises(ValueError):
            validate_control({}, direction="both")


class ConfigTests(unittest.TestCase):
    def test_defaults_are_internally_consistent_and_public_limits_are_safe(self):
        cfg = TerminalConfig()
        self.assertGreaterEqual(cfg.ring_bytes, cfg.output_high_water_bytes + cfg.ssh_window_bytes)
        public = cfg.public_limits()
        self.assertEqual(16384, public["frame_bytes"])
        self.assertNotIn("agent_buffer_bytes", public)
        self.assertNotIn("ticket", public)

    def test_known_environment_numeric_overrides(self):
        cfg = TerminalConfig.from_env({"CODEPIER_SSH_TERMINAL_WRITE_TIMEOUT": "0.5", "CODEPIER_SSH_TERMINAL_MAX_SESSIONS_PER_USER": "4", "OTHER_PASSWORD": "ignored"})
        self.assertEqual(0.5, cfg.write_timeout)
        self.assertEqual(4, cfg.max_sessions_per_user)

    def test_environment_cannot_disable_limits_or_smuggle_nonfinite_values(self):
        for value in ["NaN", "Infinity", "-1", "1e3", "", " 5", "5\n", "True"]:
            with self.subTest(value=value), self.assertRaises(ValueError):
                TerminalConfig.from_env({"CODEPIER_SSH_TERMINAL_WRITE_TIMEOUT": value})
        with self.assertRaises(ValueError):
            TerminalConfig.from_env({"CODEPIER_SSH_TERMINAL_FRAME_BYTES": "1024.0"})

    def test_cross_budget_and_timing_constraints(self):
        changes = [
            {"output_low_water_bytes": 262144}, {"ring_bytes": 65536},
            {"hub_relay_bytes": 262144}, {"pending_input_bytes": 4096},
            {"lease_renew_seconds": 30}, {"ticket_ttl_seconds": 5, "auth_timeout_seconds": 6},
            {"min_cols": 10, "max_cols": 9}, {"frame_bytes": True},
            {"agent_buffer_bytes": 1024 * 1024}, {"connect_timeout": float("nan")},
            {"frame_bytes": 10**1000},
        ]
        for change in changes:
            with self.subTest(change=change), self.assertRaises((ValueError, TypeError)):
                TerminalConfig(**change)


if __name__ == "__main__":
    unittest.main()
