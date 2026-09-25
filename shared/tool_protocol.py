"""Additive Hub/Agent tool-contract negotiation, independent of transport crypto.

A catalog digest identifies a catalog, not semantic compatibility or authenticity.
Increment an individual wire epoch and its minimum release for a breaking tool
change; adding an unrelated tool must never break existing receipt recovery.
"""
from __future__ import annotations
from dataclasses import dataclass
import re
import hashlib
import json
from functools import lru_cache
from shared.util import DevError, VERSION

CONTRACT_PROTOCOL = 1
# Explicit overrides live here when a tool's *meaning*, not just optional schema,
# changes. Existing backend contracts default to epoch 1 and the original release floor.
# Epoch 2 prevents legacy peers (which assume epoch 1 for every name)
# from accepting primitives that they do not implement.
TOOL_WIRE_VERSIONS: dict[str, int] = {name: 2 for name in ("read", "write", "edit", "exec")}
TOOL_MINIMUM_RELEASES: dict[str, tuple[int, int, int]] = {}
MINIMUM_RELEASE = (1, 0, 0)


def semantic_version(value):
    if not isinstance(value, str) or len(value) > 80:
        return None
    match = re.fullmatch(r'(0|[1-9]\d*)\.(0|[1-9]\d*)\.(0|[1-9]\d*)(?:[-+][0-9A-Za-z.-]+)?', value)
    return tuple(map(int, match.groups())) if match else None


def wire_version(name):
    return TOOL_WIRE_VERSIONS.get(name, 1)


def advertisement(catalog_sha256):
    # Contract definitions register integration tools; import only at composition
    # time, after that registration, to avoid a contracts/protocol import cycle.
    from shared.contracts import TOOLS
    from shared.agent_lifecycle import DEVICE_ACTIONS
    names = {name for name, tool in TOOLS.items() if not tool.local} | DEVICE_ACTIONS | {'system_validate'}
    return {'version': VERSION, 'contract_protocol': CONTRACT_PROTOCOL,
            'catalog_sha256': catalog_sha256,
            'tool_contracts': {name: wire_version(name) for name in sorted(names)}}


@dataclass(frozen=True)
class PeerContract:
    release: tuple[int, int, int] | None
    legacy: bool
    catalog_sha256: str | None
    tools: dict[str, int] | None

    def public(self, local_catalog):
        return {'version': '.'.join(map(str, self.release)) if self.release else None,
                'contract_protocol': 0 if self.legacy else CONTRACT_PROTOCOL,
                'catalog_sha256': self.catalog_sha256,
                'catalog_matches': self.catalog_sha256 == local_catalog if self.catalog_sha256 else None,
                'legacy_compatibility': self.legacy,
                'note': 'Catalog mismatch can be additive; admission checks the requested tool epoch and minimum release. Receipts remain recoverable.'}


def negotiate(frame):
    if not isinstance(frame, dict):
        raise ValueError('Invalid peer protocol frame')
    release = semantic_version(frame.get('version'))
    if 'contract_protocol' not in frame:
        return PeerContract(release, True, None, None)
    if type(frame['contract_protocol']) is not int or frame['contract_protocol'] != CONTRACT_PROTOCOL:
        raise ValueError('Unsupported tool contract protocol')
    catalog, tools = frame.get('catalog_sha256'), frame.get('tool_contracts')
    if (release is None or release < MINIMUM_RELEASE or not isinstance(catalog, str)
            or not re.fullmatch(r'[a-f0-9]{64}', catalog) or not isinstance(tools, dict)
            or not 1 <= len(tools) <= 512):
        raise ValueError('Invalid tool contract advertisement')
    if any(not isinstance(name, str) or not re.fullmatch(r'[a-z][a-z0-9_]{0,99}', name)
           or type(epoch) is not int or not 1 <= epoch <= 65535 for name, epoch in tools.items()):
        raise ValueError('Invalid tool contract entry')
    return PeerContract(release, False, catalog, dict(tools))


def require_compatible(peer, name):
    if peer is None:  # Existing in-process adapters without a transport handshake.
        return
    required = TOOL_MINIMUM_RELEASES.get(name, MINIMUM_RELEASE)
    release_ok = peer.release is None and peer.legacy and required == MINIMUM_RELEASE or peer.release is not None and peer.release >= required
    epoch_ok = wire_version(name) == (1 if peer.legacy else peer.tools.get(name))
    if not release_ok or not epoch_ok:
        raise DevError('AGENT_UPGRADE_REQUIRED', 'Hub/Agent 工具语义版本不兼容；请核对版本并更新。未开始新操作，旧操作仍可按原编号查询。', 409)


def validate_call_epoch(name, epoch):
    if epoch is None:
        epoch = 1  # Legacy delivery-v2 frames are frozen at wire epoch 1.
    if type(epoch) is not int or epoch != wire_version(name):
        raise DevError('TOOL_PROTOCOL_MISMATCH', '收到的工具语义版本与本机实现不一致；未执行工具，请更新 Hub/Agent', 409)


@lru_cache(maxsize=1)
def catalog_digest():
    # Resolve after contract registration; shared by runtime and handshake only.
    from shared.contracts import tool_definitions
    return hashlib.sha256(json.dumps(tool_definitions(), sort_keys=True, ensure_ascii=False).encode()).hexdigest()
