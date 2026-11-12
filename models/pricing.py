# -*- coding: utf-8 -*-
"""Assistant de tarification : des prix d'offres tirés du coût réel de l'IA.

Prix d'un quiz = coût IA mesuré par quiz réussi (les échecs facturés compris) × (1 + marge de
sécurité), converti en monnaie locale, puis tel que la marge voulue reste après la commission Wave :

    prix unitaire = coût local × (1 + sécurité) / ((1 − commission) × (1 − marge))

Chaque offre applique sa remise de volume, puis le prix est arrondi au palier supérieur (50 F…).
Un prix qui ne couvrirait plus le coût (remise trop forte) est signalé et remonté au minimum.
"""
import math
from datetime import timedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

from .comprendre import DEFAULT_MODEL, price_of

# Jetons d'un quiz typique (texte moyen, 10 questions), quand il n'y a pas encore de mesure.
TYPICAL_TOKENS = (6000, 3000)
DEFAULT_TIERS = [
    # nom, description, quiz, remise %, validité (jours), mise en avant
    ("Découverte", "Pour essayer les quiz IA", 10, 0, 90, False),
    ("Étude", "Un trimestre de révisions", 30, 10, 180, True),
    ("Intensif", "Toute une année", 100, 20, 365, False),
]


class ComprendreAiPricing(models.TransientModel):
    _name = 'comprendre.ai.pricing'
    _description = "Assistant de tarification des quiz IA"

    period_days = fields.Integer("Mesure sur (jours)", default=90)
    measured_quizzes = fields.Integer("Quiz IA réussis mesurés", readonly=True)
    measured_cost = fields.Float("Coût mesuré par quiz ($)", digits=(12, 4), readonly=True,
                                 help="Coût de tous les appels (échecs facturés compris) ÷ quiz réussis.")
    estimated_cost = fields.Float("Coût estimé par quiz ($)", digits=(12, 4), readonly=True,
                                  help="Sans mesure suffisante : prix du modèle × jetons d'un quiz typique.")
    cost_usd = fields.Float("Coût retenu par quiz ($)", digits=(12, 4), required=True,
                            help="Pré-rempli avec la mesure (ou l'estimation) ; modifiable.")
    rate = fields.Float("Taux du dollar", required=True, default=600.0, help="Monnaie locale pour 1 $.")
    currency_id = fields.Many2one('res.currency', readonly=True)
    safety_percent = fields.Float("Marge de sécurité (%)", default=30.0,
                                  help="Couvre les textes plus longs que la moyenne et les hausses de prix.")
    wave_fee_percent = fields.Float("Commission Wave (%)", default=1.0)
    margin_percent = fields.Float("Marge voulue (%)", default=40.0, help="Part du prix qui reste après coûts et commission.")
    rounding = fields.Integer("Arrondir au palier de", default=50, help="En monnaie locale (ex. 50 : 1 450, 1 500…).")
    activate = fields.Boolean("Mettre en vente", help="Active les offres appliquées (sinon, elles restent comme elles sont).")
    line_ids = fields.One2many('comprendre.ai.pricing.line', 'wizard_id', "Offres")
    unit_price = fields.Float("Prix de base d'un quiz", compute='_compute_unit', digits=(12, 2))
    floor_price = fields.Float("Prix plancher d'un quiz", compute='_compute_unit', digits=(12, 2),
                               help="Couvre le coût (avec la sécurité) et la commission, sans marge.")
    summary = fields.Html(compute='_compute_summary', sanitize=False)

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        Param = self.env['ir.config_parameter'].sudo()
        try:
            res['rate'] = float(Param.get_param('comprendre.usd_xof_rate', 600) or 600)
        except ValueError:
            res['rate'] = 600.0
        company = self.env['comprendre.ai.order']._selling_company()
        res['currency_id'] = company.currency_id.id
        if company.currency_id.name == 'USD':
            res['rate'] = 1.0
            res['rounding'] = 1
        days = res.get('period_days') or 90
        measured, count = self._measure(days)
        estimated = self._estimate()
        res.update(measured_cost=measured, measured_quizzes=count, estimated_cost=estimated,
                   cost_usd=measured if count >= 20 else max(measured, estimated))
        Pack = self.env['comprendre.ai.pack'].with_context(active_test=False)
        packs = Pack.search([('company_id', '=', company.id)], order='sequence, credits')
        if packs:
            base = max(p.price / p.credits for p in packs if p.credits) if packs else 0
            lines = []
            for p in packs:
                per = p.price / p.credits if p.credits else 0
                lines.append((0, 0, {'pack_id': p.id, 'name': p.name, 'description': p.description, 'credits': p.credits,
                                     'discount': round(100 * (1 - per / base)) if base and per < base else 0,
                                     'validity_days': p.validity_days, 'highlight': p.highlight}))
        else:
            lines = [(0, 0, {'name': n, 'description': d, 'credits': c, 'discount': r, 'validity_days': v, 'highlight': h})
                     for n, d, c, r, v, h in DEFAULT_TIERS]
        res['line_ids'] = lines
        return res

    @api.model
    def _measure(self, days):
        """Coût moyen par quiz réussi sur la période : (coût de tous les appels) ÷ (quiz réussis)."""
        since = fields.Date.context_today(self) - timedelta(days=max(1, days) - 1)
        self.env.flush_all()
        self.env.cr.execute("""SELECT coalesce(sum(cost), 0), count(*) FILTER (WHERE state = 'done')
                                 FROM comprendre_ai_request WHERE day >= %s""", [since])
        cost, done = self.env.cr.fetchone()
        return (float(cost) / done if done else 0.0), done

    @api.model
    def _estimate(self):
        model = self.env['ir.config_parameter'].sudo().get_param('comprendre.model') or DEFAULT_MODEL
        price = price_of(model) or price_of(DEFAULT_MODEL)
        return (TYPICAL_TOKENS[0] * price[0] + TYPICAL_TOKENS[1] * price[1]) / 1e6

    @api.onchange('period_days')
    def _onchange_period(self):
        measured, count = self._measure(self.period_days or 90)
        self.measured_cost, self.measured_quizzes = measured, count
        if count >= 20:
            self.cost_usd = measured

    def _round_up(self, amount):
        step = max(1, self.rounding or 1)
        return math.ceil(round(amount, 6) / step) * step

    @api.depends('cost_usd', 'rate', 'safety_percent', 'wave_fee_percent', 'margin_percent')
    def _compute_unit(self):
        for w in self:
            fee = min(max(w.wave_fee_percent, 0), 50) / 100
            margin = min(max(w.margin_percent, 0), 90) / 100
            cost = w.cost_usd * (w.rate or 1) * (1 + max(w.safety_percent, 0) / 100)
            w.floor_price = cost / (1 - fee)
            w.unit_price = w.floor_price / (1 - margin)

    @api.depends('unit_price', 'floor_price', 'measured_quizzes', 'line_ids.below_floor', 'line_ids.name')
    def _compute_summary(self):
        for w in self:
            w.summary = w._summary_html()

    def _summary_html(self):
        cur = self.currency_id.symbol or ''
        if not self.cost_usd:
            return _("<p class='text-warning'>Coût par quiz inconnu : indiquez-le pour calculer les prix.</p>")
        low = self.line_ids.filtered('below_floor')
        parts = [_("<p>Un quiz coûte environ <b>%(cost)s %(cur)s</b> en IA (sécurité comprise). Prix de base : "
                   "<b>%(unit)s %(cur)s</b> le quiz ; plancher sans marge : %(floor)s %(cur)s.</p>",
                   cost=round(self.cost_usd * (self.rate or 1) * (1 + self.safety_percent / 100), 1),
                   unit=round(self.unit_price, 1), floor=round(self.floor_price, 1), cur=cur)]
        if self.measured_quizzes < 20:
            parts.append(_("<p class='text-warning'>Moins de 20 quiz IA mesurés : le coût retenu est une "
                           "estimation. Refaites ce calcul quand il y aura plus d'usage.</p>"))
        if low:
            parts.append(_("<p class='text-danger'>Remise trop forte pour %s : prix remonté au plancher.</p>",
                           ", ".join(low.mapped('name'))))
        return ''.join(parts)

    def action_apply(self):
        self.ensure_one()
        if not self.cost_usd or self.cost_usd <= 0:
            raise UserError(_("Indiquez le coût par quiz."))
        if not self.line_ids:
            raise UserError(_("Ajoutez au moins une offre."))
        Pack = self.env['comprendre.ai.pack'].with_context(active_test=False)
        company = self.env['comprendre.ai.order']._selling_company()
        for line in self.line_ids:
            if line.credits <= 0:
                raise UserError(_("L'offre %s doit donner au moins un quiz.", line.name))
            values = {'name': line.name, 'description': line.description, 'credits': line.credits,
                      'price': line.price, 'validity_days': line.validity_days, 'highlight': line.highlight,
                      'sequence': line.sequence}
            if self.activate:
                values['active'] = True
            if line.pack_id:
                line.pack_id.write(values)
            else:
                Pack.create(dict(values, company_id=company.id, active=self.activate))
        if self.currency_id.name != 'USD':
            self.env['ir.config_parameter'].sudo().set_param('comprendre.usd_xof_rate', self.rate)
        return self.env['ir.actions.act_window']._for_xml_id('comprendre_backend.action_packs')


class ComprendreAiPricingLine(models.TransientModel):
    _name = 'comprendre.ai.pricing.line'
    _description = "Offre proposée par l'assistant de tarification"
    _order = 'sequence, credits'

    wizard_id = fields.Many2one('comprendre.ai.pricing', required=True, ondelete='cascade')
    pack_id = fields.Many2one('comprendre.ai.pack', "Offre existante")
    sequence = fields.Integer(default=10)
    name = fields.Char("Nom", required=True)
    description = fields.Char("Description")
    credits = fields.Integer("Quiz IA", required=True, default=10)
    discount = fields.Float("Remise (%)", default=0.0, help="Par rapport au prix de base d'un quiz.")
    validity_days = fields.Integer("Validité (jours)", default=180)
    highlight = fields.Boolean("Mise en avant")
    price = fields.Float("Prix proposé", digits=(12, 0), compute='_compute_price')
    per_quiz = fields.Float("Par quiz", digits=(12, 1), compute='_compute_price')
    cost = fields.Float("Coût IA", digits=(12, 0), compute='_compute_price')
    margin = fields.Float("Marge", digits=(12, 0), compute='_compute_price',
                          help="Ce qui reste après coût IA et commission Wave.")
    margin_percent = fields.Float("Marge (%)", digits=(12, 0), compute='_compute_price')
    below_floor = fields.Boolean(compute='_compute_price')

    @api.depends('credits', 'discount', 'wizard_id.unit_price', 'wizard_id.floor_price', 'wizard_id.rounding',
                 'wizard_id.wave_fee_percent', 'wizard_id.cost_usd', 'wizard_id.rate')
    def _compute_price(self):
        """Prix arrondi au palier supérieur, jamais sous le plancher (coût + sécurité + commission)."""
        for line in self:
            w = line.wizard_id
            fee = min(max(w.wave_fee_percent, 0), 50) / 100
            credits = max(line.credits, 0)
            wanted = w.unit_price * credits * (1 - min(max(line.discount, 0), 90) / 100)
            floor = w.floor_price * credits
            line.below_floor = bool(credits) and wanted < floor - 1e-6
            price = w._round_up(max(wanted, floor)) if credits else 0
            line.price = price
            line.per_quiz = price / credits if credits else 0
            line.cost = w.cost_usd * (w.rate or 1) * credits
            line.margin = price * (1 - fee) - line.cost
            line.margin_percent = 100 * line.margin / price if price else 0
