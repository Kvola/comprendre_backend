# -*- coding: utf-8 -*-
"""Achat de quiz IA dans l'application : une offre payée par Wave délivre un code IA.

Parcours :

1. L'application affiche les **offres** actives (``comprendre.ai.pack``).
2. L'utilisateur en choisit une : un **achat** (``comprendre.ai.order``) est créé
   pour son appareil anonyme, puis un encaissement Wave (socle ``queue_payment``) :

   * **API Wave Checkout** configurée pour la société : session de paiement ;
     Wave confirme par webhook signé (``/queue/wave/webhook``) → payé tout seul ;
   * sinon **lien marchand** : la personne paie sur le lien Wave ; un agent
     valide l'encaissement dans le back-office (elle peut donner l'identifiant
     de sa transaction Wave pour l'aider).

3. Paiement confirmé (hook ``_on_payment_paid``) → un **code IA** est créé avec
   les quiz de l'offre et **activé sur l'appareil** : rien à saisir.

Une validation défaite (``_on_payment_reverted``) désactive le code ; une
nouvelle validation le réactive (jamais deux codes pour un achat).
"""
import logging
import re
import uuid

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError

_logger = logging.getLogger(__name__)

ORDER_STATES = [
    ('draft', "À payer"),
    ('done', "Payé, code délivré"),
    ('cancelled', "Annulé"),
]


class ComprendreAiPack(models.Model):
    """Une offre de quiz IA vendue dans l'application."""
    _name = 'comprendre.ai.pack'
    _description = "Offre de quiz IA"
    _order = 'sequence, price, id'

    name = fields.Char("Nom", required=True, translate=True, help="Affiché dans l'application (« Pack Étude »).")
    description = fields.Char("Description", translate=True)
    sequence = fields.Integer(default=10)
    active = fields.Boolean(default=True)
    credits = fields.Integer("Quiz IA", required=True, default=20)
    price = fields.Monetary("Prix", required=True, currency_field='currency_id')
    company_id = fields.Many2one('res.company', "Société qui encaisse", required=True,
                                 default=lambda self: self.env['comprendre.ai.order']._selling_company())
    currency_id = fields.Many2one(related='company_id.currency_id', store=True)
    validity_days = fields.Integer("Validité (jours)", default=365,
                                   help="Durée de validité du code délivré, à partir du paiement (0 : sans limite).")
    daily_limit = fields.Integer("Plafond par jour", default=0, help="Quiz IA par jour sur le code (0 : pas de plafond).")
    order_ids = fields.One2many('comprendre.ai.order', 'pack_id', "Achats")
    sold_count = fields.Integer("Vendus", compute='_compute_sold')

    _credits_positive = models.Constraint('CHECK(credits > 0)', "Une offre donne au moins un quiz IA.")
    _price_positive = models.Constraint('CHECK(price > 0)', "Le prix doit être positif.")
    _validity_positive = models.Constraint('CHECK(validity_days >= 0)', "La validité ne peut pas être négative.")

    def _compute_sold(self):
        for p in self:
            p.sold_count = len(p.order_ids.filtered(lambda o: o.state == 'done'))

    def _api_dict(self):
        self.ensure_one()
        return {
            'id': self.id,
            'name': self.name,
            'description': self.description or '',
            'credits': self.credits,
            'price': self.price,
            'currency': self.currency_id.name,
            'validity_days': self.validity_days,
            'daily_limit': self.daily_limit,
        }


class ComprendreAiOrder(models.Model):
    """L'achat d'une offre par un appareil : document encaissable (``queue.payable.mixin``)."""
    _name = 'comprendre.ai.order'
    _description = "Achat de quiz IA"
    _inherit = ['mail.thread', 'queue.payable.mixin']
    _order = 'id desc'

    # Produit de la plateforme : sans lien Wave propre à la société, l'opérateur encaisse.
    _wave_operator_collects = True

    name = fields.Char("Référence", required=True, copy=False, readonly=True, default='/', index=True)
    device_id = fields.Many2one('comprendre.device', "Appareil", required=True, index=True, ondelete='restrict',
                                readonly=True)
    pack_id = fields.Many2one('comprendre.ai.pack', "Offre", required=True, ondelete='restrict', readonly=True)
    credits = fields.Integer("Quiz IA", required=True, readonly=True)
    validity_days = fields.Integer("Validité (jours)", readonly=True)
    daily_limit = fields.Integer("Plafond par jour", readonly=True)
    amount = fields.Monetary("Montant", required=True, readonly=True)
    company_id = fields.Many2one('res.company', "Société", required=True, readonly=True)
    currency_id = fields.Many2one('res.currency', "Devise", required=True, readonly=True)
    state = fields.Selection(ORDER_STATES, string="État", default='draft', required=True, tracking=True, index=True)
    pay_mode = fields.Selection([('checkout', "API Wave (confirmation automatique)"),
                                 ('merchant', "Lien marchand (validation par un agent)")],
                                string="Mode de paiement", readonly=True)
    client_ref = fields.Char("Clé d'idempotence", readonly=True, copy=False, index=True,
                             help="Fournie par l'application : un même achat renvoyé (réseau coupé, double appui) "
                                  "retrouve celui-ci au lieu d'en créer un second.")
    wave_ref = fields.Char("Transaction Wave (déclarée)", readonly=True, tracking=True,
                           help="Identifiant de transaction donné par l'acheteur (lien marchand), pour le rapprochement.")
    code_id = fields.Many2one('comprendre.ai.code', "Code IA délivré", readonly=True, copy=False)
    paid_at = fields.Datetime("Payé le", readonly=True, copy=False)
    awaiting_validation = fields.Boolean("À valider", compute='_compute_awaiting', search='_search_awaiting',
                                         help="Paiement déclaré, en attente d'un agent.")

    _client_ref_unique = models.Constraint('UNIQUE(device_id, client_ref)', "Achat déjà enregistré.")
    _amount_positive = models.Constraint('CHECK(amount > 0)', "Montant invalide.")

    # ------------------------------------------------------------------ #
    @api.model
    def _selling_company(self):
        """Société qui vend et encaisse : paramètre ``comprendre.company_id``, sinon la société principale."""
        param = self.env['ir.config_parameter'].sudo().get_param('comprendre.company_id')
        company = self.env['res.company'].sudo().browse(int(param)).exists() if param and param.isdigit() else None
        return company or self.env.ref('base.main_company')

    @api.model_create_multi
    def create(self, vals_list):
        for vals in vals_list:
            if vals.get('name', '/') == '/':
                vals['name'] = self.env['ir.sequence'].next_by_code('comprendre.ai.order') or '/'
        return super().create(vals_list)

    @api.depends('amount_paid', 'amount')
    def _compute_payment_amounts(self):
        return super()._compute_payment_amounts()

    @api.depends('state', 'amount_paid')
    def _compute_awaiting(self):
        for o in self:
            o.awaiting_validation = o.state == 'draft' and any(
                p.state == 'to_validate' for p in o._all_payments())

    def _search_awaiting(self, operator, value):
        if operator not in ('=', '!='):
            raise UserError(_("Recherche non prise en charge."))
        ids = self.env['queue.payment'].sudo().search(
            [('res_model', '=', self._name), ('state', '=', 'to_validate')]).mapped('res_id')
        if (operator == '=') == bool(value):
            return [('id', 'in', ids), ('state', '=', 'draft')]
        return ['|', ('id', 'not in', ids), ('state', '!=', 'draft')]

    # -- Contrat de queue.payable.mixin ----------------------------------- #
    def _payment_is_open(self):
        self.ensure_one()
        return self.state == 'draft'

    def _payment_company(self):
        self.ensure_one()
        return self.company_id

    def _payment_description(self):
        self.ensure_one()
        return _("Comprendre — %(credits)s quiz IA (%(ref)s)", credits=self.credits, ref=self.name)

    def _on_payment_paid(self, payment):
        res = super()._on_payment_paid(payment)
        for order in self.filtered(lambda o: o.state == 'draft' and o.payment_state in ('paid', 'overpaid')):
            order._deliver()
        return res

    def _on_payment_reverted(self, payment):
        res = super()._on_payment_reverted(payment)
        for order in self.filtered(lambda o: o.state == 'done' and o.payment_state not in ('paid', 'overpaid')):
            order.code_id.active = False
            order.state = 'draft'
            order.message_post(body=_("Validation défaite : le code %s est désactivé.", order.code_id.code),
                               message_type='notification')
        return res

    # ------------------------------------------------------------------ #
    def _deliver(self):
        """Crée (ou réactive) le code IA de l'achat et l'active sur l'appareil acheteur."""
        self.ensure_one()
        Code = self.env['comprendre.ai.code'].sudo()
        today = fields.Date.context_today(self)
        if self.code_id:
            self.code_id.active = True
            code = self.code_id
        else:
            code = Code.create({
                'name': _("Achat %s", self.name),
                'credits': self.credits,
                'daily_limit': self.daily_limit,
                'max_devices': 1,
                'date_expiry': today + relativedelta(days=self.validity_days - 1) if self.validity_days else False,
            })
        Activation = self.env['comprendre.ai.code.activation'].sudo()
        if not Activation.search_count([('code_id', '=', code.id), ('device_id', '=', self.device_id.id)]):
            Activation.create({'code_id': code.id, 'device_id': self.device_id.id})
        self.write({'code_id': code.id, 'state': 'done', 'paid_at': fields.Datetime.now()})
        self.message_post(body=_("Paiement confirmé : code %(code)s (%(credits)s quiz IA) activé sur l'appareil.",
                                 code=code.code, credits=self.credits), message_type='notification')
        return code

    @api.model
    def _buy(self, device, pack_id, client_ref=None):
        """Crée l'achat (ou retrouve celui de ``client_ref``) et prépare son paiement Wave."""
        from .comprendre import Refusal  # noqa: PLC0415
        client_ref = re.sub(r'[^A-Za-z0-9_-]', '', str(client_ref or ''))[:64] or uuid.uuid4().hex
        order = self.search([('device_id', '=', device.id), ('client_ref', '=', client_ref)], limit=1)
        if not order:
            try:
                pack = self.env['comprendre.ai.pack'].sudo().browse(int(pack_id)).exists()
            except (TypeError, ValueError):
                pack = None
            if not pack or not pack.active:
                raise Refusal(_("Cette offre n'est plus proposée."), 'pack_unavailable')
            order = self.create({
                'device_id': device.id, 'pack_id': pack.id, 'credits': pack.credits,
                'validity_days': pack.validity_days, 'daily_limit': pack.daily_limit,
                'amount': pack.price, 'company_id': pack.company_id.id, 'currency_id': pack.currency_id.id,
                'client_ref': client_ref,
            })
        if order.state != 'draft':
            return order, {}
        return order, order._start_payment()

    def _payment_ref(self):
        """Clé d'idempotence du paiement : un achat n'a jamais qu'un encaissement ouvert."""
        self.ensure_one()
        return 'comprendre:%s' % self.name

    def _start_payment(self):
        """Paiement Wave : session API si la société en a une, sinon lien marchand.

        Renvoie ``{'mode', 'url'}`` ; lève ``Refusal('payment_unavailable')`` si
        aucun moyen n'est configuré (ni API, ni lien marchand).
        """
        from .comprendre import Refusal  # noqa: PLC0415
        from odoo.addons.queue_payment.models.queue_wave import WaveError  # noqa: PLC0415
        self.ensure_one()
        Wave = self.env['queue.wave.mixin'].sudo()
        ref = self._payment_ref()
        if Wave._wave_api_configured(self.company_id):
            payment = self._new_payment(self.amount_residual, 'wave', client_ref=ref, state='pending')
            try:
                session_id, url = Wave._wave_checkout_session(
                    self.amount_residual, self.currency_id.name, client_reference=ref,
                    return_url=self._return_url(), company=self.company_id)
            except WaveError:
                _logger.warning("Comprendre : session Wave impossible pour %s, repli sur le lien marchand", self.name)
            else:
                payment.sudo().write({'provider_ref': session_id})
                self.pay_mode = 'checkout'
                return {'mode': 'checkout', 'url': url}
            payment._submit(method='wave')
            url = payment._wave_url()
        else:
            url = self._wave_payment_url(client_ref=ref)['url']
        if not url:
            raise Refusal(_("Le paiement n'est pas disponible pour le moment."), 'payment_unavailable')
        self.pay_mode = 'merchant'
        return {'mode': 'merchant', 'url': url}

    def _return_url(self):
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        return '%s/comprendre/paiement/%s' % (base, self.name)

    def _declare_wave_ref(self, wave_ref):
        """L'acheteur donne l'identifiant de sa transaction Wave (lien marchand) pour aider l'agent."""
        from .comprendre import Refusal  # noqa: PLC0415
        self.ensure_one()
        ref = re.sub(r'\s+', '', str(wave_ref or ''))[:64]
        if len(ref) < 6:
            raise Refusal(_("Identifiant de transaction trop court."), 'bad_wave_ref')
        if self.state != 'draft':
            return self
        payment = self._all_payments().filtered(lambda p: p.state in ('pending', 'to_validate'))[:1]
        if payment:
            try:
                payment._check_ref_unused(ref)
            except UserError as e:
                raise Refusal(str(e.args[0]), 'wave_ref_used') from e
            payment.sudo().write({'provider_ref': ref})
            if payment.state == 'pending':
                payment._submit(method='wave')
        self.wave_ref = ref
        return self

    def _api_dict(self):
        self.ensure_one()
        payments = self._all_payments()
        return {
            'ref': self.name,
            'pack_id': self.pack_id.id,
            'pack': self.pack_id.name,
            'credits': self.credits,
            'amount': self.amount,
            'currency': self.currency_id.name,
            'state': self.state,
            'mode': self.pay_mode or False,
            'awaiting_validation': any(p.state == 'to_validate' for p in payments),
            'wave_ref': self.wave_ref or False,
            'created': fields.Datetime.to_string(self.create_date),
            'code': self.code_id.code if self.state == 'done' else False,
        }

    @api.model
    def _device_orders(self, device, days=60):
        """Achats récents de l'appareil (les annulés ne sont pas montrés)."""
        since = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        return self.search([('device_id', '=', device.id), ('create_date', '>=', since),
                            ('state', '!=', 'cancelled')], limit=20)

    @api.model
    def _cron_cancel_stale(self, days=30):
        """Achats jamais payés au bout de ``days`` jours : annulés, avec leurs encaissements ouverts."""
        limit = fields.Datetime.subtract(fields.Datetime.now(), days=days)
        stale = self.search([('state', '=', 'draft'), ('create_date', '<', limit)])
        for order in stale:
            for payment in order._all_payments().filtered(lambda p: p.state in ('draft', 'pending', 'to_validate')):
                payment.action_cancel(reason=_("Achat %s non payé depuis %s jours.", order.name, days))
            if order.payment_state == 'unpaid':
                order.state = 'cancelled'
        return len(stale)

    def action_cancel(self):
        for order in self.filtered(lambda o: o.state == 'draft'):
            if order.amount_paid:
                raise UserError(_("%s a déjà reçu un paiement : défaites d'abord sa validation.", order.name))
            for payment in order._all_payments().filtered(lambda p: p.state in ('draft', 'pending', 'to_validate')):
                payment.action_cancel(reason=_("Achat annulé dans le back-office."))
            order.state = 'cancelled'
