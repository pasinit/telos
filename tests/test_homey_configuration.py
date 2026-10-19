import json

import pytest
from scripts.configure_homey import configure, fingerprint, load_state

from telos.core.persistence import Database
from telos.core.repos.registry import RegistryRepo
from telos.core.repos.routines import RoutineRepo


@pytest.fixture
def saved(tmp_path):
    registry = RegistryRepo(Database(tmp_path / 'registry.db'))
    registry.create('u', 'Homey', 'Property agent', 'old', ['browser_open'])
    routine = RoutineRepo(Database(tmp_path / 'routines.db')).create(
        'u', 'homey', 'Search and send to owner@example.com. STILL TRACKING old IDs',
        interval_seconds=86400, time_of_day='08:00', notify_email=True)
    prompts = tmp_path / 'u/homey/prompts'
    prompts.mkdir(parents=True)
    (prompts / 'current.txt').write_text('old')
    return tmp_path, routine.id


def test_config_update_preserves_ownership_schedule_and_original_tools(saved):
    root, routine_id = saved
    before = load_state(root, 'u', routine_id)
    preview = configure(root, 'u', routine_id, 'new', 'new routine', 'owner@example.com')
    assert load_state(root, 'u', routine_id) == before
    backup = root / 'backup.json'
    result = configure(root, 'u', routine_id, 'new', 'new routine', 'owner@example.com', apply=True,
        service_stopped=True, expected_state=preview['state_sha256'], backup=backup)
    assert result['applied']
    assert json.loads(backup.read_text()) == before
    assert backup.stat().st_mode & 0o777 == 0o600
    after = load_state(root, 'u', routine_id)
    assert after['current_prompt'] == 'new'
    assert json.loads(after['agent']['tools']) == ['browser_open', 'record_property_assessment', 'list_property_candidates']
    assert after['routine']['time_of_day'] == before['routine']['time_of_day']
    assert after['routine']['last_run'] == before['routine']['last_run']
    assert 'STILL TRACKING' not in after['routine']['description']
    assert 'owner@example.com' in after['routine']['description']


def test_config_update_refuses_stale_state_recipient_changes_and_other_users(saved):
    root, routine_id = saved
    before = load_state(root, 'u', routine_id)
    with pytest.raises(ValueError, match='dry-run'):
        configure(root, 'u', routine_id, 'new', 'new routine', 'owner@example.com', apply=True,
            service_stopped=True, expected_state='stale', backup=root / 'backup.json')
    with pytest.raises(ValueError, match='Recipient'):
        configure(root, 'u', routine_id, 'new', 'new routine', 'other@example.com')
    with pytest.raises(ValueError, match='owner'):
        configure(root, 'other', routine_id, 'new', 'new routine', 'owner@example.com')
    assert fingerprint(load_state(root, 'u', routine_id)) == fingerprint(before)
    assert not (root / 'backup.json').exists()


def test_config_update_refuses_pending_candidate(saved):
    root, routine_id = saved
    (root / 'u/homey/prompts/candidate.txt').write_text('pending unrelated work')
    with pytest.raises(ValueError, match='pending'):
        configure(root, 'u', routine_id, 'new', 'new routine', 'owner@example.com')
