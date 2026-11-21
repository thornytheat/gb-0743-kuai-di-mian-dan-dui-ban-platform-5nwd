from collections import Counter, defaultdict

from app.modules.intake.contract import valid_code
from app.reasons import LABELS

USAGE = 'usage'
ALLOWANCE = 'allowance'
DEFERRED = 'deferred'


def _label_of(row):
    data = row.get('data')
    if isinstance(data, dict) and valid_code(data.get('label')):
        return data['label']
    return None


def _parcel_of(row):
    data = row.get('data')
    if isinstance(data, dict) and valid_code(data.get('parcel')):
        return data['parcel']
    return None


def _receipt(row):
    return {'line': row['line'], 'raw': row['raw'], 'code': row.get('code')}


def _void(ticket):
    if not ticket.get('void'):
        return None
    void = ticket['void']
    return {'id': void['id'], 'at': void['at'], 'reason': void['reason'],
            'line': void['line'], 'raw': void['raw']}


def _parcel_state(parcel, parcels):
    row = parcels.get(parcel) or {}
    live = list(row.get('live_labels') or [])
    return {'parcel': parcel, 'live_labels': live, 'shippable': bool(live),
            'valid': row.get('valid', 0), 'revoked': row.get('revoked', 0),
            'isolated': row.get('isolated', 0)}


def _successor_chain(label, ledger):
    """沿后继走到最后一张有效面单，同时保留每一段接续。"""
    steps, seen, current, expected = [], {label}, label, None
    while True:
        ticket = ledger.get(current)
        if ticket is None:
            return steps, None, 'SUCCESSOR_MISSING'
        if ticket['label'] in seen and ticket['label'] != label:
            return steps, None, 'SUCCESSOR_LOOP'
        seen.add(ticket['label'])
        successor = ticket['replaced_by']
        if not successor:
            return steps, ticket, None
        target = ledger.get(successor)
        expected = ticket['label']
        steps.append({'from': expected, 'to': successor,
                      'state': target['state'] if target else None,
                      'linked': bool(target and target['replaces'] == expected)})
        if target is None:
            return steps, None, 'SUCCESSOR_MISSING'
        if target['replaces'] != expected:
            return steps, target, 'SUCCESSOR_NOT_LINKED'
        if successor in seen:
            return steps, None, 'SUCCESSOR_LOOP'
        current = successor


def _ticket_issues(ticket, ledger, parcels):
    """作废前后的纸单与包裹当前可发面单必须对得上。"""
    label = ticket['label']
    live = _parcel_state(ticket['parcel'], parcels)['live_labels']
    if ticket['state'] == 'valid':
        return [] if label in live else ['PARCEL_LIVE_MISMATCH']
    if ticket['state'] != 'revoked':
        return []
    if not ticket['replaced_by']:
        return ['PARCEL_HAS_OTHER_LIVE'] if live else []
    steps, terminal, failure = _successor_chain(label, ledger)
    if failure:
        return [failure]
    if terminal is None:
        return ['SUCCESSOR_MISSING']
    if terminal['state'] != 'valid':
        return ['SUCCESSOR_NOT_SHIPPABLE']
    if live != [terminal['label']]:
        return ['PARCEL_LIVE_MISMATCH']
    return []


def _base(key, label, parcel, state, disposition, reason, rows, parcels):
    first = rows[0]
    data = first.get('data') if isinstance(first.get('data'), dict) else {}
    at = data.get('at') if isinstance(data.get('at'), str) else None
    return {
        'key': key, 'label': label, 'parcel': parcel,
        'lines': [row['line'] for row in rows], 'accounted_rows': len(rows), 'at': at,
        'state': state, 'disposition': disposition, 'reason': reason,
        'carrier_counts': disposition in (USAGE, ALLOWANCE),
        'finance_pays': disposition == USAGE,
        'claim_credit': disposition == ALLOWANCE,
        'codes': [], 'receipts': [_receipt(row) for row in rows], 'duplicates': [],
        'void': None, 'predecessor': None, 'successor': None,
        'parcel_state': _parcel_state(parcel, parcels),
        'issues': [], 'explain': LABELS[reason],
    }


def _ticket_item(base, ticket, ledger, parcels):
    state = ticket['state']
    if state == 'valid':
        disposition, reason = USAGE, 'BILL_USAGE'
    elif state == 'revoked':
        disposition, reason = ALLOWANCE, 'BILL_VOIDED'
    elif state == 'isolated':
        disposition, reason = ALLOWANCE, 'BILL_REPRINT_REJECTED' if ticket['replaces'] else 'BILL_REJECTED'
    else:
        disposition, reason = DEFERRED, 'BILL_UNREADABLE'
    item = _base(f'label:{ticket["label"]}', ticket['label'], ticket['parcel'], state,
                 disposition, reason, [base], parcels)
    item.update(codes=list(ticket['codes']), void=_void(ticket),
                predecessor=ticket['replaces'], successor=ticket['replaced_by'],
                issues=_ticket_issues(ticket, ledger, parcels))
    return item


def _pending_item(key, label, parcel, rows, state, reason, codes, parcels):
    return _base(key, label, parcel, state, DEFERRED, reason, rows, parcels) | {'codes': list(codes)}


def _sheets(intake, ledger, parcels):
    groups = defaultdict(list)
    for row in intake['accepted']:
        groups[_label_of(row)].append(('accepted', row))
    unlabeled = []
    for row in intake['excluded']:
        label = _label_of(row)
        if label:
            groups[label].append(('excluded', row))
        else:
            unlabeled.append(row)

    items, ignored = [], []
    for label, entries in groups.items():
        accepted = [row for kind, row in entries if kind == 'accepted']
        excluded = [row for kind, row in entries if kind == 'excluded']
        if accepted:
            item = _ticket_item(accepted[0], ledger[label], ledger, parcels)
            for row in excluded:
                if row['code'] == 'DUPLICATE_PRINT':
                    item['duplicates'].append(_receipt(row))
                    ignored.append(dict(_receipt(row), kind='duplicate_receipt', label=label, sheet=item['key']))
                else:
                    items.append(_pending_item(f'line:{row["line"]}', label, _parcel_of(row), [row],
                                               'unreadable', 'BILL_UNREADABLE', [row['code']], parcels))
            items.append(item)
        else:
            conflicts = [row for row in excluded if row['code'] == 'PRINT_CONFLICT']
            if conflicts:
                items.append(_pending_item(f'label:{label}', label, _parcel_of(conflicts[0]), conflicts,
                                           'conflict', 'BILL_CONFLICT', ['PRINT_CONFLICT'], parcels))
            else:
                for row in excluded:
                    items.append(_pending_item(f'line:{row["line"]}', label, _parcel_of(row), [row],
                                               'unreadable', 'BILL_UNREADABLE', [row['code']], parcels))
    for row in unlabeled:
        if row['code'] == 'BLANK_LINE':
            ignored.append(dict(_receipt(row), kind='blank', label=None, sheet=None))
        else:
            items.append(_pending_item(f'line:{row["line"]}', None, None, [row],
                                       'unreadable', 'BILL_UNREADABLE', [row['code']], parcels))
    items.sort(key=lambda item: (item['at'] or '9999', item['label'] or '', item['lines'][0]))
    ignored.sort(key=lambda row: row['line'])
    return items, ignored


def _chain(label, ticket, ledger, parcels, item_by_label):
    steps, terminal, failure = _successor_chain(label, ledger)
    for step in steps:
        step['from_receipts'] = item_by_label[step['from']]['receipts'] if step['from'] in item_by_label else []
        step['to_receipts'] = item_by_label[step['to']]['receipts'] if step['to'] in item_by_label else []
        step['to_codes'] = list(ledger[step['to']]['codes']) if step['to'] in ledger else []
    predecessor = ticket['replaces']
    issues = _ticket_issues(ticket, ledger, parcels)
    return {
        'voided': label, 'parcel': ticket['parcel'], 'voided_state': ticket['state'],
        'void': _void(ticket),
        'voided_receipts': item_by_label[label]['receipts'] if label in item_by_label else [],
        'predecessor': predecessor,
        'predecessor_receipts': item_by_label[predecessor]['receipts'] if predecessor in item_by_label else [],
        'links': steps,
        'terminal': terminal['label'] if terminal else None,
        'terminal_state': terminal['state'] if terminal else None,
        'terminal_receipts': item_by_label[terminal['label']]['receipts']
        if terminal and terminal['label'] in item_by_label else [],
        'successor': ticket['replaced_by'],
        'successor_state': ledger[ticket['replaced_by']]['state']
        if ticket['replaced_by'] in ledger else None,
        'successor_codes': list(ledger[ticket['replaced_by']]['codes'])
        if ticket['replaced_by'] in ledger else [],
        'failure': failure,
        'parcel_state': _parcel_state(ticket['parcel'], parcels),
        'issues': issues, 'consistent': not issues,
    }


def bill(intake, tickets, reconcile):
    ledger = {row['label']: row for row in tickets['labels']}
    parcels = {row['parcel']: row for row in reconcile['parcels']}
    items, ignored = _sheets(intake, ledger, parcels)
    by_label = {item['label']: item for item in items
                if item['label'] and item['key'] == f'label:{item["label"]}'}
    chains = [_chain(label, ticket, ledger, parcels, by_label)
              for label, ticket in ledger.items() if ticket['void']]
    chains.sort(key=lambda row: (row['void']['at'], row['voided']))

    attempts = [{'label': row['label'], 'target': row['replaces'], 'parcel': row['parcel'],
                 'state': row['state'], 'codes': list(row['codes'])}
                for row in ledger.values() if row['replaces']]
    attempts.sort(key=lambda row: row['label'])

    counts = Counter(item['disposition'] for item in items)
    states = Counter(row['state'] for row in ledger.values())
    totals = {
        'sheets': len(items), 'usage': counts[USAGE], 'allowance': counts[ALLOWANCE],
        'deferred': counts[DEFERRED],
        'carrier_billed': counts[USAGE] + counts[ALLOWANCE],
        'finance_payable': counts[USAGE], 'credit_claim': counts[ALLOWANCE],
        'accounted_rows': sum(item['accounted_rows'] for item in items),
        'duplicate_rows': sum(len(item['duplicates']) for item in items),
        'blank_rows': len([row for row in ignored if row['kind'] == 'blank']),
        'input_rows': intake['input_rows'],
    }
    checks = [
        {'code': 'USAGE_MATCHES_LIVE', 'ok': totals['usage'] == states['valid'],
         'detail': f"用量 {totals['usage']} / 可发面单 {states['valid']}"},
        {'code': 'ALLOWANCE_MATCHES_LEDGER',
         'ok': totals['allowance'] == states['revoked'] + states['isolated'],
         'detail': (f"折让 {totals['allowance']} / 作废 {states['revoked']}"
                    f" + 隔离 {states['isolated']}")},
        {'code': 'SHEET_DISPOSITION_TOTAL',
         'ok': totals['usage'] + totals['allowance'] + totals['deferred'] == totals['sheets'],
         'detail': f"{totals['sheets']} 张 = {totals['usage']} + {totals['allowance']} + {totals['deferred']}"},
        {'code': 'ROW_ACCOUNTING',
         'ok': totals['accounted_rows'] + totals['duplicate_rows'] + totals['blank_rows'] == totals['input_rows'],
         'detail': (f"{totals['accounted_rows']} 行纸单 + {totals['duplicate_rows']} 行重复回执"
                    f" + {totals['blank_rows']} 行空白 = {totals['input_rows']} 行输入")},
        {'code': 'CARRIER_FINANCE_SPLIT',
         'ok': totals['carrier_billed'] == totals['finance_payable'] + totals['credit_claim'],
         'detail': (f"承运商 {totals['carrier_billed']} = 应付 {totals['finance_payable']}"
                    f" + 折让 {totals['credit_claim']}")},
        {'code': 'PARCEL_LIVE_MATCHES_USAGE',
         'ok': all(item['label'] in item['parcel_state']['live_labels']
                   for item in items if item['disposition'] == USAGE),
         'detail': '计入用量的面单均为包裹当前可发面单'},
        {'code': 'VOID_CHAIN_CONSISTENT', 'ok': all(chain['consistent'] for chain in chains),
         'detail': f"{len(chains)} 条作废链" + ('' if all(chain['consistent'] for chain in chains)
                                                else '，存在未接续或未生效后继')},
    ]
    return {'sheets': items, 'chains': chains, 'reprint_attempts': attempts,
            'ignored': ignored, 'totals': totals, 'checks': checks,
            'ok': all(check['ok'] for check in checks)}
