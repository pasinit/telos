import asyncio
from pathlib import Path
from unittest.mock import AsyncMock

import httpx
import pytest
from pontifex.codex import CodexRunConfig

from telos.services.nightly.worker import create_app
from telos.services.nightly.worker_client import WorkerSession
from tests.test_nightly_implementer import _FakeSession, _implementer, _proposal, _report

TOKEN = 'worker-test-token-' * 3


@pytest.fixture
def worker(tmp_path):
    root = tmp_path / 'jobs'
    root.mkdir()
    (root / 'nightly-impl-test').mkdir()
    app = create_app(token=TOKEN, workspace_root=root, outside_root=tmp_path)
    return app, root


async def test_auth_paths_and_schema(worker, tmp_path):
    app, root = worker
    (root / 'nightly-impl-link').symlink_to(tmp_path, target_is_directory=True)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://worker') as client:
        assert (await client.get('/healthz')).status_code == 200
        assert (await client.post('/preflight', json={'job_id': 'nightly-impl-test', 'provider': 'claude'})).status_code == 401
        client.headers['Authorization'] = f'Bearer {TOKEN}'
        for name in ['../outside', '/tmp', 'nightly-impl-missing', 'nightly-impl-link']:
            result = await client.post('/preflight', json={'job_id': name, 'provider': 'claude'})
            assert result.status_code in (400, 422)
        result = await client.post('/execute', json={**payload(), 'workspace_write': False})
        assert result.status_code == 422


def payload(provider='codex', timeout=30):
    return dict(job_id='nightly-impl-test', provider=provider, model='', system_prompt='system', prompt='task',
                max_turns=12, total_timeout_s=timeout)


@pytest.mark.parametrize('provider', ['codex', 'claude'])
async def test_session_preserves_provider_and_sandbox(worker, monkeypatch, provider):
    app, root = worker
    session = _FakeSession({'summary': 'done', 'prs': [], 'skipped': []})
    monkeypatch.setattr('telos.services.nightly.worker.CodexSession' if provider == 'codex' else 'telos.services.nightly.worker.AgentSession',
                        lambda: session)
    probe = AsyncMock(return_value=None)
    monkeypatch.setattr('telos.services.nightly.worker.check_codex_sandbox', probe)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://worker',
                                headers={'Authorization': f'Bearer {TOKEN}'}) as client:
        assert (await client.post('/preflight', json={'job_id': 'nightly-impl-test', 'provider': provider})).json() == {'error': None}
        result = await client.post('/execute', json=payload(provider))
    assert result.json()['structured_output']['summary'] == 'done'
    config = session.calls[0]['config']
    assert config.cwd == str(root / 'nightly-impl-test')
    assert config.extra_env['GIT_CONFIG_KEY_0'] == 'credential.https://github.com.helper'
    assert '$GITHUB_TOKEN' in config.extra_env['GIT_CONFIG_VALUE_0']
    if provider == 'codex':
        assert config.workspace_write and config.network_access
        assert config.max_tool_calls == 12
        probe.assert_awaited_once()
    else:
        assert 'Bash' in config.allowed_tools and config.max_turns == 12
        probe.assert_not_called()


async def test_cancel_stops_job_before_acknowledging_and_allows_next_job(worker, monkeypatch):
    app, _ = worker
    started, stopped = asyncio.Event(), asyncio.Event()

    class SlowSession:
        async def run(self, **kwargs):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr('telos.services.nightly.worker.CodexSession', SlowSession)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://worker',
                                headers={'Authorization': f'Bearer {TOKEN}'}) as client:
        task = asyncio.create_task(client.post('/execute', json=payload()))
        await asyncio.wait_for(started.wait(), 2)
        assert (await client.post('/execute', json=payload())).status_code == 409
        assert (await client.delete('/jobs/nightly-impl-test')).json() == {'stopped': True}
        assert stopped.is_set()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (await client.get('/healthz')).json()['busy'] is False
        monkeypatch.setattr('telos.services.nightly.worker.CodexSession', lambda: _FakeSession())
        assert (await client.post('/execute', json=payload())).status_code == 200


async def test_worker_exception_releases_slot(worker, monkeypatch):
    app, _ = worker
    session = _FakeSession()
    session.run = AsyncMock(side_effect=RuntimeError('broken'))
    monkeypatch.setattr('telos.services.nightly.worker.CodexSession', lambda: session)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app, raise_app_exceptions=False), base_url='http://worker',
                                headers={'Authorization': f'Bearer {TOKEN}'}) as client:
        assert (await client.post('/execute', json=payload())).status_code == 500
        assert (await client.get('/healthz')).json()['busy'] is False


def test_service_credential_not_in_child_environment(tmp_path, monkeypatch):
    monkeypatch.setenv('NIGHTLY_WORKER_TOKEN', TOKEN)
    create_app(workspace_root=tmp_path)
    import os
    assert 'NIGHTLY_WORKER_TOKEN' not in os.environ


async def test_implementer_uses_shared_clones_and_worker_not_local_session(tmp_path, monkeypatch):
    shared = tmp_path / 'shared'
    session = _FakeSession()
    impl = _implementer(tmp_path, session, provider='codex', worker_url='http://worker', worker_token=TOKEN, workspace_root=shared)
    monkeypatch.setattr('telos.services.nightly.implementer.check_codex_sandbox', AsyncMock(side_effect=AssertionError('local sandbox used')))
    monkeypatch.setattr(WorkerSession, 'preflight', AsyncMock(return_value=None))
    seen = []

    async def run(self, **kwargs):
        path = Path(kwargs['config'].cwd)
        assert path.parent == shared and (path / 'telos' / 'README.md').read_text() == 'hello\n'
        seen.append(path)
        return await _FakeSession({'summary': 'inspected code', 'prs': [], 'skipped': []}).run(**kwargs)

    monkeypatch.setattr(WorkerSession, 'run', run)
    result = await impl.run(_report(_proposal()))
    assert result.summary == 'inspected code' and not session.calls
    assert not seen[0].exists()


async def test_client_never_sends_github_credential_and_cancels_on_failure(monkeypatch):
    calls = []

    async def handler(request):
        calls.append(request)
        if request.method == 'DELETE':
            return httpx.Response(200, json={'stopped': True})
        return httpx.Response(503)

    real_client = httpx.AsyncClient
    monkeypatch.setattr('telos.services.nightly.worker_client.httpx.AsyncClient',
                        lambda **kw: real_client(**kw, transport=httpx.MockTransport(handler)))
    worker = WorkerSession('http://worker', TOKEN, 'codex')
    with pytest.raises(httpx.HTTPStatusError):
        await worker.run(config=CodexRunConfig(cwd='/nightly-work/nightly-impl-test', extra_env={'GITHUB_TOKEN': 'secret'}),
                         system_prompt='s', prompt='p')
    assert b'secret' not in calls[0].content
    assert calls[1].method == 'DELETE' and worker.cleanup_safe


async def test_worker_timeout_cancels_session_and_releases_slot(worker, monkeypatch):
    app, _ = worker
    stopped = asyncio.Event()

    class SlowSession:
        async def run(self, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                stopped.set()

    monkeypatch.setattr('telos.services.nightly.worker.CodexSession', SlowSession)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://worker',
                                headers={'Authorization': f'Bearer {TOKEN}'}) as client:
        response = await client.post('/execute', json=payload(timeout=0.01))
        assert response.json()['error'] == 'Coding worker timed out'
        assert stopped.is_set()
        assert not (await client.get('/healthz')).json()['busy']


async def test_unconfirmed_worker_stop_preserves_shared_workspace(tmp_path, monkeypatch):
    shared = tmp_path / 'shared'
    impl = _implementer(tmp_path, _FakeSession(), provider='codex', worker_url='http://worker', worker_token=TOKEN, workspace_root=shared)

    async def failure(self, workdir):
        self.cleanup_safe = False
        raise httpx.ConnectError('worker unreachable')

    monkeypatch.setattr(WorkerSession, 'preflight', failure)
    result = await impl.run(_report(_proposal()))
    assert 'worker unreachable' in result.error
    assert len(list(shared.iterdir())) == 1
    assert len(impl._report_store.list_reports()) == 1
