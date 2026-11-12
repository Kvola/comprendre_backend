# -*- coding: utf-8 -*-
"""Cartes de codes IA à imprimer : 2 colonnes × 3 lignes par page, avec un QR vers la page du code."""
import base64

from odoo import api, models


class ReportCodeCards(models.AbstractModel):
    _name = 'report.comprendre_backend.report_code_cards'
    _description = "Cartes de codes IA"

    @api.model
    def _get_report_values(self, docids, data=None):
        codes = self.env['comprendre.ai.code'].with_context(active_test=False).browse(docids).exists()
        Report = self.env['ir.actions.report']
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        cards = []
        for code in codes:
            try:
                png = Report.barcode('QR', code._share_url(), width=200, height=200, quiet=False)
                qr = 'data:image/png;base64,%s' % base64.b64encode(png).decode()
            except Exception:  # noqa: BLE001 — une carte sans QR reste utilisable
                qr = ''
            cards.append({
                'code': code.code,
                'credits': code.credits,
                'daily_limit': code.daily_limit,
                'devices': code.max_devices,
                'expiry': code.date_expiry.strftime('%d/%m/%Y') if code.date_expiry else '',
                'name': code.batch_id.name or code.name or '',
                'qr': qr,
            })
        rows = [cards[i:i + 2] for i in range(0, len(cards), 2)]
        pages = [rows[i:i + 3] for i in range(0, len(rows), 3)]
        return {'doc_ids': codes.ids, 'docs': codes, 'pages': pages, 'apps_url': base + '/apps/comprendre'}
