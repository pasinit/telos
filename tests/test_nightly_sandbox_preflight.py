import asyncio
import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from telos.services.nightly.sandbox_preflight import check_codex_sandbox
from tests.test_nightly_implementer import _FakeSession, _implementer, _proposal, _report


async def test_blocked_preflight_skips_every_proposal_without_cloning_or_model(tmp_path, monkeypatch):
    check = AsyncMock(return_value='bwrap: Failed to make / slave: Permission denied')
    monkeypatch.setattr('telos.services.nightly.implementer.check_codex_sandbox', check)
    session = _FakeSession()
    impl = _implementer(tmp_path, session, provider='codex')
    monkeypatch.setattr(impl, '_prepare_clones', lambda *a: pytest.fail('must not clone before preflight succeeds'))
    result = await impl.run(_report(_proposal(title='one'), _proposal(title='two')))
    assert not session.calls and result.cost_usd == 0 and not result.prs
    assert [s['title'] for s in result.skipped] == ['one', 'two']
    assert 'Infrastructure blocked' in result.summary
    row = impl._report_store.list_reports()[0]
    assert row['error'] == check.return_value
    assert len(json.loads(row['skipped_json'])) == 2
    assert not Path(check.call_args.args[0]).exists()


async def test_successful_preflight_preserves_codex_session_configuration(tmp_path, monkeypatch):
    monkeypatch.setattr('telos.services.nightly.implementer.check_codex_sandbox', AsyncMock(return_value=None))
    session = _FakeSession({'summary': 'done', 'prs': [], 'skipped': []})
    impl = _implementer(tmp_path, session, provider='codex')
    await impl.run(_report(_proposal()))
    assert len(session.calls) == 1
    config = session.calls[0]['config']
    assert config.workspace_write and config.network_access
    assert config.extra_env == {'GITHUB_TOKEN': 'tok'}


async def test_claude_does_not_use_codex_preflight(tmp_path, monkeypatch):
    monkeypatch.setattr('telos.services.nightly.implementer.check_codex_sandbox', AsyncMock(side_effect=AssertionError))
    session = _FakeSession()
    await _implementer(tmp_path, session, provider='claude').run(_report(_proposal()))
    assert len(session.calls) == 1



class ProbeClient:
    outside_write = False
    should_timeout = False
    disconnected = False

    def __init__(self, **kwargs):
        assert kwargs['extra_env']['GITHUB_TOKEN'] == ''

    async def connect(self):
        pass

    async def request(self, method, params):
        assert method == 'command/exec'
        assert params['sandboxPolicy']['type'] == 'workspaceWrite'
        if self.should_timeout:
            await asyncio.sleep(60)
        cwd = Path(params['cwd'])
        assert params['sandboxPolicy']['writableRoots'] == [str(cwd.parent)]
        (cwd / '.git' / 'refs' / 'heads').mkdir(parents=True)
        (cwd / 'probe.txt').write_text('sandbox probe')
        if self.outside_write:
            Path(params['command'][-1]).write_text('forbidden')
        return {'exitCode': 0, 'stdout': 'TELOS_SANDBOX_PREFLIGHT_OK', 'stderr': ''}

    async def disconnect(self):
        type(self).disconnected = True


def setup_probe(tmp_path, monkeypatch):
    home = tmp_path / 'home'
    home.mkdir()
    monkeypatch.setattr(Path, 'home', lambda: home)
    monkeypatch.setattr('telos.services.nightly.sandbox_preflight.CodexClient', ProbeClient)
    monkeypatch.setattr(ProbeClient, 'disconnected', False)
    return home


@pytest.mark.parametrize('outside_write', [False, True])
async def test_probe_checks_artifacts_and_outside_canary(tmp_path, monkeypatch, outside_write):
    home = setup_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(ProbeClient, 'outside_write', outside_write)
    result = await check_codex_sandbox(tmp_path)
    assert ('outside-workspace' in result) if outside_write else result is None
    assert ProbeClient.disconnected
    assert list(home.iterdir()) == [] and not list(tmp_path.glob('.sandbox-probe-*'))


@pytest.mark.parametrize('cancel', [False, True])
async def test_timeout_and_cancellation_disconnect_before_cleanup(tmp_path, monkeypatch, cancel):
    home = setup_probe(tmp_path, monkeypatch)
    monkeypatch.setattr(ProbeClient, 'should_timeout', True)
    if cancel:
        task = asyncio.create_task(check_codex_sandbox(tmp_path))
        await asyncio.sleep(0.01)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
    else:
        result = await check_codex_sandbox(tmp_path, timeout_s=0.01)
        assert 'timed out' in result
    assert ProbeClient.disconnected
    assert not list(tmp_path.glob('.sandbox-probe-*')) and list(home.iterdir()) == []
