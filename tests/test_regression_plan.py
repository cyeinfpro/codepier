"""Exhaustiveness and failure semantics are executable contracts, not source greps."""
from __future__ import annotations
import copy
from pathlib import Path
import pytest
from scripts.regression_plan import available_workers, classify_events, fingerprint, merge_summaries, plan_jobs, pytest_command

NODES = ['tests/test_a.py::test_one', 'tests/test_a.py::test_matrix[light]',
         'tests/test_a.py::test_matrix[dark]', 'tests/test_b.py::test_two']
MARKERS = {node: ['isolated_case'] for node in NODES if 'matrix' in node}


def summaries(count=3):
    result = []
    for index in range(count):
        jobs = plan_jobs(NODES, MARKERS, shard_index=index, shard_count=count)
        result.append({'shard': {'index': index, 'count': count}, 'full_collection': NODES,
                       'collection_sha256': fingerprint(NODES), 'source_inventory_sha256': 'source-fixture',
                       'verified': True, 'outcomes': {n: 'passed' for job in jobs for n in job['nodeids']}})
    return result


@pytest.mark.parametrize('count', [1, 2, 3, 4])
def test_shards_partition_every_case_exactly_once(count):
    jobs = [job for index in range(count) for job in plan_jobs(NODES, MARKERS, shard_index=index, shard_count=count)]
    actual = [node for job in jobs for node in job['nodeids']]
    assert sorted(actual) == sorted(NODES)
    assert len(actual) == len(set(actual))
    assert len({job['name'] for job in jobs}) == len(jobs)
    leftover = next(job for job in jobs if job['nodeids'] == [NODES[0]])
    assert leftover['selectors'] == [NODES[0]]  # Must not rerun both isolated matrix cases.
    assert merge_summaries(summaries(count))['collected'] == len(NODES)


@pytest.mark.parametrize('problem', ['missing_shard', 'duplicate_shard', 'source', 'collection', 'unverified', 'skipped', 'missing_case', 'duplicate_case', 'unexpected', 'phases', 'process', 'stale'])
def test_merge_rejects_incomplete_or_misleading_evidence(problem):
    reports = copy.deepcopy(summaries())
    first, last = reports[0], reports[-1]
    if problem == 'missing_shard': reports.pop()
    elif problem == 'duplicate_shard': last['shard']['index'] = 0
    elif problem == 'source': last['source_inventory_sha256'] = 'other-source'
    elif problem == 'collection': last['collection_sha256'] = 'bad'
    elif problem == 'unverified': last['verified'] = False
    elif problem == 'skipped': last['outcomes'][next(iter(last['outcomes']))] = 'skipped'
    elif problem == 'missing_case': first['outcomes'].pop(next(iter(first['outcomes'])))
    elif problem == 'duplicate_case': last['outcomes'].update(first['outcomes'])
    elif problem == 'unexpected': last['unexpected_tests'] = ['tests/extra.py::case']
    elif problem == 'phases': last['duplicate_phases'] = ['tests/test_a.py::test_one']
    elif problem == 'process': last['module_failures'] = [{'exit_code': 1}]
    elif problem == 'stale': last['source_changed_during_run'] = ['hub/store.py']
    with pytest.raises(ValueError): merge_summaries(reports)


@pytest.mark.parametrize('count,index', [(0, 0), (65, 0), (1, -1), (2, 2)])
def test_invalid_shard_selection_is_not_silently_empty(count, index):
    with pytest.raises(ValueError): plan_jobs(NODES, shard_index=index, shard_count=count)


def test_bad_collection_and_duplicate_cases_are_rejected():
    with pytest.raises(ValueError): plan_jobs(NODES + [NODES[0]])
    with pytest.raises(ValueError): plan_jobs(['tests/../outside.py::case'])
    assert available_workers() >= 1


def test_serial_regression_marker_marks_only_its_job_exclusive():
    markers = {NODES[3]: ['serial_regression']}
    jobs = plan_jobs(NODES, markers)
    flagged = {node: job['exclusive'] for job in jobs for node in job['nodeids']}
    assert flagged[NODES[3]] is True
    assert all(flagged[node] is False for node in NODES[:3])


def test_computer_timeout_collection_runs_in_exclusive_job():
    from types import SimpleNamespace
    from tests.conftest import pytest_collection_modifyitems

    module = 'tests/test_computer_timeout_diagnostics.py'
    node = module + '::test_human_wait_does_not_consume_native_timeout[0.35]'
    marker_names = []
    item = SimpleNamespace(path=Path(__file__).with_name(Path(module).name), fixturenames=[],
                           add_marker=lambda marker: marker_names.append(marker.name))
    config = SimpleNamespace(getoption=lambda option: False)
    pytest_collection_modifyitems(config, [item])

    jobs = plan_jobs([node], {node: marker_names})
    assert 'serial_regression' in marker_names
    assert len(jobs) == 1
    assert jobs[0]['exclusive'] is True and jobs[0]['limited'] is False
    assert jobs[0]['nodeids'] == [node]


def test_browser_and_integration_markers_use_limited_pool_without_exclusive_mode():
    markers = {NODES[0]: ['browser'], NODES[3]: ['integration']}
    jobs = plan_jobs(NODES, markers)
    by_node = {node: job for job in jobs for node in job['nodeids']}
    assert by_node[NODES[0]]['limited'] and not by_node[NODES[0]]['exclusive']
    assert by_node[NODES[3]]['limited'] and not by_node[NODES[3]]['exclusive']


def test_each_job_owns_tmp_cache_xml_and_optional_coverage():
    directory = Path('isolated evidence') / 'case'
    command = pytest_command(['python', '-m', 'pytest'], directory, [NODES[0]], coverage=True)
    assert '--basetemp=' + str(directory / 'tmp') in command
    assert '--junitxml=' + str(directory / 'results.xml') in command
    assert 'cache_dir=' + str(directory / 'cache') in command
    assert '--cov=hub' in command and command[-1] == NODES[0]


@pytest.mark.parametrize('mode,expected', [('passed','passed'),('teardown_failed','failed'),('setup_skipped','skipped'),('xfail','skipped'),('missing','missing')])
def test_no_teardown_skip_or_missing_phase_can_make_a_green_receipt(mode, expected):
    events = [{'when': phase, 'outcome': 'passed'} for phase in ('setup', 'call', 'teardown')]
    if mode == 'teardown_failed': events[-1]['outcome'] = 'failed'
    elif mode == 'setup_skipped': events = [{'when': 'setup', 'outcome': 'skipped'}]
    elif mode == 'xfail': events[1]['xfail'] = True
    elif mode == 'missing': events.pop()
    outcome, duplicates = classify_events([NODES[0]], {NODES[0]: events})
    assert outcome[NODES[0]] == expected and not duplicates
    _, duplicates = classify_events([NODES[0]], {NODES[0]: events + [events[0]]})
    assert duplicates == [NODES[0]]


def test_large_collection_has_linear_node_parsing_budget():
    class CountedNode(str):
        splits = 0

        def split(self, *args, **kwargs):
            type(self).splits += 1
            return super().split(*args, **kwargs)

    nodes = [CountedNode(f'tests/test_module_{module:04d}.py::test_case_{case}')
             for module in range(1000) for case in range(10)]
    jobs = plan_jobs(nodes)
    assert len(jobs) == 1000
    assert sum(len(job['nodeids']) for job in jobs) == len(nodes)
    assert all(job['selectors'] == [job['module']] for job in jobs)
    assert CountedNode.splits <= 3 * len(nodes)


def resource_reports():
    from scripts.regression_plan import RESOURCE_LAYOUT, RESOURCE_NODEIDS
    nodes = NODES + sorted(RESOURCE_NODEIDS)
    markers = MARKERS | {node: ['resource_regression', 'serial_regression'] for node in RESOURCE_NODEIDS}
    result = []
    for index in range(5):
        jobs = plan_jobs(nodes, markers, shard_index=index, shard_count=5, resource_shard=True)
        result.append({'layout': RESOURCE_LAYOUT, 'shard': {'index': index, 'count': 5},
                       'full_collection': nodes, 'collection_sha256': fingerprint(nodes),
                       'source_inventory_sha256': 'source-fixture', 'verified': True,
                       'outcomes': {node: 'passed' for job in jobs for node in job['nodeids']}})
    return result


def test_resource_shard_is_exactly_once_and_has_no_preceding_jobs():
    from scripts.regression_plan import RESOURCE_NODEIDS
    reports = resource_reports()
    assert set(reports[4]['outcomes']) == RESOURCE_NODEIDS
    assert all(not set(report['outcomes']) & RESOURCE_NODEIDS for report in reports[:4])
    assert merge_summaries(reports)['collected'] == len(NODES) + len(RESOURCE_NODEIDS)


@pytest.mark.parametrize('problem', ['missing_marker', 'missing_test', 'extra_marked', 'extra_in_module', 'wrong_count'])
def test_resource_layout_rejects_silent_collection_drift(problem):
    from scripts.regression_plan import RESOURCE_NODEIDS
    resource = next(iter(RESOURCE_NODEIDS))
    nodes = NODES + [resource]
    markers = MARKERS | {resource: ['resource_regression']}
    count = 5
    if problem == 'missing_marker':
        markers.pop(resource)
    elif problem == 'missing_test':
        nodes.remove(resource)
    elif problem == 'extra_marked':
        markers[NODES[0]] = ['resource_regression']
    elif problem == 'extra_in_module':
        nodes.append(resource.split('::')[0] + '::test_unregistered')
    else:
        count = 4
    with pytest.raises(ValueError):
        plan_jobs(nodes, markers, shard_count=count, resource_shard=True)


@pytest.mark.parametrize('problem', ['missing_shard', 'duplicate_shard', 'missing_case', 'duplicate_case',
                                    'misplaced_resource', 'layout_mixed', 'failed_resource'])
def test_resource_merge_rejects_incomplete_or_misplaced_evidence(problem):
    from scripts.regression_plan import RESOURCE_NODEIDS
    reports = resource_reports()
    node = next(iter(RESOURCE_NODEIDS))
    if problem == 'missing_shard':
        reports.pop()
    elif problem == 'duplicate_shard':
        reports[4]['shard']['index'] = 0
    elif problem == 'missing_case':
        reports[4]['outcomes'].clear()
    elif problem == 'duplicate_case':
        reports[0]['outcomes'][node] = 'passed'
    elif problem == 'misplaced_resource':
        reports[0]['outcomes'][node] = reports[4]['outcomes'].pop(node)
    elif problem == 'layout_mixed':
        reports[4]['layout'] = 'ordinary'
    else:
        reports[4]['verified'] = False
    with pytest.raises(ValueError):
        merge_summaries(reports)
