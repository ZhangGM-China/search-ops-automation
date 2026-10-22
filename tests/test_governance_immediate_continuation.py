from datetime import date
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from sqlalchemy import create_engine, event, select
from sqlalchemy.orm import sessionmaker

from src.persistence.models import Base
from src.query_governance.checkpoint import SqlAlchemyCheckpointStore
from src.query_governance.models import QueryGovernanceTask, QuerySpec, NodeResult
from src.query_governance.selection import (BrandQueryMetrics, CandidateDataset,
    InMemoryQuerySelectionStore, QuerySelectionService)
from src.query_governance.workflow import QueryGovernanceWorkflow
import src.query_governance.scheduler as jobs
from test_query_governance_checkpoint import Search, Actions, MODEL_CONFIG_SNAPSHOT
from test_query_governance_selection import metrics


@pytest.fixture
def runtime(tmp_path, monkeypatch):
    engine = create_engine('sqlite:///' + str(tmp_path / 'continuation.db'))
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine)
    monkeypatch.setenv('TOP_QUERY_TASK_LOCK_DIR', str(tmp_path / 'locks'))
    monkeypatch.setattr(jobs, 'reload_config', lambda: None)
    monkeypatch.setattr(jobs, 'BRAND_CONFIGS', {'atlas': {}, 'harbor': {}})
    monkeypatch.setattr(jobs, 'is_brand_enabled', lambda _: True)
    monkeypatch.setattr(jobs, 'get_brand_business_config', lambda _: {})
    monkeypatch.setattr(jobs, 'check_modes_for_date', lambda *_: {})
    monkeypatch.setattr(jobs, 'QUERY_GOVERNANCE_MAX_NODE_STARTS', 2)
    yield factory
    engine.dispose()


def setup_brands(runtime, brands=('atlas', 'harbor'), *, on_search=None):
    events, writes, workflows, selections, sources = [], [], {}, {}, {}
    for brand in brands:
        class ObservedSearch(Search):
            def mixed(self, brand, query):
                events.append(('s0', brand, query))
                if on_search:
                    on_search(brand, query)
                return super().mixed(brand, query)
        class Runner:
            def __init__(self, node):
                self.node = node
            def run(self, spec, context):
                events.append((self.node, spec.brand, spec.query))
                return NodeResult.executed({}, action={'type': self.node})
        class Gateway(Actions):
            def execute_and_verify(self, spec, action, context):
                writes.append((spec.brand, spec.query_id, action['type']))
                return NodeResult.executed({})
        store = SqlAlchemyCheckpointStore(runtime)
        workflows[brand] = QueryGovernanceWorkflow(
            search=ObservedSearch(), store=store, checkpoint=store,
            runners={n: Runner(n) for n in ('recall', 'reorder', 'relevance')},
            action_gateway=Gateway(), model_config_snapshot=MODEL_CONFIG_SNAPSHOT)
        source = Mock()
        source.fetch.return_value = CandidateDataset(
            BrandQueryMetrics(100, 20, 5), tuple(metrics('连衣裙' + str(i), pv=30) for i in range(5)))
        sources[brand] = source
        selections[brand] = QuerySelectionService(source=source, store=InMemoryQuerySelectionStore())
    return workflows, selections, sources, events, writes


def run_mainline(runtime, workflows, selections, **kwargs):
    return jobs.query_governance_mainline_job(
        target_date=date(2026, 10, 8), workflow_factory=workflows.__getitem__,
        session_factory=runtime, selection_service_factory=selections.__getitem__,
        experiment_service_factory=lambda: SimpleNamespace(get_active_experiment_for_brand=lambda *_a, **_kw: None),
        overview_summary_materializer=lambda *_: None, **kwargs)


def test_mainline_finishes_yields_immediately_and_round_robins_frozen_brands(runtime, monkeypatch):
    workflows, selections, sources, events, writes = setup_brands(runtime)
    slices = []
    for brand, workflow in workflows.items():
        original = workflow.run_batch
        def observe(*args, _brand=brand, _workflow=workflow, _original=original, **kwargs):
            result = _original(*args, **kwargs)
            slices.append((_brand, _workflow._node_starts))
            return result
        monkeypatch.setattr(workflow, 'run_batch', observe)
    result = run_mainline(runtime, workflows, selections)
    assert [result['brands'][b]['status'] for b in workflows] == ['completed', 'completed']
    assert [brand for brand, _ in slices[:6]] == ['atlas', 'harbor'] * 3
    assert all(count <= 2 for _, count in slices)
    assert all(source.fetch.call_count == 1 for source in sources.values())
    assert len(writes) == len(set(writes))
    with runtime() as db:
        assert len(db.scalars(select(QueryGovernanceTask)).all()) == 10
    for brand in workflows:
        nodes = [node for node, b, _ in events if b == brand]
        # Later Mixed reads verify action effects; the first five are the S0 barrier.
        assert nodes[:5] == ['s0'] * 5
        assert nodes.index('recall') >= 5
        if 'reorder' in nodes:
            assert max(i for i, n in enumerate(nodes) if n == 'reorder') < nodes.index('relevance')


def test_shutdown_stops_new_nodes_and_continuation(runtime):
    stopped = [False]
    workflows, selections, sources, events, _ = setup_brands(runtime, ('atlas',),
        on_search=lambda *_: stopped.__setitem__(0, True))
    workflows['atlas'].should_continue = lambda: not stopped[0]
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
    assert result['brands']['atlas']['status'] == 'running'
    assert len(events) == 1


def test_continuation_observes_controls_once(runtime):
    workflows, _, _, events, writes = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    observer = Mock()
    workflow.experiment_observer = observer
    specs = [QuerySpec('t' + str(i), 'atlas', '连衣裙' + str(i), experiment_id=1, group_type='treatment') for i in range(5)]
    specs.append(QuerySpec('control', 'atlas', '外套', experiment_id=1, group_type='control'))
    workflow.run_batch(specs, batch_id='experiment', data_date=date(2026, 10, 8))
    for _ in range(30):
        if not workflow.invocation_yielded:
            break
        workflow.resume_yielded_batch()
    assert workflow.checkpoint.get_checkpoint('experiment').status == 'completed'
    controls = [call for call in observer.record_observation.call_args_list if call.args[0].is_control]
    assert len(controls) == 1
    assert [(node, query) for node, _, query in events if query == '外套'] == [('s0', '外套')]
    assert all(query_id != 'control' for _, query_id, _ in writes)


def test_no_progress_yield_returns_without_busy_loop(runtime, monkeypatch):
    workflows, selections, _, events, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    resume = Mock(wraps=workflow.resume_yielded_batch)
    monkeypatch.setattr(workflow, 'resume_yielded_batch', resume)
    monkeypatch.setattr(jobs, '_workflow_progress', lambda *_: ('running', 0))
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
    assert result['brands']['atlas']['status'] == 'running'
    assert resume.call_count == 1
    assert len(events) <= 4


@pytest.mark.parametrize('failure', ['disabled', 'busy', 'fence'])
def test_continuation_isolates_disabled_busy_or_lost_fence(runtime, monkeypatch, failure):
    from src.persistence import SchedulerLeaderFenceError
    from src.runtime.process_guard import ProcessLockError
    workflows, selections, _, events, _ = setup_brands(runtime)
    atlas = workflows['atlas']
    original = atlas.run_batch
    def initial(*args, **kwargs):
        result = original(*args, **kwargs)
        if failure == 'disabled':
            monkeypatch.setattr(jobs, 'is_brand_enabled', lambda b: b != 'atlas')
        else:
            atlas.assert_scheduler_fence = lambda: (_ for _ in ()).throw(SchedulerLeaderFenceError('lost'))
        return result
    if failure == 'busy':
        acquire = jobs.acquire_process_lock
        def busy(key, **kwargs):
            import hashlib
            if key == 'brand-governance-' + hashlib.sha256(b'atlas').hexdigest():
                raise ProcessLockError('busy')
            return acquire(key, **kwargs)
        monkeypatch.setattr(jobs, 'acquire_process_lock', busy)
    else:
        monkeypatch.setattr(atlas, 'run_batch', initial)
    result = run_mainline(runtime, workflows, selections, brand_lock_wait_seconds=0)
    assert result['brands']['harbor']['status'] == 'completed'
    assert result['brands']['atlas']['status'] == ('error' if failure == 'fence' else 'skipped')
    assert sum(b == 'atlas' for _, b, _ in events) == (0 if failure == 'busy' else 2)
    if failure == 'busy':
        assert result['brands']['atlas']['reason'] == 'batch_busy'
        assert result['skippedBrands'] == ['atlas:batch_busy']


def test_brand_lock_wait_recovers_after_transient_busy(runtime, monkeypatch, caplog):
    from src.runtime.process_guard import ProcessLockError
    workflows, selections, _, events, _ = setup_brands(runtime, ('atlas',))
    acquire = jobs.acquire_process_lock
    busy_once = {'raised': False}
    def transient(key, **kwargs):
        import hashlib
        if (key == 'brand-governance-' + hashlib.sha256(b'atlas').hexdigest()
                and not busy_once['raised']):
            busy_once['raised'] = True
            raise ProcessLockError('busy')
        return acquire(key, **kwargs)
    monkeypatch.setattr(jobs, 'acquire_process_lock', transient)
    monkeypatch.setattr(jobs, 'QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS', 0)
    with caplog.at_level('WARNING', logger='src.query_governance.scheduler'):
        result = run_mainline(runtime, workflows, selections, brand_filter='atlas', brand_lock_wait_seconds=60)
    assert result['brands']['atlas']['status'] == 'completed'
    assert result['skippedBrands'] == []
    assert any('brand lock busy, waiting' in record.getMessage() for record in caplog.records)
    # 瞬时占用不应丢品牌：S0 事件照常发生。
    assert any(node == 's0' for node, _, _ in events)


def test_brand_lock_wait_exhaustion_skips_brand_with_reason(runtime, monkeypatch, caplog):
    from src.runtime.process_guard import ProcessLockError
    workflows, selections, sources, events, _ = setup_brands(runtime, ('atlas',))
    acquire = jobs.acquire_process_lock
    def always_busy(key, **kwargs):
        import hashlib
        if key == 'brand-governance-' + hashlib.sha256(b'atlas').hexdigest():
            raise ProcessLockError('busy')
        return acquire(key, **kwargs)
    monkeypatch.setattr(jobs, 'acquire_process_lock', always_busy)
    monkeypatch.setattr(jobs, 'QUERY_GOVERNANCE_BRAND_LOCK_POLL_SECONDS', 0)
    with caplog.at_level('WARNING', logger='src.query_governance.scheduler'):
        result = run_mainline(runtime, workflows, selections, brand_filter='atlas', brand_lock_wait_seconds=0.1)
    assert result['brands']['atlas']['status'] == 'skipped'
    assert result['brands']['atlas']['reason'] == 'batch_busy'
    assert result['skippedBrands'] == ['atlas:batch_busy']
    assert result['status'] == 'success'
    assert sources['atlas'].fetch.call_count == 0
    assert any('brand lock still busy' in record.getMessage() for record in caplog.records)
    assert any('skipped for the day' in record.getMessage() for record in caplog.records)


def test_tracked_metrics_are_materialized_once_per_batch(runtime, monkeypatch):
    workflows, selections, _, _, _ = setup_brands(runtime)
    materialize = Mock(return_value=7)
    monkeypatch.setattr(jobs, '_materialize_tracked_query_metrics', materialize)
    result = run_mainline(runtime, workflows, selections,
        metric_provider_factory=lambda _: lambda *_: {})
    assert materialize.call_count == 2
    assert all(item['materializedMetricCount'] == 7 for item in result['brands'].values())


def test_metric_materialization_failure_does_not_fail_brand_or_drop_continuation(runtime, monkeypatch):
    workflows, selections, sources, _, _ = setup_brands(runtime, ('atlas',))
    materialize = Mock(side_effect=RuntimeError('MySQL server has gone away'))
    monkeypatch.setattr(jobs, '_materialize_tracked_query_metrics', materialize)
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas',
        metric_provider_factory=lambda _: lambda *_: {})
    # One retry, then the cache failure is skipped without killing the brand.
    assert materialize.call_count == 2
    assert result['brands']['atlas']['status'] == 'completed'
    assert result['brands']['atlas']['materializedMetricCount'] == 0
    assert result['status'] == 'success'
    with runtime() as db:
        assert len(db.scalars(select(QueryGovernanceTask)).all()) == 5


def test_500_queries_finish_in_one_scheduler_run_without_duplicate_actions(runtime, monkeypatch):
    workflows, selections, sources, events, writes = setup_brands(runtime, ('atlas',))
    source = sources['atlas']
    source.fetch.return_value = CandidateDataset(BrandQueryMetrics(10000, 2000, 500),
        tuple(metrics('连衣裙' + str(i), pv=30) for i in range(500)))
    monkeypatch.setattr(jobs, 'get_brand_business_config', lambda _: {
        'query_governance_extract_limit': 500, 'query_governance_candidate_high_pv_limit': 500})
    monkeypatch.setattr(jobs, 'QUERY_GOVERNANCE_MAX_NODE_STARTS', 100)
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
    assert result['brands']['atlas']['status'] == 'completed'
    assert result['brands']['atlas']['selectedCount'] == 500
    assert source.fetch.call_count == 1
    assert len(writes) == len(set(writes))
    assert len({query_id for _, query_id, _ in writes}) == 500


def test_control_snapshot_survives_observer_failure_and_does_not_accumulate_slice_results(runtime):
    workflows, _, _, events, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    observer = Mock()
    observer.record_observation.side_effect = RuntimeError('metrics unavailable')
    workflow.experiment_observer = observer
    specs = [QuerySpec('t', 'atlas', '连衣裙'), QuerySpec('c', 'atlas', '外套', group_type='control')]
    workflow.run_batch(specs, batch_id='controls-fail')
    for _ in range(8):
        if not workflow.invocation_yielded:
            break
        previous = workflow._batch_results
        workflow.resume_yielded_batch()
        assert workflow._batch_results is not previous
        assert set(workflow._observed_control_results) == {'c'}
    assert observer.record_observation.call_count == 1
    assert sum(query == '外套' for _, _, query in events) == 1


def test_continuation_uses_batch_checkpoint_reads(runtime):
    workflows, _, _, _, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    store = workflow.checkpoint
    assert store is not None
    get_node = store.get_node
    batch_reads = []

    def get_nodes(batch_id, specs, node):
        specs = tuple(specs)
        batch_reads.append((node, len(specs)))
        return {
            (spec.brand, spec.query): get_node(batch_id, spec, node)
            for spec in specs
        }

    store.get_nodes = get_nodes
    store.get_node = lambda *_args, **_kwargs: pytest.fail(
        "continuation must use one batch checkpoint read per node"
    )
    specs = [
        QuerySpec(f"q{i}", 'atlas', f'连衣裙{i}')
        for i in range(5)
    ]
    workflow.run_batch(specs, batch_id='batch-read', data_date=date(2026, 10, 8))
    while workflow.invocation_yielded:
        workflow.resume_yielded_batch()

    assert batch_reads
    assert all(size == len(specs) for _, size in batch_reads)


def test_batch_checkpoint_read_uses_one_select(runtime):
    workflows, _, _, _, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    specs = [
        QuerySpec(f"q{i}", 'atlas', f'连衣裙{i}')
        for i in range(5)
    ]
    workflow.run_batch(specs, batch_id='single-select', data_date=date(2026, 10, 8))
    while workflow.invocation_yielded:
        workflow.resume_yielded_batch()

    statements = []
    engine = runtime.kw['bind']

    def capture(_conn, _cursor, statement, _parameters, _context, _executemany):
        statements.append(statement)

    event.listen(engine, 'before_cursor_execute', capture)
    try:
        loaded = workflow.checkpoint.get_nodes('single-select', specs, 's0')
    finally:
        event.remove(engine, 'before_cursor_execute', capture)

    assert len(loaded) == len(specs)
    assert sum(statement.lstrip().upper().startswith('SELECT') for statement in statements) == 1


def test_experiment_scheduler_continues_frozen_treatment_and_reads_controls_once(runtime, monkeypatch):
    from src.query_governance.experiments import QueryGovernanceExperimentService, QueryGovernanceExperimentScheduler
    service = QueryGovernanceExperimentService(runtime)
    draft = service.create_or_replace_draft(brand='atlas', created_by='operator', experiment_name='fixed',
        treatment_queries=['连衣裙' + str(i) for i in range(5)], control_queries=['外套'], today=date(2026, 10, 8))
    service.schedule_experiment(draft['id'])
    workflows, _, _, events, writes = setup_brands(runtime, ('atlas',))
    observer = Mock()
    monkeypatch.setattr(jobs, 'QueryGovernanceExperimentScheduler', lambda session_factory, workflow_factory:
        QueryGovernanceExperimentScheduler(session_factory, workflow_factory, observer_factory=lambda _: observer))
    workflow_factory = Mock(side_effect=workflows.__getitem__)
    result = jobs.query_governance_mainline_job(brand_filter='atlas', target_date=date(2026, 10, 8),
        workflow_factory=workflow_factory, session_factory=runtime,
        overview_summary_materializer=lambda *_: None,
        selection_service_factory=lambda _: pytest.fail('experiment must not dynamically select'))
    assert result['brands']['atlas']['status'] == 'completed'
    assert result['brands']['atlas']['treatmentCount'] == 5
    assert result['brands']['atlas']['controlCount'] == 1
    assert workflow_factory.call_count == 1
    assert observer.record_observation.call_count == 1
    assert observer.record.call_count == 5
    assert sum(query == '外套' for _, _, query in events) == 1
    assert all('外套' not in query_id for _, query_id, _ in writes)


def test_time_budget_resets_for_each_immediate_slice(runtime, monkeypatch):
    clock = [0.0]
    monkeypatch.setattr('src.query_governance.workflow.time.monotonic', lambda: clock[0])
    workflows, selections, _, _, writes = setup_brands(runtime, ('atlas',),
        on_search=lambda *_: clock.__setitem__(0, clock[0] + 241))
    slices = []
    workflow = workflows['atlas']
    original = workflow.run_batch
    def observe(*args, **kwargs):
        result = original(*args, **kwargs)
        slices.append(workflow._node_starts)
        return result
    monkeypatch.setattr(workflow, 'run_batch', observe)
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
    assert result['brands']['atlas']['status'] == 'completed'
    assert slices[:5] == [1] * 5
    assert len(writes) == len(set(writes))


def test_shutdown_does_not_open_the_next_brand_or_fetch_new_samples(runtime):
    stopped = [False]
    workflows, selections, sources, events, _ = setup_brands(runtime,
        on_search=lambda *_: stopped.__setitem__(0, True))
    for workflow in workflows.values():
        workflow.should_continue = lambda: not stopped[0]
    result = run_mainline(runtime, workflows, selections)
    assert result['brands']['atlas']['status'] == 'running'
    assert result['brands']['harbor']['reason'] == 'scheduler_stopping'
    assert sources['harbor'].fetch.call_count == 0
    assert all(brand == 'atlas' for _, brand, _ in events)


def test_brand_lock_is_held_across_immediate_continuation(runtime, monkeypatch, tmp_path):
    workflows, selections, _, _, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    original_resume = workflow.resume_yielded_batch
    checked = []
    def resume_with_lock_check():
        import hashlib
        from src.runtime.process_guard import ProcessLockError, acquire_process_lock
        try:
            acquire_process_lock('brand-governance-' + hashlib.sha256(b'atlas').hexdigest(),
                                 lock_dir=tmp_path / 'locks')
        except ProcessLockError:
            checked.append(True)
        recovered = jobs.query_governance_recovery_job(
            workflow_factory=lambda _: pytest.fail('active continuation must retain its brand lock'),
            session_factory=runtime)
        assert recovered['candidateCount'] == 1
        assert recovered['skippedCount'] == 1
        assert recovered['skips'][0]['reason'] == 'batch_busy'
        assert recovered['resumedCount'] == 0
        return original_resume()
    monkeypatch.setattr(workflow, 'resume_yielded_batch', resume_with_lock_check)
    result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
    assert result['brands']['atlas']['status'] == 'completed'
    assert checked


@pytest.mark.parametrize('failure', ['callback', 'reload', 'resume'])
def test_held_brand_locks_release_even_on_continuation_exception(runtime, monkeypatch, tmp_path, failure):
    import hashlib
    from src.runtime.process_guard import acquire_process_lock
    workflows, selections, _, _, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    if failure == 'resume':
        workflow.resume_yielded_batch = Mock(side_effect=RuntimeError('broken'))
    elif failure == 'reload':
        calls = [0]
        def reload():
            calls[0] += 1
            if calls[0] > 1:
                raise RuntimeError('broken')
        monkeypatch.setattr(jobs, 'reload_config', reload)
    else:
        original = workflow.run_batch
        def initial(*args, **kwargs):
            result = original(*args, **kwargs)
            workflow.should_continue = lambda: (_ for _ in ()).throw(RuntimeError('broken'))
            return result
        workflow.run_batch = initial
    if failure == 'resume':
        result = run_mainline(runtime, workflows, selections, brand_filter='atlas')
        assert result['brands']['atlas']['status'] == 'error'
    else:
        with pytest.raises(RuntimeError, match='broken'):
            run_mainline(runtime, workflows, selections, brand_filter='atlas')
    lock = acquire_process_lock('brand-governance-' + hashlib.sha256(b'atlas').hexdigest(), lock_dir=tmp_path / 'locks')
    lock.release()


def test_shutdown_between_control_reads_stops_unread_controls(runtime):
    stopped = [False]
    workflows, _, _, events, _ = setup_brands(runtime, ('atlas',),
        on_search=lambda *_: stopped.__setitem__(0, True))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    workflow.should_continue = lambda: not stopped[0]
    specs = [QuerySpec('t', 'atlas', '连衣裙')]
    specs += [QuerySpec('c' + str(i), 'atlas', '外套' + str(i), group_type='control') for i in range(3)]
    workflow.run_batch(specs, batch_id='shutdown-controls')
    assert [query for _, _, query in events] == ['外套0']
    assert set(workflow._observed_control_results) == {'c0'}
    assert not workflow.invocation_yielded


def test_lost_fence_on_resume_preserves_checkpoint_version_and_yield_marker(runtime):
    from src.persistence import SchedulerLeaderFenceError
    from src.query_governance.models import QueryGovernanceWorkflowRun
    import json
    workflows, _, _, _, _ = setup_brands(runtime, ('atlas',))
    workflow = workflows['atlas']
    workflow.max_node_starts = 2
    specs = [QuerySpec(str(i), 'atlas', '连衣裙' + str(i)) for i in range(5)]
    workflow.run_batch(specs, batch_id='fence-before-checkpoint')
    with runtime() as db:
        row = db.scalars(select(QueryGovernanceWorkflowRun)).one()
        before = (row.checkpoint_version, row.checkpoint_payload)
        assert json.loads(row.checkpoint_payload)['invocation_yielded'] is True
    workflow.assert_scheduler_fence = lambda: (_ for _ in ()).throw(SchedulerLeaderFenceError('lost'))
    with pytest.raises(SchedulerLeaderFenceError):
        workflow.resume_yielded_batch()
    with runtime() as db:
        row = db.scalars(select(QueryGovernanceWorkflowRun)).one()
        assert (row.checkpoint_version, row.checkpoint_payload) == before
