"""Independent score-only import regressions; synthetic profile and job sources."""

import json
import sqlite3
from concurrent.futures import ThreadPoolExecutor
from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_local_handoff import job, write_json
from test_local_handoff import local as local_fixture

from applypilot import local_handoff as handoff
from applypilot import local_score as score

local = local_fixture


def bundle(local, value=6):
    path = score.export_job(local.url)
    folder = path.parent
    snapshot = handoff.read(path)
    assessment = {'text': 'Fixture and CAD experience supports a transferable match.',
                  'evidence': [{'source': 'resume', 'quote': local.resume['summary']},
                               {'source': 'job', 'quote': 'Design fixtures using CAD.'}]}
    result = {'version': 1, 'kind': 'score_only', 'source': handoff.SOURCE,
              'author': 'independent-test-author', 'handoff_sha256': handoff.digest(path),
              'job_url': local.url, 'score': {'value': value, 'reasoning': assessment},
              'strengths': [], 'gaps': [], 'eligibility': score.eligibility_summary(
                  snapshot['job'], handoff.read(folder/'profile.json'), handoff.read(folder/'searches.yaml'))}
    result_path = folder / 'score-result.json'
    write_json(result_path, result)
    review = {'version': 1, 'kind': 'score_only', 'source': handoff.SOURCE,
              'reviewer': 'independent-test-reviewer', 'handoff_sha256': handoff.digest(path),
              'result_sha256': handoff.digest(result_path), 'semantic_assessments_checked': True,
              'notes': 'Synthetic source confirms fixture overlap; score is not eligibility.',
              'assessments': [{'assessment_sha256': handoff.object_digest(assessment),
                               'verdict': 'supported', 'notes': 'Both source statements support CAD fixture overlap.'}]}
    review_path = folder / 'score-review.json'
    write_json(review_path, review)
    return SimpleNamespace(path=path, folder=folder, result=result, result_path=result_path,
                           review=review, review_path=review_path)


def import_bundle(b):
    return score.import_result(b.path, b.result_path, b.review_path)


def reject_clean(local, b):
    before = list(local.conn.iterdump())
    with pytest.raises(ValueError):
        import_bundle(b)
    assert list(local.conn.iterdump()) == before
    assert not list(b.folder.glob('*.manifest.json'))


def test_import_changes_only_score_fields_no_documents_or_attempts(local):
    b = bundle(local)
    before = job(local)
    attempts = list(local.conn.execute('SELECT * FROM application_attempts'))
    receipt = import_bundle(b)
    after = job(local)
    assert {k for k in before if before[k] != after[k]} == {'fit_score', 'score_reasoning', 'scored_at'}
    assert after['fit_score'] == 6
    assert list(local.conn.execute('SELECT * FROM application_attempts')) == attempts
    assert receipt['documents_generated'] is False and receipt['submitted'] is False
    assert json.loads(after['score_reasoning'])['eligibility']['decision'] == 'not_assessed'
    assert not list(b.folder.glob('*tailored*')) and not list(b.folder.glob('cover*'))
    reject_clean(local, b)


@pytest.mark.parametrize('field,value', [('version', True), ('version', 2), ('kind', 'documents'),
    ('source', 'model_api'), ('job_url', 'other'), ('author', ''), ('handoff_sha256', '0'*64),
    ('strengths', {}), ('gaps', None), ('submitted', True)])
def test_malformed_result_rejects_even_with_rehashed_review(local, field, value):
    b = bundle(local)
    b.result[field] = value
    write_json(b.result_path, b.result)
    b.review['result_sha256'] = handoff.digest(b.result_path)
    write_json(b.review_path, b.review)
    reject_clean(local, b)


@pytest.mark.parametrize('value', [True, 0, 11, '6', 6.0, None])
def test_score_requires_real_integer_in_range(local, value):
    b = bundle(local, value)
    reject_clean(local, b)


@pytest.mark.parametrize('mutation', ['wrong_quote', 'job_only', 'unknown_source', 'no_evidence', 'duplicate'])
def test_evidence_failures_are_checked_before_review_hash(local, mutation):
    b = bundle(local)
    assessment = b.result['score']['reasoning']
    if mutation == 'wrong_quote':
        assessment['evidence'][0]['quote'] = 'Invented aerospace accomplishment'
    elif mutation == 'job_only':
        assessment['evidence'] = assessment['evidence'][1:]
    elif mutation == 'unknown_source':
        assessment['evidence'][0]['source'] = 'internet'
    elif mutation == 'no_evidence':
        assessment['evidence'] = []
    else:
        b.result['strengths'] = [deepcopy(assessment)]
    write_json(b.result_path, b.result)
    with pytest.raises(ValueError):
        score.validate_result(b.path, b.result_path)
    reject_clean(local, b)


@pytest.mark.parametrize('field,value', [('reviewer', ' Independent-Test-Author '), ('reviewer', ''),
    ('semantic_assessments_checked', 'true'), ('semantic_assessments_checked', False),
    ('assessments', []), ('result_sha256', '0'*64), ('handoff_sha256', '0'*64), ('submitted', True)])
def test_review_cannot_be_missing_stale_or_self_attested(local, field, value):
    b = bundle(local)
    b.review[field] = value
    write_json(b.review_path, b.review)
    reject_clean(local, b)


@pytest.mark.parametrize('mutation', ['eligible', 'omit_unknown', 'reorder_unknown'])
def test_score_cannot_claim_unknown_eligibility(local, mutation):
    b = bundle(local)
    if mutation == 'eligible':
        b.result['eligibility']['decision'] = 'eligible'
    elif mutation == 'omit_unknown':
        b.result['eligibility']['unknowns'].pop()
    else:
        b.result['eligibility']['unknowns'].reverse()
    write_json(b.result_path, b.result)
    with pytest.raises(ValueError):
        score.validate_result(b.path, b.result_path)
    reject_clean(local, b)


@pytest.mark.parametrize('field,value', [('full_description', 'Changed posting'), ('claim_token', 'worker'),
    ('apply_status', 'applied'), ('verification_state', 'submitted_but_unverified'), ('fit_score', 4)])
def test_stale_or_active_job_rejects_score_update(local, field, value):
    b = bundle(local)
    local.conn.execute(f'UPDATE jobs SET {field}=? WHERE url=?', (value, local.url))
    local.conn.commit()
    reject_clean(local, b)


def test_sql_failure_rolls_back_score_and_receipt(local):
    b = bundle(local)
    local.conn.execute("CREATE TRIGGER block_receipt BEFORE UPDATE ON local_preparation_handoffs "
                       "BEGIN SELECT RAISE(ABORT, 'synthetic crash'); END")
    before = list(local.conn.iterdump())
    with pytest.raises(sqlite3.DatabaseError):
        import_bundle(b)
    assert list(local.conn.iterdump()) == before


def test_two_simultaneous_imports_only_one_consumes(local):
    b = bundle(local)
    def worker():
        try:
            import_bundle(b)
            return True
        except ValueError:
            return False
    with ThreadPoolExecutor(max_workers=2) as pool:
        outcomes = list(pool.map(lambda _: worker(), range(2)))
    assert outcomes.count(True) == 1


def test_score_receipt_cannot_be_used_for_document_import(local):
    b = bundle(local)
    import_bundle(b)
    with pytest.raises(ValueError, match='replay'):
        handoff.load_current(b.path)
