# -*- coding: utf-8 -*-
"""Codes IA : un code unique, remis à une personne ou à un groupe, qui donne un crédit
de quiz IA en plus du quota gratuit du jour.

- Le code est saisi dans l'application ; il est alors **lié à l'appareil** (anonyme).
  ``max_devices`` (1 par défaut) borne le nombre d'appareils qui peuvent l'utiliser :
  un code de classe peut servir à 30 téléphones et partager leurs ``credits``.
- Chaque quiz IA réussi au-delà du quota gratuit consomme 1 crédit (une demande en
  cours compte déjà, comme pour le quota gratuit).
- Facultatif : date limite, plafond de quiz par jour et par appareil.
- Changement de téléphone : un code à un seul appareil peut être **transféré** par
  son détenteur (il le saisit sur le nouveau téléphone et confirme), un nombre de fois
  borné (``transfers_allowed``). L'utilisateur peut aussi **retirer** un code de son
  téléphone (pour le donner à quelqu'un) : la place se libère, le crédit reste.
- Les codes se créent un par un ou **par lots** (une école, un partenaire) : le lot
  suit l'usage de ses codes et permet de tous les modifier, désactiver ou imprimer.
"""
import re
import secrets

from dateutil.relativedelta import relativedelta

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Sans 0/O, 1/I/L : un code se dicte et se recopie sans erreur.
CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
CODE_LENGTH = 8
DEFAULT_TRANSFERS = 3
# Une ligne « terminée » (expirée, épuisée, désactivée) reste visible dans l'application ce nombre de jours.
ENDED_VISIBLE_DAYS = 90


def code_key(text):
    """Forme de comparaison d'un code : majuscules, sans espaces ni tirets (« ia-7kq3 m9px » = « IA7KQ3M9PX »)."""
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def new_code(prefix=''):
    """« 7KQ3-M9PX » (ou « LYC-7KQ3-M9PX » avec un préfixe) : 8 caractères tirés au hasard (40 bits)."""
    raw = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    code = '%s-%s' % (raw[:4], raw[4:])
    return '%s-%s' % (prefix, code) if prefix else code


def clean_prefix(prefix):
    return code_key(prefix)[:6]


class ComprendreAiCode(models.Model):
    _name = 'comprendre.ai.code'
    _description = "Code IA"
    _inherit = ['mail.thread']
    _order = 'create_date desc, id desc'
    _rec_name = 'code'

    code = fields.Char("Code", required=True, copy=False, default=lambda self: self._unique_code(), tracking=True,
                       help="Saisi dans l'application (majuscules, espaces et tirets indifférents).")
    key = fields.Char("Clé", compute='_compute_key', store=True, index=True, readonly=True)
    name = fields.Char("Bénéficiaire", tracking=True,
                       help="Pour qui est ce code (personne, classe, partenaire…) : note interne.")
    partner_id = fields.Many2one('res.partner', "Contact", tracking=True,
                                 help="Facultatif : la personne ou l'organisation à qui le code est remis.")
    batch_id = fields.Many2one('comprendre.ai.code.batch', "Lot", index=True, ondelete='set null', tracking=True)
    note = fields.Text("Notes internes")
    credits = fields.Integer("Quiz IA offerts", required=True, default=20, tracking=True,
                             help="Nombre total de quiz IA, partagé entre les appareils du code.")
    daily_limit = fields.Integer("Plafond par jour", default=0, tracking=True,
                                 help="Quiz IA par appareil et par jour sur ce code (0 : pas de plafond).")
    max_devices = fields.Integer("Appareils", required=True, default=1, tracking=True,
                                 help="Nombre d'appareils qui peuvent activer ce code.")
    date_expiry = fields.Date("Valable jusqu'au", tracking=True, help="Vide : sans limite de date.")
    transfers_allowed = fields.Integer(
        "Transferts permis", default=DEFAULT_TRANSFERS, tracking=True,
        help="Code à un seul appareil : combien de fois son détenteur peut le passer sur un autre téléphone "
             "lui-même (changement de téléphone). Au-delà, un gestionnaire libère la place.")
    transfer_count = fields.Integer("Transferts faits", default=0, readonly=True, copy=False)
    active = fields.Boolean(default=True, tracking=True)
    origin = fields.Selection([('manual', "Créé à la main"), ('batch', "Lot"), ('purchase', "Achat")],
                              string="Origine", compute='_compute_origin', store=True)
    order_ids = fields.One2many('comprendre.ai.order', 'code_id', "Achat")
    activation_ids = fields.One2many('comprendre.ai.code.activation', 'code_id', "Appareils activés")
    request_ids = fields.One2many('comprendre.ai.request', 'code_id', "Quiz IA")
    used = fields.Integer("Utilisés", compute='_compute_used', store=True)
    remaining = fields.Integer("Restants", compute='_compute_used', store=True)
    usage_percent = fields.Float("Utilisation (%)", compute='_compute_used', store=True, aggregator='avg')
    last_used = fields.Datetime("Dernier quiz IA", compute='_compute_used', store=True)
    device_count = fields.Integer("Nombre d'appareils", compute='_compute_devices', store=True)
    cost = fields.Float("Coût ($)", digits=(12, 4), compute='_compute_cost')
    days_left = fields.Integer("Jours restants", compute='_compute_state')
    state = fields.Selection([('new', "Pas encore utilisé"), ('active', "En cours"), ('exhausted', "Épuisé"),
                              ('expired', "Expiré"), ('disabled', "Désactivé")],
                             string="État", compute='_compute_state', search='_search_state')

    _key_unique = models.Constraint('UNIQUE(key)', "Ce code existe déjà.")
    _credits_positive = models.Constraint('CHECK(credits >= 0)', "Le nombre de quiz offerts ne peut pas être négatif.")
    _devices_positive = models.Constraint('CHECK(max_devices >= 1)', "Il faut au moins un appareil.")
    _daily_positive = models.Constraint('CHECK(daily_limit >= 0)', "Le plafond par jour ne peut pas être négatif.")
    _transfers_positive = models.Constraint('CHECK(transfers_allowed >= 0)',
                                            "Le nombre de transferts permis ne peut pas être négatif.")

    @api.model
    def _unique_code(self, prefix=''):
        for _attempt in range(20):
            code = new_code(prefix)
            if not self.with_context(active_test=False).search_count([('key', '=', code_key(code))]):
                return code
        raise UserError(_("Impossible de tirer un code libre : réessayez."))

    @api.depends('code')
    def _compute_key(self):
        for c in self:
            c.key = code_key(c.code)

    @api.depends('order_ids', 'batch_id')
    def _compute_origin(self):
        for c in self:
            c.origin = 'purchase' if c.order_ids else ('batch' if c.batch_id else 'manual')

    @api.depends('credits', 'request_ids.state')
    def _compute_used(self):
        for c in self:
            counted = c.request_ids.filtered(lambda r: r.state in ('done', 'pending'))
            c.used = len(counted)
            c.remaining = max(0, c.credits - c.used)
            c.usage_percent = min(100.0, 100.0 * c.used / c.credits) if c.credits else 0.0
            c.last_used = max(counted.mapped('create_date')) if counted else False

    @api.depends('activation_ids')
    def _compute_devices(self):
        for c in self:
            c.device_count = len(c.activation_ids)

    def _compute_cost(self):
        for c in self:
            c.cost = sum(c.request_ids.mapped('cost'))

    @api.depends('active', 'date_expiry', 'remaining', 'used', 'device_count')
    @api.depends_context('tz')
    def _compute_state(self):
        today = fields.Date.context_today(self)
        for c in self:
            c.days_left = (c.date_expiry - today).days if c.date_expiry else 0
            if not c.active:
                c.state = 'disabled'
            elif c.date_expiry and c.date_expiry < today:
                c.state = 'expired'
            elif c.remaining <= 0:
                c.state = 'exhausted'
            else:
                c.state = 'active' if c.used or c.device_count else 'new'

    def _search_state(self, operator, value):
        """Domaine pour filtrer et regrouper par état (dépend de la date du jour : pas stocké)."""
        if operator in ('=', '!='):
            values = [value]
        elif operator in ('in', 'not in'):
            values = list(value)
        else:
            raise UserError(_("Recherche non prise en charge."))
        today = fields.Date.context_today(self)
        valid = ['&', ('active', '=', True), '|', ('date_expiry', '=', False), ('date_expiry', '>=', today)]
        domains = {
            'disabled': [('active', '=', False)],
            'expired': [('active', '=', True), ('date_expiry', '<', today)],
            'exhausted': valid + [('remaining', '<=', 0)],
            'active': valid + [('remaining', '>', 0), '|', ('used', '>', 0), ('device_count', '>', 0)],
            'new': valid + [('remaining', '>', 0), ('used', '=', 0), ('device_count', '=', 0)],
        }
        ids = set()
        for v in values:
            if v in domains:
                ids |= set(self.with_context(active_test=False).search(domains[v]).ids)
        if operator in ('!=', 'not in'):
            return [('id', 'not in', list(ids))]
        return [('id', 'in', list(ids))]

    @api.constrains('code')
    def _check_code(self):
        for c in self:
            if len(c.key or '') < 6:
                raise ValidationError(_("Un code doit avoir au moins 6 lettres ou chiffres."))
            # Requête directe : une recherche ORM écrirait d'abord la clé, et la base
            # répondrait par une erreur technique au lieu de ce message.
            self.env.cr.execute('SELECT 1 FROM comprendre_ai_code WHERE key = %s AND id != %s LIMIT 1', [c.key, c.id])
            if self.env.cr.fetchone():
                raise ValidationError(_("Le code %s existe déjà (majuscules, espaces et tirets ne comptent pas).", c.code))

    @api.constrains('max_devices')
    def _check_devices(self):
        for c in self:
            if c.max_devices < len(c.activation_ids):
                raise ValidationError(_(
                    "Le code %(code)s est déjà activé sur %(n)s appareils : libérez-en d'abord, "
                    "ou gardez au moins %(n)s appareils.", code=c.code, n=len(c.activation_ids)))

    # ------------------------------------------------------------------ #
    # Consommation (comptes en direct : servent au quota, sous verrou)
    # ------------------------------------------------------------------ #
    def _consumed(self):
        """Quiz IA comptés sur ce code (réussis ou en cours)."""
        self.ensure_one()
        return self.env['comprendre.ai.request'].sudo().search_count(
            [('code_id', '=', self.id), ('state', 'in', ('done', 'pending'))])

    def _valid_today(self):
        """Actif, dans les dates, avec du crédit."""
        self.ensure_one()
        today = fields.Date.context_today(self)
        return self.active and not (self.date_expiry and self.date_expiry < today) and self._consumed() < self.credits

    def _used_today_by(self, device):
        self.ensure_one()
        return self.env['comprendre.ai.request'].sudo().search_count([
            ('code_id', '=', self.id), ('device_id', '=', device.id),
            ('day', '=', fields.Date.context_today(self)), ('state', 'in', ('done', 'pending'))])

    def _used_by(self, device):
        self.ensure_one()
        return self.env['comprendre.ai.request'].sudo().search_count([
            ('code_id', '=', self.id), ('device_id', '=', device.id), ('state', 'in', ('done', 'pending'))])

    def _usable_by(self, device):
        """Ce code peut-il payer un quiz de cet appareil aujourd'hui ?"""
        self.ensure_one()
        if not self._valid_today():
            return False
        return not self.daily_limit or self._used_today_by(device) < self.daily_limit

    def _live_state(self):
        """État d'après les comptes en direct (le champ stocké peut avoir une demande de retard)."""
        self.ensure_one()
        if not self.active:
            return 'disabled'
        if self.date_expiry and self.date_expiry < fields.Date.context_today(self):
            return 'expired'
        return 'exhausted' if self._consumed() >= self.credits else 'active'

    def _info(self, device):
        """Ce que l'application affiche pour ce code (les champs ajoutés en 1.5 sont ignorés avant)."""
        self.ensure_one()
        daily_left = max(0, self.daily_limit - self._used_today_by(device)) if self.daily_limit else None
        activation = self.activation_ids.filtered(lambda a: a.device_id == device)[:1]
        consumed = self._consumed()
        last = self.env['comprendre.ai.request'].sudo().search(
            [('code_id', '=', self.id), ('state', 'in', ('done', 'pending'))], order='id desc', limit=1)
        single = self.max_devices == 1
        return {
            'code': self.code,
            'remaining': max(0, self.credits - consumed),
            'credits': self.credits,
            'expires': fields.Date.to_string(self.date_expiry) if self.date_expiry else False,
            'daily_left': daily_left if daily_left is not None else False,
            'usable_today': self._usable_by(device),
            # 1.5 :
            'state': self._live_state(),
            'used': consumed,
            'used_by_me': self._used_by(device),
            'daily_limit': self.daily_limit,
            'max_devices': self.max_devices,
            'devices': len(self.activation_ids),
            'activated': fields.Datetime.to_string(activation.create_date) if activation else False,
            'last_used': fields.Datetime.to_string(last.create_date) if last else False,
            'source': 'purchase' if self.order_ids else 'gift',
            'transfers_left': max(0, self.transfers_allowed - self.transfer_count) if single else 0,
        }

    # ------------------------------------------------------------------ #
    # Actions de l'utilisateur (application)
    # ------------------------------------------------------------------ #
    @api.model
    def _redeem(self, device, text, transfer=False, can_transfer=False):
        """Active un code sur un appareil ; renvoie ``(code, déjà_activé)``.

        Lève ``Refusal`` (motif traduit par l'application). ``can_transfer`` : l'application
        sait proposer un transfert (1.5 et plus) ; ``transfer`` : l'utilisateur l'a confirmé.
        """
        from .comprendre import Refusal  # noqa: PLC0415 — import circulaire évité
        key = code_key(text)
        if len(key) < 6:
            raise Refusal(_("Ce code IA n'existe pas."), 'code_unknown')
        code = self.with_context(active_test=False).search([('key', '=', key)], limit=1)
        if not code:
            raise Refusal(_("Ce code IA n'existe pas."), 'code_unknown')
        # Verrou : deux téléphones qui activent en même temps le dernier « appareil » du code.
        self.env.cr.execute('SELECT id FROM comprendre_ai_code WHERE id = %s FOR UPDATE', [code.id])
        Activation = self.env['comprendre.ai.code.activation'].sudo()
        already = Activation.search([('code_id', '=', code.id), ('device_id', '=', device.id)], limit=1)
        if already:
            return code, True  # déjà sur ce téléphone : rien à refuser, l'application l'affiche
        if not code.active:
            raise Refusal(_("Ce code IA a été désactivé."), 'code_disabled')
        if code.date_expiry and code.date_expiry < fields.Date.context_today(self):
            raise Refusal(_("Ce code IA a expiré."), 'code_expired')
        # Un code épuisé est refusé avant d'occuper une place d'appareil.
        if code._consumed() >= code.credits:
            raise Refusal(_("Tous les quiz IA de ce code ont été utilisés."), 'code_exhausted')
        holders = Activation.search([('code_id', '=', code.id)])
        if len(holders) >= code.max_devices:
            left = code.transfers_allowed - code.transfer_count
            if not can_transfer:  # application 1.3/1.4 : ancien motif
                raise Refusal(_("Ce code IA est déjà utilisé sur un autre appareil."), 'code_taken')
            if code.max_devices > 1:
                raise Refusal(_("Ce code IA est déjà utilisé sur tous les appareils prévus."), 'code_full')
            if left <= 0:
                raise Refusal(_("Ce code IA est déjà utilisé sur un autre appareil et ne peut plus être transféré."),
                              'code_taken')
            if not transfer:
                raise Refusal(_("Ce code IA est utilisé sur un autre téléphone : le transférer sur celui-ci ?"),
                              'code_transferable', transfers_left=left)
            holders.unlink()
            code.transfer_count += 1
            code.message_post(body=_("Transféré par son détenteur sur un autre appareil (transfert %(n)s sur %(max)s).",
                                     n=code.transfer_count, max=code.transfers_allowed),
                              message_type='notification')
        Activation.create({'code_id': code.id, 'device_id': device.id})
        return code, False

    @api.model
    def _release(self, device, text):
        """L'utilisateur retire un code de son téléphone : la place se libère (pour le donner),
        les quiz déjà faits restent comptés."""
        from .comprendre import Refusal  # noqa: PLC0415
        key = code_key(text)
        activation = self.env['comprendre.ai.code.activation'].sudo().search(
            [('code_id.key', '=', key), ('device_id', '=', device.id)], limit=1) if key else None
        if not activation:
            raise Refusal(_("Ce code IA n'est pas activé sur ce téléphone."), 'code_unknown')
        code = activation.code_id
        activation.unlink()
        code.message_post(body=_("Retiré d'un appareil par son utilisateur : la place est libre."),
                          message_type='notification')
        return code

    # ------------------------------------------------------------------ #
    # Actions des gestionnaires
    # ------------------------------------------------------------------ #
    def action_disable(self):
        self.write({'active': False})

    def action_enable(self):
        self.write({'active': True})

    def action_print_cards(self):
        # Cartes sans en-tête de société : pas besoin de configurer la mise en page d'abord.
        return self.env.ref('comprendre_backend.action_report_code_cards').report_action(self, config=False)

    def action_open_requests(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': _("Quiz IA du code %s", self.code),
            'res_model': 'comprendre.ai.request',
            'view_mode': 'list,pivot,graph',
            'domain': [('code_id', '=', self.id)],
        }

    def action_update(self):
        return {
            'type': 'ir.actions.act_window',
            'name': _("Modifier des codes IA"),
            'res_model': 'comprendre.ai.code.update',
            'view_mode': 'form',
            'target': 'new',
            'context': {'default_code_ids': [(6, 0, self.ids)]},
        }

    def _share_url(self):
        """Page publique du code (QR des cartes) : le code et comment l'utiliser, rien d'autre."""
        self.ensure_one()
        base = self.env['ir.config_parameter'].sudo().get_param('web.base.url', '').rstrip('/')
        return '%s/comprendre/code/%s' % (base, self.code)


class ComprendreAiCodeActivation(models.Model):
    """Un appareil (anonyme) qui a activé un code IA."""
    _name = 'comprendre.ai.code.activation'
    _description = "Activation d'un code IA"
    _order = 'id desc'

    code_id = fields.Many2one('comprendre.ai.code', "Code", required=True, index=True, ondelete='cascade')
    device_id = fields.Many2one('comprendre.device', "Appareil", required=True, index=True, ondelete='cascade')
    create_date = fields.Datetime("Activé le", readonly=True)
    used = fields.Integer("Quiz IA", compute='_compute_used', help="Quiz IA faits sur ce code par cet appareil.")

    _unique = models.Constraint('UNIQUE(code_id, device_id)', "Ce code est déjà activé sur cet appareil.")

    def _compute_used(self):
        for a in self:
            a.used = a.code_id._used_by(a.device_id) if a.code_id and a.device_id else 0

    def action_release(self):
        """Libère la place (changement de téléphone) : les quiz déjà faits restent comptés."""
        for a in self:
            a.code_id.message_post(body=_("Place libérée par un gestionnaire."), message_type='notification')
        self.unlink()


class ComprendreAiCodeBatch(models.Model):
    """Un lot de codes remis ensemble (une classe, une école, un partenaire, une opération)."""
    _name = 'comprendre.ai.code.batch'
    _description = "Lot de codes IA"
    _inherit = ['mail.thread']
    _order = 'id desc'

    name = fields.Char("Nom du lot", required=True, tracking=True, help="Ex. « Lycée Classique — Terminale A ».")
    partner_id = fields.Many2one('res.partner', "Contact", tracking=True)
    note = fields.Text("Notes internes")
    active = fields.Boolean(default=True)
    code_ids = fields.One2many('comprendre.ai.code', 'batch_id', "Codes",
                               context={'active_test': False})
    code_count = fields.Integer("Nombre de codes", compute='_compute_stats')
    activated_count = fields.Integer("Codes activés", compute='_compute_stats')
    credits = fields.Integer("Quiz IA offerts", compute='_compute_stats')
    used = fields.Integer("Utilisés", compute='_compute_stats')
    remaining = fields.Integer("Restants", compute='_compute_stats')
    usage_percent = fields.Float("Utilisation (%)", compute='_compute_stats')
    activation_percent = fields.Float("Codes activés (%)", compute='_compute_stats')
    cost = fields.Float("Coût ($)", digits=(12, 4), compute='_compute_stats')
    date_expiry = fields.Date("Expire le", compute='_compute_stats', help="La date limite la plus proche du lot.")
    last_used = fields.Datetime("Dernier quiz IA", compute='_compute_stats')
    enabled_count = fields.Integer("Codes actifs", compute='_compute_stats')
    disabled_count = fields.Integer("Codes désactivés", compute='_compute_stats')

    @api.depends('code_ids.used', 'code_ids.credits', 'code_ids.device_count', 'code_ids.active',
                 'code_ids.date_expiry')
    def _compute_stats(self):
        for b in self:
            codes = b.with_context(active_test=False).code_ids
            b.code_count = len(codes)
            b.activated_count = len(codes.filtered('device_count'))
            b.credits = sum(codes.mapped('credits'))
            b.used = sum(codes.mapped('used'))
            live = codes.filtered(lambda c: c.state in ('new', 'active'))
            b.remaining = sum(live.mapped('remaining'))
            b.usage_percent = min(100.0, 100.0 * b.used / b.credits) if b.credits else 0.0
            b.activation_percent = 100.0 * b.activated_count / b.code_count if b.code_count else 0.0
            b.cost = sum(codes.mapped('cost'))
            dates = [d for d in codes.filtered('active').mapped('date_expiry') if d]
            b.date_expiry = min(dates) if dates else False
            used_dates = [d for d in codes.mapped('last_used') if d]
            b.last_used = max(used_dates) if used_dates else False
            b.enabled_count = len(codes.filtered('active'))
            b.disabled_count = b.code_count - b.enabled_count

    def _codes(self):
        return self.with_context(active_test=False).code_ids

    def action_open_codes(self):
        self.ensure_one()
        return {
            'type': 'ir.actions.act_window',
            'name': self.name,
            'res_model': 'comprendre.ai.code',
            'view_mode': 'list,form',
            'domain': [('batch_id', '=', self.id)],
            'context': {'active_test': False, 'default_batch_id': self.id},
        }

    def action_print_cards(self):
        codes = self._codes().filtered('active')
        if not codes:
            raise UserError(_("Aucun code actif à imprimer dans ce lot."))
        return codes.action_print_cards()

    def action_update(self):
        return self._codes().action_update()

    def action_disable(self):
        self._codes().filtered('active').action_disable()
        self.message_post(body=_("Tous les codes du lot sont désactivés."), message_type='notification')

    def action_enable(self):
        self._codes().filtered(lambda c: not c.active).action_enable()
        self.message_post(body=_("Tous les codes du lot sont réactivés."), message_type='notification')


class ComprendreAiCodeGenerate(models.TransientModel):
    """Crée plusieurs codes IA d'un coup, réunis dans un lot (une classe, un partenaire…)."""
    _name = 'comprendre.ai.code.generate'
    _description = "Créer des codes IA"

    quantity = fields.Integer("Nombre de codes", required=True, default=10)
    batch_name = fields.Char("Nom du lot", help="Ex. « Lycée Classique — Terminale A ». Vide pour un seul code sans lot.")
    batch_id = fields.Many2one('comprendre.ai.code.batch', "Ajouter au lot",
                               help="Ajouter les codes à un lot existant plutôt que d'en créer un.")
    partner_id = fields.Many2one('res.partner', "Contact")
    name = fields.Char("Bénéficiaire", help="Note commune à tous les codes.")
    prefix = fields.Char("Préfixe", size=6,
                         help="Facultatif, 2 à 6 lettres ou chiffres (ex. LYC → LYC-7KQ3-M9PX) : "
                              "on reconnaît les codes d'un même lot.")
    credits = fields.Integer("Quiz IA offerts par code", required=True, default=20)
    daily_limit = fields.Integer("Plafond par jour", default=0, help="0 : pas de plafond.")
    max_devices = fields.Integer("Appareils par code", required=True, default=1,
                                 help="1 : un code par personne. Plus : un code partagé (ex. une classe).")
    expiry_mode = fields.Selection([('none', "Sans limite"), ('days', "Pendant un nombre de jours"),
                                    ('date', "Jusqu'à une date")], string="Validité", required=True, default='days')
    validity_days = fields.Integer("Nombre de jours", default=90)
    date_expiry = fields.Date("Jusqu'au")
    preview = fields.Char("Aperçu", compute='_compute_preview')

    @api.depends('quantity', 'credits', 'max_devices', 'expiry_mode', 'validity_days', 'date_expiry', 'prefix')
    def _compute_preview(self):
        for w in self:
            expiry = w._expiry()
            w.preview = _(
                "%(n)s code(s) comme %(sample)s : %(credits)s quiz IA chacun (%(total)s au total), %(devices)s, %(until)s.",
                n=w.quantity, sample=new_code(clean_prefix(w.prefix)) if w.prefix else 'K7QM-3XPA',
                credits=w.credits, total=w.quantity * w.credits,
                devices=_("un téléphone par code") if w.max_devices <= 1 else _("%s téléphones par code", w.max_devices),
                until=_("valables jusqu'au %s", expiry.strftime('%d/%m/%Y')) if expiry else _("sans limite de date"))

    def _expiry(self):
        if self.expiry_mode == 'days' and self.validity_days > 0:
            return fields.Date.context_today(self) + relativedelta(days=self.validity_days - 1)
        if self.expiry_mode == 'date':
            return self.date_expiry
        return False

    def action_generate(self):
        self.ensure_one()
        if not 1 <= self.quantity <= 1000:
            raise UserError(_("De 1 à 1000 codes à la fois."))
        if self.credits <= 0:
            raise UserError(_("Chaque code doit offrir au moins un quiz IA."))
        expiry = self._expiry()
        if self.expiry_mode == 'date' and not expiry:
            raise UserError(_("Indiquez la date limite."))
        if expiry and expiry < fields.Date.context_today(self):
            raise UserError(_("La date limite est déjà passée."))
        prefix = clean_prefix(self.prefix)
        if self.prefix and len(prefix) < 2:
            raise UserError(_("Le préfixe doit avoir de 2 à 6 lettres ou chiffres."))
        batch = self.batch_id
        if not batch and (self.batch_name or self.quantity > 1):
            batch = self.env['comprendre.ai.code.batch'].create({
                'name': self.batch_name or self.name or _("Lot du %s", fields.Date.context_today(self).strftime('%d/%m/%Y')),
                'partner_id': self.partner_id.id,
            })
        Code = self.env['comprendre.ai.code']
        codes = Code.create([{
            'code': Code._unique_code(prefix),
            'name': self.name or False, 'partner_id': self.partner_id.id,
            'batch_id': batch.id, 'credits': self.credits, 'daily_limit': self.daily_limit,
            'max_devices': max(1, self.max_devices), 'date_expiry': expiry,
        } for _i in range(self.quantity)])
        if batch:
            batch.message_post(body=_("%(n)s codes ajoutés (%(credits)s quiz IA chacun).",
                                      n=len(codes), credits=self.credits), message_type='notification')
            return {'type': 'ir.actions.act_window', 'res_model': 'comprendre.ai.code.batch', 'res_id': batch.id,
                    'view_mode': 'form', 'target': 'current'}
        return {'type': 'ir.actions.act_window', 'res_model': 'comprendre.ai.code', 'res_id': codes.id,
                'view_mode': 'form', 'target': 'current'}


class ComprendreAiCodeUpdate(models.TransientModel):
    """Modifie plusieurs codes d'un coup : ajouter des quiz, prolonger, changer les plafonds, (dés)activer."""
    _name = 'comprendre.ai.code.update'
    _description = "Modifier des codes IA"

    code_ids = fields.Many2many('comprendre.ai.code', string="Codes", context={'active_test': False})
    code_count = fields.Integer("Codes concernés", compute='_compute_count')
    add_credits = fields.Integer("Ajouter des quiz IA", default=0, help="Par code (négatif pour en retirer).")
    expiry_action = fields.Selection([('keep', "Ne pas changer"), ('extend', "Prolonger de… jours"),
                                      ('set', "Fixer la date"), ('remove', "Sans limite de date")],
                                     string="Date limite", required=True, default='keep')
    extend_days = fields.Integer("Jours en plus", default=30)
    date_expiry = fields.Date("Nouvelle date")
    change_daily = fields.Boolean("Changer le plafond par jour")
    daily_limit = fields.Integer("Plafond par jour", default=0)
    change_devices = fields.Boolean("Changer le nombre d'appareils")
    max_devices = fields.Integer("Appareils par code", default=1)
    reset_transfers = fields.Boolean("Remettre les transferts à zéro",
                                     help="Le détenteur peut de nouveau transférer son code lui-même.")
    activity = fields.Selection([('keep', "Ne pas changer"), ('enable', "Réactiver"), ('disable', "Désactiver")],
                                string="Activation", required=True, default='keep')

    @api.model
    def default_get(self, fields_list):
        res = super().default_get(fields_list)
        if 'code_ids' in fields_list and not res.get('code_ids') and self.env.context.get('active_model') == \
                'comprendre.ai.code':
            res['code_ids'] = [(6, 0, self.env.context.get('active_ids') or [])]
        return res

    @api.depends('code_ids')
    def _compute_count(self):
        for w in self:
            w.code_count = len(w.code_ids)

    def action_apply(self):
        self.ensure_one()
        codes = self.code_ids.with_context(active_test=False)
        if not codes:
            raise UserError(_("Aucun code sélectionné."))
        today = fields.Date.context_today(self)
        if self.expiry_action == 'set' and not self.date_expiry:
            raise UserError(_("Indiquez la nouvelle date."))
        if self.expiry_action == 'set' and self.date_expiry < today:
            raise UserError(_("La nouvelle date est déjà passée."))
        if self.expiry_action == 'extend' and self.extend_days <= 0:
            raise UserError(_("Indiquez un nombre de jours positif."))
        for code in codes:
            values = {}
            if self.add_credits:
                values['credits'] = max(code.used, code.credits + self.add_credits)
            if self.expiry_action == 'extend' and code.date_expiry:
                # Un code expiré repart d'aujourd'hui : prolonger doit le rendre utilisable.
                values['date_expiry'] = max(code.date_expiry, today - relativedelta(days=1)) + relativedelta(
                    days=self.extend_days)
            elif self.expiry_action == 'set':
                values['date_expiry'] = self.date_expiry
            elif self.expiry_action == 'remove':
                values['date_expiry'] = False
            if self.change_daily:
                values['daily_limit'] = max(0, self.daily_limit)
            if self.change_devices:
                values['max_devices'] = max(1, self.max_devices, len(code.activation_ids))
            if self.reset_transfers:
                values['transfer_count'] = 0
            if self.activity != 'keep':
                values['active'] = self.activity == 'enable'
            if values:
                code.write(values)
        return {'type': 'ir.actions.client', 'tag': 'display_notification',
                'params': {'type': 'success', 'message': _("%s code(s) modifié(s).", len(codes)),
                           'next': {'type': 'ir.actions.act_window_close'}}}
