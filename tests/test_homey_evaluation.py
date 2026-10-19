from scripts.evaluate_homey import Decision, Decisions, score


def test_evaluation_requires_exactly_once_coverage_and_catches_false_matches():
    cases = [{'id': 'unknown', 'expected': 'needs_verification'}, {'id': 'match', 'expected': 'verified'}]
    result = Decisions(decisions=[Decision(id='unknown', disposition='verified', missing_evidence=[], reason='bad')])
    graded = score(cases, result)
    assert not graded['complete']
    assert graded['false_matches'] == ['unknown']
    assert graded['missed_matches'] == ['match']
    repeated = Decisions(decisions=[result.decisions[0], result.decisions[0]])
    assert not score(cases, repeated)['complete']
