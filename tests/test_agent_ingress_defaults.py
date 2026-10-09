"""Default admission never changes existing project or source policies."""
from types import SimpleNamespace

import pytest

from agent.integration_config import file_import_streaming_enabled, validate_integrations
from agent.integrations import Integrations
from shared.file_sources import file_source_policy
from shared.util import DevError


@pytest.mark.parametrize(("config", "enabled"), [
    ({}, True), ({"file_import_streaming": True}, True),
    ({"file_import_streaming": False}, False),
    ({"file_import_streaming": None}, False),
    ({"file_import_streaming": "true"}, False),
    ({"file_import_streaming": 1}, False),
    ({"file_import_streaming": 0}, False),
    ({"file_import_streaming": []}, False), (None, False), ([], False),
])
def test_default_and_explicit_invalid_values(config, enabled):
    assert file_import_streaming_enabled(config) is enabled


def test_validation_preserves_inherited_vs_explicit_choice():
    inherited = validate_integrations({})
    assert "file_import_streaming" not in inherited
    assert file_import_streaming_enabled(inherited) is True
    disabled = validate_integrations({"file_import_streaming": False})
    assert disabled["file_import_streaming"] is False
    assert file_import_streaming_enabled(disabled) is False
    assert file_source_policy(inherited)["file_source_providers"] == []
    restricted = validate_integrations({"file_hosts": [], "file_source_providers": []})
    assert restricted["file_hosts"] == []
    assert file_source_policy(restricted)["allowed_hosts"] == []


@pytest.mark.parametrize("value", [None, "true", 1, 0, [], {}])
def test_invalid_explicit_flag_rejected_by_config_loader(value):
    with pytest.raises(ValueError, match="file_import_streaming"):
        validate_integrations({"file_import_streaming": value})


@pytest.mark.parametrize(("config", "enabled"), [
    ({}, True), ({"integrations": {}}, True),
    ({"integrations": {"file_import_streaming": True}}, True),
    ({"integrations": {"file_import_streaming": False}}, False),
])
def test_execution_uses_same_effective_flag(config, enabled):
    integration = Integrations.__new__(Integrations)
    integration.agent = SimpleNamespace(config=config)
    existing = integration.incoming_uploads = object()
    if enabled:
        assert integration.upload_service() is existing
    else:
        with pytest.raises(DevError) as exc:
            integration.upload_service()
        assert exc.value.code == "FILE_IMPORT_DISABLED"
        assert integration.incoming_uploads is existing
