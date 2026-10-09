"""Settings readiness through a real disposable Hub/Agent, without a browser."""
from tests.support import wait_for


def test_settings_center_reads_real_node_without_exposing_local_roots(stack):
    catalog = stack.must(stack.client.get("/api/settings/catalog", params={"project": stack.project["id"]}))
    assert catalog["selected_project"] == stack.project["id"]
    assert str(stack.root) not in str(catalog)
    receipts = []
    def checked():
        result = stack.must(stack.client.get("/api/settings/node", params={"project": stack.project["id"]}))
        if result.get("operation_id"):
            receipts.append(result["operation_id"])
        return result if result["state"] == "checked" else None
    result = wait_for(checked, timeout=25)
    assert type(result["values"]["node_ingress"]) is bool
    assert "allowed_hosts" in result["values"]["node_file_sources"]
    assert str(stack.root) not in str(result)
    assert len(set(receipts)) <= 1
    repeat = checked()
    assert repeat["values"] == result["values"]
