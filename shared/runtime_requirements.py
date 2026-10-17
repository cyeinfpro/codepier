"""Wire validation for an immutable runtime requirement, never an approval."""
import re

PROTOCOL = 1
DRIVER = 'docker_restricted_v1'


def valid_requirement(value, project_id=None):
    return (isinstance(value, dict) and set(value) == {'protocol', 'driver', 'revision', 'project_id'}
        and type(value.get('protocol')) is int and value['protocol'] == PROTOCOL
        and value.get('driver') == DRIVER and isinstance(value.get('revision'), str)
        and re.fullmatch(r'[a-f0-9]{64}', value['revision']) is not None
        and isinstance(value.get('project_id'), str) and 1 <= len(value['project_id']) <= 100
        and (project_id is None or value['project_id'] == project_id))
