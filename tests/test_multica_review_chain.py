import importlib
from uuid import uuid4

import pytest

from test_daily_research_recovery import DAY, meeting
from test_multica_reviews import reviews, response


class ChainCLI:
    def __init__(self, fixture, reject=None, missing_receipt=None, failed=None):
        self.fixture = fixture
        module, runner, _, state = fixture
        self.agents = [{'id': agent_id, 'name': name, 'model': 'test-model',
                        'runtime_id': str(uuid4())} for name, agent_id in state['agent_ids'].items()]
        self.issues = {}
        self.runs = {}
        self.created = []
        self.reject = reject
        self.missing_receipt = missing_receipt
        self.failed = failed

    def _run_json(self, args, **kwargs):
        module, runner, _, state = self.fixture
        if args[:2] == ['agent', 'list']:
            return self.agents
        if args[:2] == ['agent', 'get']:
            return next(a for a in self.agents if a['id'] == args[2])
        if args[:2] == ['issue', 'list']:
            return {'issues': list(self.issues.values()), 'has_more': False}
        if args[:2] == ['issue', 'create']:
            assert all(r['status'] in ('completed', 'failed', 'cancelled') for r in self.runs.values())
            role = list(module.ROLES)[len(self.created)]
            bundle = module.prepare_review(DAY, role, team=runner.team)
            agent = next(a for a in self.agents if a['id'] == args[args.index('--assignee-id') + 1])
            issue = {'id': str(uuid4()), 'title': args[args.index('--title') + 1],
                     'project_id': state['project_id'], 'workspace_id': state['workspace_id'],
                     'assignee_id': agent['id'], 'status': 'todo', 'identifier': 'TEST-' + str(len(self.created) + 1)}
            self.issues[issue['id']] = issue
            task = {'id': str(uuid4()), 'agent_id': agent['id'], 'runtime_id': agent['runtime_id'],
                    'status': 'running', 'usage': [{'model': 'test-model'}]}
            self.runs[issue['id']] = task
            self.created.append(role)
            if role == self.failed:
                task['status'] = 'failed'
                return issue
            if role != self.missing_receipt:
                env = {'MULTICA_TASK_ID': task['id'], 'MULTICA_AGENT_ID': agent['id'],
                       'MULTICA_WORKSPACE_ID': state['workspace_id']}
                # The existing submit contract still verifies task identity and writes the receipt.
                module.submit_review(DAY, role, bundle['bundle_hash'],
                    response(module, bundle, 'reject' if role == self.reject else 'accept'),
                    issue['id'], team=runner.team, cli=self, environ=env)
            task['status'] = 'completed'
            issue['status'] = 'done'
            return issue
        if args[:2] == ['issue', 'get']:
            return self.issues[args[2]]
        if args[:2] == ['issue', 'runs']:
            task = self.runs.get(args[2])
            return [] if task is None or ('--active' in args and task['status'] in ('completed', 'failed', 'cancelled')) else [task]
        if args[:2] == ['issue', 'status']:
            self.issues[args[2]]['status'] = args[3]
            return self.issues[args[2]]
        if args[:2] == ['issue', 'cancel-task']:
            next(r for r in self.runs.values() if r['id'] == args[2])['status'] = 'cancelled'
            return {}
        raise AssertionError(args)


def test_default_is_read_only_and_invalid_source_never_dispatches(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    result = chain.run_chain(DAY, team=runner.team, cli=cli)
    assert result['status'] == 'planned' and not cli.created
    (runner.team / 'outputs' / DAY / '03_news.md').write_text('changed')
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked' and not cli.created


def test_six_roles_run_in_order_with_real_local_receipts_and_resume(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    module, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'accepted'
    assert cli.created == list(module.ROLES)
    assert result['execution_authorized'] is False
    assert len({r['task_id'] for r in result['roles'].values()}) == 6
    assert all(r['verdict'] == 'accept' for r in result['roles'].values())
    again = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert again['status'] == 'accepted' and len(cli.created) == 6


@pytest.mark.parametrize('failure', ['reject', 'missing_receipt', 'failed'])
def test_failed_or_unreceipted_role_stops_downstream(reviews, failure):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews, **{failure: 'market-analysts'})
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked'
    assert cli.created == ['data-quality', 'market-analysts']


def test_running_multica_task_cannot_start_another_chain(reviews, monkeypatch):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    monkeypatch.setenv('MULTICA_TASK_ID', str(uuid4()))
    with pytest.raises(ValueError, match='outside'):
        chain.run_chain(DAY, team=runner.team, cli=ChainCLI(reviews), execute=True)


def test_completed_run_with_wrong_model_is_blocked(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    original = cli._run_json
    def wrong_model(args, **kwargs):
        value = original(args, **kwargs)
        if args[:2] == ['issue', 'runs'] and '--active' not in args:
            for run in value:
                if run['status'] == 'completed':
                    run['usage'] = [{'model': 'different-model'}]
        return value
    cli._run_json = wrong_model
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked' and cli.created == ['data-quality']
    assert 'model mismatch' in result['error']


def test_existing_matching_issue_is_not_duplicated(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    module, runner, _, state = reviews
    cli = ChainCLI(reviews)
    bundle = module.prepare_review(DAY, 'data-quality', team=runner.team)
    cli.issues['existing'] = {'id': 'existing', 'title': module.issue_title(bundle),
                              'status': 'todo', 'project_id': state['project_id']}
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked' and not cli.created


def test_unrelated_active_task_prevents_dispatch(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    cli.issues['busy'] = {'id': 'busy', 'title': 'Unrelated business', 'status': 'in_progress'}
    cli.runs['busy'] = {'id': 'busy-run', 'status': 'running'}
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked' and not cli.created


def test_timeout_cancels_only_its_own_task(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews, missing_receipt='data-quality')
    original = cli._run_json
    def stalled(args, **kwargs):
        value = original(args, **kwargs)
        if args[:2] == ['issue', 'create']:
            cli.runs[value['id']]['status'] = 'running'
        return value
    cli._run_json = stalled
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True, timeout=0)
    assert result['status'] == 'blocked' and cli.created == ['data-quality']
    assert list(cli.runs.values())[0]['status'] == 'cancelled'


def test_source_change_after_a_role_never_dispatches_downstream(reviews):
    chain = importlib.import_module('trading_team.multica_review_chain')
    _, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    original = cli._run_json
    def changed(args, **kwargs):
        value = original(args, **kwargs)
        if args[:2] == ['issue', 'create']:
            (runner.team / 'outputs' / DAY / '03_news.md').write_text('changed after review')
        return value
    cli._run_json = changed
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'blocked' and cli.created == ['data-quality']


def test_transient_status_read_failure_does_not_repeat_dispatch(reviews, monkeypatch):
    chain = importlib.import_module('trading_team.multica_review_chain')
    module, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    original = cli._run_json
    failures = []
    def interrupted(args, **kwargs):
        if (args[:2] == ['issue', 'runs'] and '--active' not in args and not failures
                and cli.runs[args[2]]['status'] == 'completed'):
            failures.append(args[2])
            raise RuntimeError('Multica command failed: TLS handshake timed out')
        return original(args, **kwargs)
    cli._run_json = interrupted
    monkeypatch.setattr(chain.time, 'sleep', lambda _: None)
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'accepted' and cli.created == list(module.ROLES)
    assert len(result['roles']['data-quality']['poll_errors']) == 1


def test_persistent_status_read_failure_is_bounded_and_can_resume(reviews, monkeypatch):
    chain = importlib.import_module('trading_team.multica_review_chain')
    module, runner, _, _ = reviews
    cli = ChainCLI(reviews)
    original = cli._run_json
    failures = []
    def interrupted(args, **kwargs):
        if (args[:2] == ['issue', 'runs'] and '--active' not in args
                and cli.runs[args[2]]['status'] == 'completed'):
            failures.append(args[2])
            raise RuntimeError('Multica command failed: TLS handshake timed out')
        return original(args, **kwargs)
    cli._run_json = interrupted
    monkeypatch.setattr(chain.time, 'sleep', lambda _: None)
    with pytest.raises(RuntimeError, match='TLS handshake'):
        chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert len(failures) == 3 and cli.created == ['data-quality']
    cli._run_json = original
    result = chain.run_chain(DAY, team=runner.team, cli=cli, execute=True)
    assert result['status'] == 'accepted' and cli.created == list(module.ROLES)
