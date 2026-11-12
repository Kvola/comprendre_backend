# -*- coding: utf-8 -*-
"""Codes IA : un code unique, remis à une personne ou à un groupe, qui donne un crédit
de quiz IA en plus du quota gratuit du jour.

- Le code est saisi dans l'application ; il est alors **lié à l'appareil** (anonyme).
  ``max_devices`` (1 par défaut) borne le nombre d'appareils qui peuvent l'utiliser :
  un code de classe peut servir à 30 téléphones et partager leurs ``credits``.
- Chaque quiz IA réussi au-delà du quota gratuit consomme 1 crédit (une demande en
  cours compte déjà, comme pour le quota gratuit).
- Facultatif : date limite, plafond de quiz par jour et par appareil.
"""
import re
import secrets

from odoo import _, api, fields, models
from odoo.exceptions import UserError, ValidationError

# Sans 0/O, 1/I/L : un code se dicte et se recopie sans erreur.
CODE_ALPHABET = '23456789ABCDEFGHJKMNPQRSTUVWXYZ'
CODE_LENGTH = 8


def code_key(text):
    """Forme de comparaison d'un code : majuscules, sans espaces ni tirets (« ia-7kq3 m9px » = « IA7KQ3M9PX »)."""
    return re.sub(r'[^A-Z0-9]', '', (text or '').upper())


def new_code():
    """« 7KQ3-M9PX » : 8 caractères tirés au hasard (40 bits), en deux groupes."""
    raw = ''.join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))
    return '%s-%s' % (raw[:4], raw[4:])


class ComprendreAiCode(models.Model):
    _name = 'comprendre.ai.code'
    _description = "Code IA"
    _order = 'create_date desc, id desc'
    _rec_name = 'code'

    code = fields.Char("Code", required=True, copy=False, default=lambda self: self._unique_code(),
                       help="Saisi dans l'application (majuscules, espaces et tirets indifférents).")
    key = fields.Char("Clé", compute='_compute_key', store=True, index=True, readonly=True)
    name = fields.Char("Bénéficiaire", help="Pour qui est ce code (personne, école, partenaire…) : note interne.")
    credits = fields.Integer("Quiz IA offerts", required=True, default=20,
                             help="Nombre total de quiz IA, partagé entre les appareils du code.")
    daily_limit = fields.Integer("Plafond par jour", default=0,
                                 help="Quiz IA par appareil et par jour sur ce code (0 : pas de plafond).")
    max_devices = fields.Integer("Appareils", required=True, default=1,
                                 help="Nombre d'appareils qui peuvent activer ce code.")
    date_expiry = fields.Date("Valable jusqu'au", help="Vide : sans limite de date.")
    active = fields.Boolean(default=True)
    activation_ids = fields.One2many('comprendre.ai.code.activation', 'code_id', "Appareils activés")
    request_ids = fields.One2many('comprendre.ai.request', 'code_id', "Quiz IA")
    used = fields.Integer("Utilisés", compute='_compute_usage')
    remaining = fields.Integer("Restants", compute='_compute_usage')
    device_count = fields.Integer("Nombre d'appareils", compute='_compute_usage')
    cost = fields.Float("Coût ($)", digits=(12, 4), compute='_compute_usage')
    state = fields.Selection([('new', "Pas encore utilisé"), ('active', "En cours"), ('exhausted', "Épuisé"),
                              ('expired', "Expiré"), ('disabled', "Désactivé")],
                             string="État", compute='_compute_usage')

    _key_unique = models.Constraint('UNIQUE(key)', "Ce code existe déjà.")
    _credits_positive = models.Constraint('CHECK(credits >= 0)', "Le nombre de quiz offerts ne peut pas être négatif.")
    _devices_positive = models.Constraint('CHECK(max_devices >= 1)', "Il faut au moins un appareil.")
    _daily_positive = models.Constraint('CHECK(daily_limit >= 0)', "Le plafond par jour ne peut pas être négatif.")

    @api.model
    def _unique_code(self):
        for _attempt in range(20):
            code = new_code()
            if not self.with_context(active_test=False).search_count([('key', '=', code_key(code))]):
                return code
        raise UserError(_("Impossible de tirer un code libre : réessayez."))

    @api.depends('code')
    def _compute_key(self):
        for c in self:
            c.key = code_key(c.code)

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

    def _consumed(self):
        """Quiz IA comptés sur ce code (réussis ou en cours)."""
        self.ensure_one()
        return self.env['comprendre.ai.request'].sudo().search_count(
            [('code_id', '=', self.id), ('state', 'in', ('done', 'pending'))])

    @api.depends('credits', 'date_expiry', 'active', 'activation_ids', 'request_ids.state', 'request_ids.cost')
    def _compute_usage(self):
        today = fields.Date.context_today(self)
        for c in self:
            requests = c.request_ids
            c.used = len(requests.filtered(lambda r: r.state in ('done', 'pending')))
            c.remaining = max(0, c.credits - c.used)
            c.device_count = len(c.activation_ids)
            c.cost = sum(requests.mapped('cost'))
            if not c.active:
                c.state = 'disabled'
            elif c.date_expiry and c.date_expiry < today:
                c.state = 'expired'
            elif c.remaining <= 0:
                c.state = 'exhausted'
            else:
                c.state = 'active' if c.used or c.activation_ids else 'new'

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

    def _usable_by(self, device):
        """Ce code peut-il payer un quiz de cet appareil aujourd'hui ?"""
        self.ensure_one()
        if not self._valid_today():
            return False
        return not self.daily_limit or self._used_today_by(device) < self.daily_limit

    def _info(self, device):
        """Ce que l'application affiche pour ce code."""
        self.ensure_one()
        daily_left = max(0, self.daily_limit - self._used_today_by(device)) if self.daily_limit else None
        return {
            'code': self.code,
            'remaining': max(0, self.credits - self._consumed()),
            'credits': self.credits,
            'expires': fields.Date.to_string(self.date_expiry) if self.date_expiry else False,
            'daily_left': daily_left if daily_left is not None else False,
            'usable_today': self._usable_by(device),
        }

    # ------------------------------------------------------------------ #
    @api.model
    def _redeem(self, device, text):
        """Active un code sur un appareil. Lève ``Refusal`` (motif traduit par l'application)."""
        from .comprendre import Refusal  # noqa: PLC0415 — import circulaire évité
        key = code_key(text)
        if len(key) < 6:
            raise Refusal(_("Ce code IA n'existe pas."), 'code_unknown')
        code = self.with_context(active_test=False).search([('key', '=', key)], limit=1)
        if not code:
            raise Refusal(_("Ce code IA n'existe pas."), 'code_unknown')
        # Verrou : deux téléphones qui activent en même temps le dernier « appareil » du code.
        self.env.cr.execute('SELECT id FROM comprendre_ai_code WHERE id = %s FOR UPDATE', [code.id])
        if not code.active:
            raise Refusal(_("Ce code IA a été désactivé."), 'code_disabled')
        if code.date_expiry and code.date_expiry < fields.Date.context_today(self):
            raise Refusal(_("Ce code IA a expiré."), 'code_expired')
        # Un code épuisé est refusé avant d'occuper une place d'appareil.
        if code._consumed() >= code.credits:
            raise Refusal(_("Tous les quiz IA de ce code ont été utilisés."), 'code_exhausted')
        Activation = self.env['comprendre.ai.code.activation'].sudo()
        already = Activation.search([('code_id', '=', code.id), ('device_id', '=', device.id)], limit=1)
        if not already:
            if Activation.search_count([('code_id', '=', code.id)]) >= code.max_devices:
                raise Refusal(_("Ce code IA est déjà utilisé sur un autre appareil."), 'code_taken')
            Activation.create({'code_id': code.id, 'device_id': device.id})
        return code

    def action_disable(self):
        self.write({'active': False})

    def action_enable(self):
        self.write({'active': True})


class ComprendreAiCodeActivation(models.Model):
    """Un appareil (anonyme) qui a activé un code IA."""
    _name = 'comprendre.ai.code.activation'
    _description = "Activation d'un code IA"
    _order = 'id desc'

    code_id = fields.Many2one('comprendre.ai.code', required=True, index=True, ondelete='cascade')
    device_id = fields.Many2one('comprendre.device', required=True, index=True, ondelete='cascade')
    create_date = fields.Datetime("Activé le", readonly=True)
    used = fields.Integer("Quiz IA sur ce code", compute='_compute_used')

    _unique = models.Constraint('UNIQUE(code_id, device_id)', "Ce code est déjà activé sur cet appareil.")

    def _compute_used(self):
        Request = self.env['comprendre.ai.request'].sudo()
        for a in self:
            a.used = Request.search_count([('code_id', '=', a.code_id.id), ('device_id', '=', a.device_id.id),
                                           ('state', 'in', ('done', 'pending'))])

    def action_release(self):
        """Libère la place (changement de téléphone) : les quiz déjà faits restent comptés."""
        self.unlink()


class ComprendreAiCodeGenerate(models.TransientModel):
    """Crée plusieurs codes IA d'un coup (une classe, un partenaire…)."""
    _name = 'comprendre.ai.code.generate'
    _description = "Créer des codes IA"

    quantity = fields.Integer("Nombre de codes", required=True, default=10)
    name = fields.Char("Bénéficiaire", help="Note commune à tous les codes (école, partenaire…).")
    credits = fields.Integer("Quiz IA offerts par code", required=True, default=20)
    daily_limit = fields.Integer("Plafond par jour", default=0)
    max_devices = fields.Integer("Appareils par code", required=True, default=1)
    date_expiry = fields.Date("Valables jusqu'au")

    def action_generate(self):
        self.ensure_one()
        if not 1 <= self.quantity <= 1000:
            raise UserError(_("De 1 à 1000 codes à la fois."))
        Code = self.env['comprendre.ai.code']
        codes = Code.create([{
            'name': self.name, 'credits': self.credits, 'daily_limit': self.daily_limit,
            'max_devices': self.max_devices, 'date_expiry': self.date_expiry,
        } for _i in range(self.quantity)])
        return {
            'type': 'ir.actions.act_window',
            'name': _("Codes IA créés"),
            'res_model': 'comprendre.ai.code',
            'view_mode': 'list,form',
            'domain': [('id', 'in', codes.ids)],
        }
