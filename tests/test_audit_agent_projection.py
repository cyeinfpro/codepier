"""Agent listing computes historical activity once per page."""
from tests.collaboration_support import collab as collab


def test_agent_activity_page_uses_three_aggregates_and_preserves_views(collab, monkeypatch):
    service, owner, _, _, room, _, _, _ = collab
    rows = service.store.all('SELECT * FROM collaboration_agents WHERE room_id=?', (room['id'],))
    assert len(rows) >= 2
    expected = {row['id']: service.agent_view(room, row) for row in rows}
    queries = []
    original = service.store.all
    def tracked(sql, args=()):
        if 'GROUP BY' in sql:
            queries.append(sql)
        return original(sql, args)
    monkeypatch.setattr(service.store, 'all', tracked)
    page = service.listing(owner, room, 'agents', 100)
    assert {row['id']: row for row in page['items']} == expected
    assert len(queries) == 3
    queries.clear()
    assert service.agent_activities(room, []) == {}
    assert queries == []
