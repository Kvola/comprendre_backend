# -*- coding: utf-8 -*-
"""Tableau de bord de Comprendre : usage et coût de l'IA, codes IA, ventes — en un écran.

Un seul appel (``get_data``) renvoie tous les chiffres d'une période ; les calculs sont faits
en SQL agrégé (quelques requêtes, quel que soit le volume). Chaque chiffre est accompagné de
quoi ouvrir la liste correspondante (modèle + domaine), pour passer du chiffre aux lignes.
"""
import os
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import AccessError

PERIODS = (7, 30, 90)
DEFAULT_USD_RATE = 600.0  # F CFA pour 1 dollar, modifiable dans les réglages


class ComprendreDashboard(models.AbstractModel):
    _name = 'comprendre.dashboard'
    _description = "Tableau de bord Comprendre"

    @api.model
    def get_data(self, days=30):
        if not (self.env.su or self.env.user.has_group('comprendre_backend.group_comprendre_manager')):
            raise AccessError(_("Réservé aux gestionnaires de Comprendre."))
        days = days if days in PERIODS else 30
        self.env.flush_all()  # les calculs SQL doivent voir les dernières écritures de l'ORM
        today = fields.Date.context_today(self)
        start = today - timedelta(days=days - 1)
        Param = self.env['ir.config_parameter'].sudo()
        try:
            rate = float(Param.get_param('comprendre.usd_xof_rate', DEFAULT_USD_RATE) or DEFAULT_USD_RATE)
        except ValueError:
            rate = DEFAULT_USD_RATE
        # Le taux convertit des dollars vers la monnaie de vente ; une société en dollars n'a rien à convertir.
        currency = self.env['comprendre.ai.order'].sudo()._selling_company().currency_id
        if currency.name == 'USD':
            rate = 1.0
        return {
            'days': days,
            'start': fields.Date.to_string(start),
            'today': fields.Date.to_string(today),
            'rate': rate,
            'convert': currency.name != 'USD',
            'usage': self._usage(start, today),
            'series': self._series(start, today),
            'codes': self._codes(today),
            'batches': self._batches(),
            'sales': self._sales(start, rate),
            'alerts': self._alerts(today),
            'model': Param.get_param('comprendre.model') or 'claude-opus-5-5',
        }

    # ------------------------------------------------------------------ #
    def _usage(self, start, today):
        cr = self.env.cr
        cr.execute("""
            SELECT count(*) FILTER (WHERE state = 'done'),
                   count(*) FILTER (WHERE state = 'done' AND code_id IS NULL),
                   count(*) FILTER (WHERE state = 'done' AND code_id IS NOT NULL),
                   count(*) FILTER (WHERE state = 'rejected'),
                   count(*) FILTER (WHERE state = 'failed'),
                   count(*) FILTER (WHERE state = 'pending'),
                   coalesce(sum(cost), 0),
                   coalesce(sum(cost) FILTER (WHERE code_id IS NOT NULL), 0),
                   count(DISTINCT device_id) FILTER (WHERE state = 'done'),
                   coalesce(avg(duration_ms) FILTER (WHERE state = 'done'), 0),
                   count(*) FILTER (WHERE state = 'refused'),
                   count(DISTINCT device_id) FILTER (WHERE state = 'refused')
              FROM comprendre_ai_request WHERE day BETWEEN %s AND %s""", [start, today])
        (done, free, coded, rejected, failed, pending, cost, cost_codes, devices, duration,
         refused, refused_devices) = cr.fetchone()
        cr.execute("""SELECT count(*) FILTER (WHERE state = 'done'), coalesce(sum(cost), 0),
                             count(*) FILTER (WHERE state IN ('failed', 'rejected'))
                        FROM comprendre_ai_request WHERE day = %s""", [today])
        done_today, cost_today, failed_today = cr.fetchone()
        attempts = done + rejected + failed
        return {
            'done': done, 'free': free, 'coded': coded, 'rejected': rejected, 'failed': failed, 'pending': pending,
            'cost': float(cost), 'cost_codes': float(cost_codes),
            'cost_per_quiz': float(cost) / done if done else 0.0,
            'failure_rate': 100.0 * (rejected + failed) / attempts if attempts else 0.0,
            'devices': devices, 'duration_s': float(duration) / 1000.0,
            'refused': refused, 'refused_devices': refused_devices,
            'done_today': done_today, 'cost_today': float(cost_today), 'failed_today': failed_today,
        }

    def _series(self, start, today):
        self.env.cr.execute("""
            SELECT day,
                   count(*) FILTER (WHERE state = 'done' AND code_id IS NULL),
                   count(*) FILTER (WHERE state = 'done' AND code_id IS NOT NULL),
                   count(*) FILTER (WHERE state IN ('failed', 'rejected')),
                   coalesce(sum(cost), 0)
              FROM comprendre_ai_request WHERE day BETWEEN %s AND %s
             GROUP BY day""", [start, today])
        by_day = {row[0]: row[1:] for row in self.env.cr.fetchall()}
        series = []
        day = start
        while day <= today:
            free, coded, failed, cost = by_day.get(day, (0, 0, 0, 0))
            series.append({'day': fields.Date.to_string(day), 'free': free, 'coded': coded, 'failed': failed,
                           'cost': round(float(cost), 4)})
            day += timedelta(days=1)
        return series

    def _codes(self, today):
        Code = self.env['comprendre.ai.code'].with_context(active_test=False)
        cr = self.env.cr
        cr.execute("""
            SELECT count(*) FILTER (WHERE valid AND used = 0 AND devices = 0),
                   count(*) FILTER (WHERE valid AND (used > 0 OR devices > 0)),
                   count(*) FILTER (WHERE active AND date_expiry < %(today)s),
                   count(*) FILTER (WHERE valid AND remaining <= 0),
                   count(*) FILTER (WHERE NOT active),
                   coalesce(sum(remaining) FILTER (WHERE valid), 0),
                   coalesce(sum(remaining) FILTER (WHERE valid AND devices > 0), 0),
                   count(*) FILTER (WHERE valid AND used = 0 AND devices = 0 AND create_date < %(month)s)
              FROM (SELECT c.*, coalesce(c.device_count, 0) AS devices,
                           c.active AND (c.date_expiry IS NULL OR c.date_expiry >= %(today)s) AS valid
                      FROM comprendre_ai_code c) s""",
                   {'today': today, 'month': fields.Datetime.now() - timedelta(days=30)})
        new, active, expired, exhausted, disabled, outstanding, outstanding_activated, dormant = cr.fetchone()
        new_ = [('state', '=', 'new')]
        soon = today + timedelta(days=14)
        expiring = Code.search([('active', '=', True), ('date_expiry', '>=', today), ('date_expiry', '<=', soon),
                                ('remaining', '>', 0)], order='date_expiry, remaining desc', limit=8)
        expiring_count = Code.search_count([('active', '=', True), ('date_expiry', '>=', today),
                                            ('date_expiry', '<=', soon), ('remaining', '>', 0)])
        low = Code.search([('active', '=', True), ('remaining', '>', 0), ('usage_percent', '>=', 80),
                           '|', ('date_expiry', '=', False), ('date_expiry', '>=', today)],
                          order='usage_percent desc', limit=5)

        def row(c):
            return {'id': c.id, 'code': c.code, 'who': c.batch_id.name or c.name or c.partner_id.name or '',
                    'remaining': c.remaining, 'credits': c.credits,
                    'expiry': fields.Date.to_string(c.date_expiry) if c.date_expiry else False,
                    'days_left': (c.date_expiry - today).days if c.date_expiry else None,
                    'devices': c.device_count, 'origin': c.origin}

        return {
            'new': new, 'active': active, 'expired': expired, 'exhausted': exhausted, 'disabled': disabled,
            'outstanding': outstanding, 'outstanding_activated': outstanding_activated, 'dormant': dormant,
            'expiring_count': expiring_count,
            'expiring': [row(c) for c in expiring],
            'low': [row(c) for c in low],
            'domains': {
                'new': new_, 'active': [('state', '=', 'active')], 'exhausted': [('state', '=', 'exhausted')],
                'expired': [('state', '=', 'expired')],
                'expiring': [('active', '=', True), ('date_expiry', '>=', fields.Date.to_string(today)),
                             ('date_expiry', '<=', fields.Date.to_string(soon)), ('remaining', '>', 0)],
                'dormant': new_ + [('create_date', '<', fields.Datetime.to_string(
                    fields.Datetime.now() - timedelta(days=30)))],
            },
        }

    def _batches(self):
        batches = self.env['comprendre.ai.code.batch'].search([], order='id desc', limit=40)
        rows = [{'id': b.id, 'name': b.name, 'codes': b.code_count, 'activated': b.activated_count,
                 'credits': b.credits, 'used': b.used, 'usage': round(b.usage_percent, 1),
                 'activation': round(b.activation_percent, 1),
                 'expiry': fields.Date.to_string(b.date_expiry) if b.date_expiry else False}
                for b in batches]
        # Les lots qui vivent (usage récent) d'abord, puis les plus gros.
        rows.sort(key=lambda r: (-r['used'], -r['codes']))
        return rows[:6]

    def _sales(self, start, rate):
        Order = self.env['comprendre.ai.order'].sudo()
        start_dt = fields.Datetime.to_datetime(start)
        paid = Order.search([('state', '=', 'done'), ('paid_at', '>=', start_dt)])
        revenue = sum(paid.mapped('amount'))
        to_validate = Order.search_count([('awaiting_validation', '=', True)])
        drafts = Order.search_count([('state', '=', 'draft')])
        currency = paid[:1].currency_id or Order._selling_company().currency_id
        self.env.cr.execute("""
            SELECT coalesce(sum(r.cost), 0) FROM comprendre_ai_request r
              JOIN comprendre_ai_order o ON o.code_id = r.code_id
             WHERE r.day >= %s""", [start])
        cost_sold = float(self.env.cr.fetchone()[0])
        return {
            'paid': len(paid), 'revenue': revenue, 'credits_sold': sum(paid.mapped('credits')),
            'currency': currency.name or 'XOF', 'symbol': currency.symbol or 'F',
            'to_validate': to_validate, 'drafts': drafts,
            'cost_sold_local': cost_sold * rate,
            'packs_on_sale': self.env['comprendre.ai.pack'].sudo().search_count([]),
        }

    def _alerts(self, today):
        Param = self.env['ir.config_parameter'].sudo()
        alerts = []
        if Param.get_param('comprendre.enabled', '1') in ('0', 'False', 'false'):
            alerts.append({'level': 'danger', 'text': _("Le service de quiz IA est désactivé.")})
        if not (Param.get_param('comprendre.anthropic_api_key') or os.environ.get('ANTHROPIC_API_KEY')):
            alerts.append({'level': 'danger', 'text': _("Aucune clé API Anthropic : aucun quiz IA ne peut être créé.")})
        Req = self.env['comprendre.ai.request'].sudo()
        failed = Req.search_count([('day', '=', today), ('state', 'in', ('failed', 'rejected'))])
        done = Req.search_count([('day', '=', today), ('state', '=', 'done')])
        if failed >= 3 and failed > done:
            alerts.append({'level': 'warning', 'action': 'failures',
                           'text': _("Beaucoup d'échecs aujourd'hui (%(f)s échecs pour %(d)s réussis) : crédit "
                                     "Anthropic épuisé ou service indisponible ?", f=failed, d=done)})
        Order = self.env['comprendre.ai.order'].sudo()
        if Order._collection_info()['source'] == 'operator' and \
                self.env['comprendre.ai.pack'].sudo().search_count([]):
            alerts.append({'level': 'warning', 'action': 'settings',
                           'text': _("Aucun lien marchand Wave n'est réglé : les achats sont encaissés sur le compte "
                                     "de l'opérateur de la plateforme.")})
        to_validate = Order.search_count([('awaiting_validation', '=', True)])
        if to_validate:
            alerts.append({'level': 'warning', 'action': 'to_validate',
                           'text': _("%s achat(s) payé(s) par lien Wave attendent votre validation.", to_validate)})
        return alerts
