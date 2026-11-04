"""Additive indexes for bounded collaboration history and background work."""


def migrate(db):
    indexes = {
        'collaboration_idempotency_created': ('collaboration_idempotency', 'created'),
        'collaboration_audit_created': ('collaboration_audit', 'created'),
        'telegram_inbox_created': ('telegram_inbox', 'created'),
        'mcp_event_deliveries_created': ('mcp_event_deliveries', 'created'),
        'mcp_event_deliveries_event_state': ('mcp_event_deliveries', 'event_id,state'),
        'room_mutations_created': ('room_mutations', 'created_at'),
        'dot_consumer_mutations_created': ('dot_consumer_mutations', 'created'),

        'collaboration_jobs_assignee': ('collaboration_jobs', 'assignee_agent_id,id'),
        'telegram_inbox_account_created': ('telegram_inbox', 'account_id,created'),
        'channel_attachments_owner_quota': ('channel_attachments', 'space_id,owner_user_id,state,size'),
        'collaboration_messages_reply': ('collaboration_messages', 'reply_to_id,created'),
        'dot_messages_message': ('collaboration_dot_messages', 'message_id,sequence'),
        'weixin_deliveries_message': ('weixin_deliveries', 'message_id,created'),
        'channel_mirrors_message': ('channel_mirrors', 'message_id'),
        'channel_mirrors_scope': ('channel_mirrors', 'room_id,conversation_id,created'),
        'channel_mirrors_binding_created': ('channel_mirrors', 'binding_id,created'),
        'delegation_requests_work': ('delegation_requests', 'work_item_id'),
        'coordination_messages_goal': ('coordination_messages', 'goal_id,created'),
        'room_members_legacy_dot': ('room_members', 'legacy_dot_id'),
        'room_work_bridges_policy': ('room_member_work_bridges', 'policy_id'),
        'room_work_bridges_legacy_dot': ('room_member_work_bridges', 'legacy_dot_id,member_id'),
        'room_native_assignments_member': ('room_native_work_assignments', 'member_id'),
        'room_work_sources_goal': ('room_work_sources', 'goal_id'),
        'channel_owner_messages_auth': ('channel_owner_messages', 'authorization_id'),
        'room_channel_owner_inputs_auth': ('room_channel_owner_inputs', 'authorization_id'),
        'collaboration_rooms_project': ('collaboration_rooms', 'project_id'),
        'monitor_evidence_expiry': ('monitor_evidence', 'expires_at'),
        'monitor_samples_collected': ('monitor_samples', 'collected_at'),
        'monitor_incidents_evidence': ('monitor_incidents', 'evidence_id'),
        'coordination_goals_pending': ('coordination_goals', 'state,id'),
        'coordination_goals_conversation_page': ('coordination_goals', 'conversation_id,created,id'),
        'delegation_requests_policy_goal': ('delegation_requests', 'policy_id,policy_version,goal_id'),
    }
    for name, (table, columns) in indexes.items():
        db.execute(f'CREATE INDEX IF NOT EXISTS {name} ON {table}({columns})')
    db.execute("""CREATE INDEX IF NOT EXISTS mcp_event_policy_object_sequence ON mcp_event_outbox(
        name,json_extract(data,'$.policy_id'),json_extract(data,'$.policy_version'),object_id,seq)""")
