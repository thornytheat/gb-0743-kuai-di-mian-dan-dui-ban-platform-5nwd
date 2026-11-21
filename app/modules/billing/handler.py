import csv
import io

from flask import Blueprint, Response, jsonify

from app.errors import DomainError
from app.reasons import LABELS
from app.web import snapshot

bp = Blueprint('billing', __name__)

HEADERS = ['纸单', '包裹', '状态', '归类', '依据', '源行', '原始回执', '作废登记', '旧单', '后继',
           '包裹当前可发', '重复回执行']

DISPOSITION = {'usage': '承运商用量', 'allowance': '折让', 'deferred': '暂缓'}


def _billing(run):
    billing = run.get('billing')
    if billing is None:
        raise DomainError('BILLING_NOT_ARCHIVED', 409, run=run['manifest']['id'])
    return billing


@bp.get('/api/billing')
def inspect():
    run = snapshot()
    return jsonify(id=run['manifest']['id'], data=_billing(run))


def _rows(billing):
    for item in billing['sheets']:
        yield [item['label'] or '（无面单号）', item['parcel'] or '—', item['state'],
               DISPOSITION[item['disposition']], LABELS[item['reason']],
               ' / '.join(str(line) for line in item['lines']),
               ' / '.join(receipt['raw'] for receipt in item['receipts']),
               item['void']['id'] if item['void'] else '',
               item['predecessor'] or '', item['successor'] or '',
               ' / '.join(item['parcel_state']['live_labels']) or '无',
               ' / '.join(str(receipt['line']) for receipt in item['duplicates'])]


@bp.get('/api/billing/statement.csv')
def statement():
    run = snapshot()
    billing = _billing(run)
    out = io.StringIO()
    writer = csv.writer(out)
    writer.writerow(HEADERS)
    writer.writerows(_rows(billing))
    totals = billing['totals']
    writer.writerow([])
    writer.writerow(['合计', f"纸单 {totals['sheets']}", f"承运商 {totals['carrier_billed']}",
                     f"应付 {totals['finance_payable']}", f"折让 {totals['credit_claim']}",
                     f"暂缓 {totals['deferred']}"])
    return Response('﻿' + out.getvalue(), mimetype='text/csv; charset=utf-8',
                    headers={'Content-Disposition': f'attachment; filename="label-billing-{run["manifest"]["id"][:12]}.csv"'})
