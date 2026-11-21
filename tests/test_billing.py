import io
from copy import deepcopy

from app.modules.billing.service import bill
from app.reasons import LABELS


def sheets(batch):
    return {row['label']: row for row in batch['billing']['sheets'] if row['label']}


def chain(batch, label):
    return next(row for row in batch['billing']['chains'] if row['voided'] == label)


def test_every_sheet_has_one_explained_disposition(batch):
    billing = batch['billing']
    assert billing['ok'] is True
    assert all(row['disposition'] in ('usage', 'allowance', 'deferred') for row in billing['sheets'])
    assert all(row['reason'] in LABELS for row in billing['sheets'])
    assert all(row['explain'] == LABELS[row['reason']] for row in billing['sheets'])


def test_usage_is_exactly_what_finance_pays_and_nothing_else(batch):
    totals = batch['billing']['totals']
    assert (totals['usage'], totals['finance_payable'], totals['carrier_billed'], totals['credit_claim']) == (15, 15, 33, 18)
    assert all(row['finance_pays'] is (row['disposition'] == 'usage') for row in batch['billing']['sheets'])
    assert all(row['carrier_counts'] is (row['disposition'] != 'deferred') for row in batch['billing']['sheets'])


def test_voided_sheet_is_allowance_not_usage_even_when_reprinted(batch):
    rows = sheets(batch)
    old, new = rows['L-001'], rows['L-001-R']
    assert (old['disposition'], old['void']['reason'], old['successor']) == ('allowance', 'JAM', 'L-001-R')
    assert (new['disposition'], new['predecessor'], new['successor']) == ('usage', 'L-001', None)
    assert old['parcel_state']['live_labels'] == ['L-001-R']


def test_broken_reprint_is_allowance_for_carrier_but_not_payable(batch):
    rows = sheets(batch)
    for label, code in [('L-002-A', 'REPRINT_NOT_VOID'), ('L-001-B', 'REPRINT_REUSED'), ('L-014-Q', 'REPRINT_PARCEL')]:
        assert rows[label]['codes'] == [code]
        assert (rows[label]['disposition'], rows[label]['reason']) == ('allowance', 'BILL_REPRINT_REJECTED')
        assert rows[label]['carrier_counts'] and not rows[label]['finance_pays']


def test_voided_without_reprint_leaves_no_live_sheet_and_stays_consistent(batch):
    row = chain(batch, 'L-015')
    assert (row['successor'], row['terminal'], row['parcel_state']['live_labels']) == (None, 'L-015', [])
    assert row['consistent'] is True and row['void']['reason'] == 'SMEAR'
    assert sheets(batch)['L-015']['disposition'] == 'allowance'


def test_conflicting_receipts_are_deferred_with_both_raw_rows(batch):
    row = sheets(batch)['L-024']
    assert (row['state'], row['disposition'], row['reason']) == ('conflict', 'deferred', 'BILL_CONFLICT')
    assert row['lines'] == [24, 36] and [item['line'] for item in row['receipts']] == [24, 36]
    assert 'dpi' in row['receipts'][0]['raw'] and not row['carrier_counts']


def test_duplicate_receipt_and_blank_line_never_count_twice(batch):
    totals = batch['billing']['totals']
    assert totals['accounted_rows'] + totals['duplicate_rows'] + totals['blank_rows'] == totals['input_rows']
    assert (totals['duplicate_rows'], totals['blank_rows']) == (1, 1)
    assert [row['line'] for row in sheets(batch)['L-025']['duplicates']] == [35]
    kinds = {(row['line'], row['kind']) for row in batch['billing']['ignored']}
    assert kinds == {(35, 'duplicate_receipt'), (39, 'blank')}


def test_original_receipts_are_kept_verbatim(batch):
    source_rows = {row['line']: row['raw'] for row in batch['intake']['accepted']}
    for item in batch['billing']['sheets']:
        for receipt in item['receipts']:
            if receipt['line'] in source_rows:
                assert receipt['raw'] == source_rows[receipt['line']]
    receipt = chain(batch, 'L-001')['voided_receipts'][0]
    assert receipt['line'] == 1 and receipt['raw'].startswith('{"label":"L-001"')


def test_successor_that_fails_qc_is_flagged_against_the_voided_sheet(batch):
    ledger = deepcopy(batch['tickets'])
    target = next(row for row in ledger['labels'] if row['label'] == 'L-001-R')
    target['state'] = 'isolated'
    target['codes'] = ['FIELD_OVERLAP']
    result = bill(batch['intake'], ledger, batch['reconcile'])
    row = chain({'billing': result}, 'L-001')
    assert row['issues'] == ['SUCCESSOR_NOT_SHIPPABLE'] and row['consistent'] is False
    assert result['checks'][-1]['ok'] is False


def test_revoked_sheet_with_extra_live_sheet_is_flagged(batch):
    ledger = deepcopy(batch['tickets'])
    target = next(row for row in ledger['labels'] if row['label'] == 'L-015')
    other = next(row for row in ledger['labels'] if row['label'] == 'L-001-R')
    other['parcel'] = '0015'
    other['replaces'] = None
    reconcile = deepcopy(batch['reconcile'])
    for row in reconcile['parcels']:
        if row['parcel'] == '0015':
            row['live_labels'] = ['L-001-R']
    result = bill(batch['intake'], ledger, reconcile)
    row = chain({'billing': result}, 'L-015')
    assert row['issues'] == ['PARCEL_HAS_OTHER_LIVE'] and row['consistent'] is False


def test_late_duplicate_receipt_rerun_does_not_accumulate(client, batch_id, sources):
    base = client.get('/api/billing').json['data']['totals']
    print_row = next(row for row in sources['prints'].decode('utf-8-sig').splitlines() if '"L-025"' in row)
    appended = sources['prints'].rstrip(b'\n') + b'\n' + print_row.encode() + b'\n'
    first = client.post('/api/intake', data={'expected': batch_id, 'prints': (io.BytesIO(appended), 'prints.jsonl')})
    assert first.status_code == 200
    after = client.get('/api/billing').json['data']['totals']
    assert (after['usage'], after['carrier_billed'], after['finance_payable']) == (base['usage'], base['carrier_billed'], base['finance_payable'])
    assert after['duplicate_rows'] == base['duplicate_rows'] + 1
    again = client.post('/api/intake', data={'expected': first.json['id'], 'prints': (io.BytesIO(appended), 'prints.jsonl')})
    assert again.json == {'id': first.json['id'], 'created': False, 'changed': False}
    assert client.get('/api/billing').json['data']['totals'] == after


def test_rerun_repeats_billing_without_double_counting(client, batch_id):
    before = client.get('/api/billing').json['data']
    assert client.post('/api/runs/rerun', json={'expected': batch_id}).json == {'id': batch_id, 'created': False, 'changed': False}
    assert client.get('/api/billing').json['data'] == before
    assert len(client.get('/api/runs').json['history']) == 1


def test_failed_generation_keeps_previous_statement_and_receipts(client, batch_id):
    before = client.get(f'/api/runs/{batch_id}/evidence.json').data
    totals = client.get('/api/billing').json['data']['totals']
    response = client.post('/api/intake', data={'expected': batch_id, 'prints': (io.BytesIO(b''), 'empty.jsonl')})
    assert (response.status_code, response.json['code']) == (422, 'EMPTY_INPUT')
    assert client.get('/health').json['run'] == batch_id
    assert client.get(f'/api/runs/{batch_id}/evidence.json').data == before
    assert client.get('/api/billing').json['data']['totals'] == totals


def test_csv_statement_lists_each_sheet_and_totals(client, batch_id):
    body = client.get('/api/billing/statement.csv').get_data(as_text=True)
    assert 'attachment' in client.get('/api/billing/statement.csv').headers['Content-Disposition']
    lines = [line for line in body.splitlines() if line.strip()]
    assert len(lines) == 1 + len(client.get('/api/billing').json['data']['sheets']) + 1
    assert any('L-024' in line and '暂缓' in line for line in lines)
    assert any(line.startswith('合计') and '承运商 33' in line and '应付 15' in line for line in lines)


def test_billing_is_an_eighth_entry_point(client, batch_id):
    assert '费用对账' in client.get('/').get_data(as_text=True)
    response = client.get(f'/api/billing?run={batch_id}')
    assert response.status_code == 200 and response.json['id'] == batch_id


def test_void_then_reprint_moves_one_sheet_from_usage_to_allowance(client, batch_id):
    before = client.get('/api/billing').json['data']
    client.post('/api/tickets/void', json={'expected': batch_id, 'label': 'L-002',
                                           'at': '2026-11-20T08:30:00+08:00', 'reason': 'LOST'})
    after_void = client.get('/api/billing').json['data']
    assert after_void['totals']['usage'] == before['totals']['usage'] - 1
    assert after_void['totals']['allowance'] == before['totals']['allowance'] + 1
    adjusted = client.get('/api/runs').json['manifest']['id']
    client.post('/api/tickets/reprint', json={'expected': adjusted, 'target': 'L-002',
                                              'label': 'L-002-NEW', 'at': '2026-11-20T08:35:00+08:00'})
    final = client.get('/api/billing').json['data']
    assert final['totals']['usage'] == before['totals']['usage']
    row = next(item for item in final['sheets'] if item['label'] == 'L-002')
    successor = next(item for item in final['sheets'] if item['label'] == 'L-002-NEW')
    assert (row['disposition'], row['successor']) == ('allowance', 'L-002-NEW')
    assert (successor['disposition'], successor['predecessor']) == ('usage', 'L-002')
    assert all(chain['consistent'] for chain in final['chains'])
    assert final['ok'] is True
