import json

from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from pydantic import ValidationError

from telos.core.delegation import DelegationGraph
from telos.core.persistence import Database
from telos.core.repos.property_candidates import PropertyCandidateRepo
from telos.core.run_context import RunContext
from telos.core.tool_context import ToolContext
from telos.domain.tools.property_candidates import REQUIRED_CRITERIA, PropertyAssessmentInput, RecordPropertyAssessmentTool
from telos.services.routine_execution import RoutineExecutor


def assessment(disposition='needs_verification'):
    return {'url': 'https://example.com/property/1', 'address': 'Example Street', 'disposition': disposition,
            'criteria': {'size': {'status': 'unknown', 'evidence': 'Floorplan unreadable'}},
            'reason': 'Size unconfirmed', 'next_action': 'Read floorplan asset'}


def test_unknowns_cannot_be_promoted_or_rejected():
    for disposition in ['verified', 'rejected']:
        with pytest.raises(ValidationError):
            PropertyAssessmentInput.model_validate(assessment(disposition))
    PropertyAssessmentInput.model_validate(assessment())


def test_verified_requires_dated_current_evidence_for_every_criterion():
    now = datetime.now(UTC).isoformat()
    data = {**assessment('verified'), 'price_pcm': 3300, 'size_sqm': 72, 'floor': 'first',
            'criteria': {key: {'status': 'pass', 'evidence': 'Observed on current detail page',
                              'source_url': 'https://example.com/property/1', 'observed_at': now}
                         for key in REQUIRED_CRITERIA}}
    PropertyAssessmentInput.model_validate(data)
    data['criteria']['availability']['observed_at'] = (datetime.now(UTC) - timedelta(days=3)).isoformat()
    with pytest.raises(ValidationError, match='past day'):
        PropertyAssessmentInput.model_validate(data)


def test_unresolved_leads_remain_retainable_after_two_days(tmp_path):
    db = Database(tmp_path / 'history.db')
    repo = PropertyCandidateRepo(db)
    start = datetime(2026, 9, 22, 7, tzinfo=UTC)
    initial = repo.record('url', {**assessment(), 'first_seen_at': start.isoformat(), 'unresolved_since': start.isoformat()}, now=start)
    later = repo.record('url', assessment(), now=start + timedelta(days=3))
    assert initial['first_seen'] == later['first_seen']
    assert 'verification_deadline' not in initial
    assert 'verification_deadline' not in later
    assert later['unresolved_since'] == start.isoformat()
    reloaded = PropertyCandidateRepo(db)
    assert len(reloaded.history('url')) == 2
    assert reloaded.page()['records'][0]['disposition'] == 'needs_verification'


def test_missing_history_does_not_start_a_new_verification_clock(tmp_path):
    repo = PropertyCandidateRepo(Database(tmp_path / 'history.db'))
    saved = repo.record('url', assessment())
    assert saved['first_seen'] is None
    assert saved['unresolved_since'] is None
    assert 'verification_deadline' not in saved
    assert saved['first_recorded_at']


def test_applied_exclusions_cannot_be_overwritten(tmp_path):
    repo = PropertyCandidateRepo(Database(tmp_path / 'history.db'))
    repo.record('url', assessment('applied'))
    with pytest.raises(ValueError, match='remain excluded'):
        repo.record('url', assessment())
    assert len(repo.history('url')) == 1


async def test_candidate_tools_isolate_users_and_agents(tmp_path):
    ctx = ToolContext(RunContext('one', DelegationGraph()), 'homey', tmp_path, tmp_path)
    result = await RecordPropertyAssessmentTool()(PropertyAssessmentInput.model_validate(assessment()), ctx)
    assert not result.is_error
    assert PropertyCandidateRepo(Database(tmp_path / 'one/homey/history.db')).page()['total'] == 1
    assert PropertyCandidateRepo(Database(tmp_path / 'two/homey/history.db')).page()['total'] == 0
    assert PropertyCandidateRepo(Database(tmp_path / 'one/other/history.db')).page()['total'] == 0


async def test_routine_injects_candidate_context_even_after_short_final(tmp_path):
    repo = PropertyCandidateRepo(Database(tmp_path / 'u/homey/history.db'))
    repo.record('https://example.com/property/1', assessment())
    messages = []

    async def stream(message, ctx):
        messages.append(message)
        yield 'text', 'No confirmed matches today'

    runtime = SimpleNamespace(get=AsyncMock(return_value=SimpleNamespace(astream=stream)), make_context=MagicMock())
    executor = RoutineExecutor(runtime, MagicMock(), lambda u: '', lambda u, a: a, '', storage_root=tmp_path)
    assert await executor.execute('u', 'homey', 'search') == 'No confirmed matches today'
    assert 'https://example.com/property/1' in messages[0]
    assert 'verification_deadline' not in messages[0]
    assert 'criterion_statuses' in messages[0]
    assert repo.page()['records'][0]['criteria']['size']['evidence'] == 'Floorplan unreadable'


def test_legacy_deadlines_are_history_not_current_policy(tmp_path):
    db = Database(tmp_path / 'history.db')
    repo = PropertyCandidateRepo(db)
    legacy = {**assessment('rejected'), 'verification_deadline': '2026-09-24T07:00:00+00:00',
              'unresolved_since': '2026-09-22T07:00:00+00:00'}
    with db.connect() as conn:
        conn.execute("INSERT INTO property_assessments(url,recorded_at,payload_json) VALUES (?,?,?)",
                     ('url', '2026-09-22T07:00:00+00:00', json.dumps(legacy)))
    for projection in (repo.page(), repo.summary()):
        record = projection['records'][0]
        assert 'verification_deadline' not in record
        assert record['disposition'] == 'rejected'
        assert record['unresolved_since'] == legacy['unresolved_since']
    assert repo.history('url') == [legacy]
    saved = repo.record('url', legacy)
    assert 'verification_deadline' not in saved
    assert saved['disposition'] == 'rejected'
    assert repo.history('url')[1] == legacy
