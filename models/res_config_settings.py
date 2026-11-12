# -*- coding: utf-8 -*-
"""Réglages de Comprendre dans Paramètres : plus besoin d'aller dans les paramètres système."""
import os
from html import escape

from odoo import _, api, fields, models
from odoo.exceptions import ValidationError

from .ai_purchase import normalize_wave_link


class ResConfigSettings(models.TransientModel):
    _inherit = 'res.config.settings'

    comprendre_enabled = fields.Boolean("Service de quiz IA ouvert")
    comprendre_api_key = fields.Char("Clé API Anthropic", config_parameter='comprendre.anthropic_api_key')
    comprendre_key_from_env = fields.Boolean(compute='_compute_comprendre_key_from_env')
    comprendre_model = fields.Char("Modèle Claude", config_parameter='comprendre.model', default='claude-opus-5-5')
    comprendre_daily_quota = fields.Integer("Quiz IA gratuits par jour", config_parameter='comprendre.daily_quota',
                                            default=5)
    comprendre_ip_cap = fields.Integer("Plafond par réseau et par jour", config_parameter='comprendre.ip_daily_cap',
                                       default=40)
    comprendre_rejected_cap = fields.Integer("Échecs facturés permis par jour",
                                             config_parameter='comprendre.daily_rejected_cap', default=3)
    comprendre_max_chars = fields.Integer("Longueur maximale d'un texte", config_parameter='comprendre.max_chars',
                                          default=30000)
    comprendre_usd_rate = fields.Float("Taux du dollar (F CFA)", config_parameter='comprendre.usd_xof_rate', default=600.0)
    # Encaissement des achats de quiz IA
    comprendre_company_id = fields.Many2one('res.company', "Société qui encaisse")
    comprendre_wave_link = fields.Char("Lien marchand Wave de Comprendre")
    comprendre_collection = fields.Html("Encaissement", compute='_compute_comprendre_collection', sanitize=False)

    @api.depends('comprendre_api_key')
    def _compute_comprendre_key_from_env(self):
        for s in self:
            s.comprendre_key_from_env = bool(os.environ.get('ANTHROPIC_API_KEY'))

    @api.model
    def get_values(self):
        res = super().get_values()
        Param = self.env['ir.config_parameter'].sudo()
        value = Param.get_param('comprendre.enabled', '1')
        res['comprendre_enabled'] = value not in ('0', 'False', 'false')
        res['comprendre_company_id'] = self.env['comprendre.ai.order']._selling_company().id
        res['comprendre_wave_link'] = Param.get_param('comprendre.wave_link') or False
        return res

    @api.onchange('comprendre_wave_link')
    def _onchange_comprendre_wave_link(self):
        if self.comprendre_wave_link:
            try:
                self.comprendre_wave_link = normalize_wave_link(self.comprendre_wave_link) or False
            except ValueError:
                return {'warning': {'title': _("Lien Wave"), 'message': _(
                    "Ce n'est pas un lien marchand Wave. Il ressemble à https://pay.wave.com/m/M_ci_xxxx/c/ci/ "
                    "(Wave Business → Encaisser → Partager le lien).")}}

    def set_values(self):
        super().set_values()
        Param = self.env['ir.config_parameter'].sudo()
        Param.set_param('comprendre.enabled', '1' if self.comprendre_enabled else '0')
        try:
            link = normalize_wave_link(self.comprendre_wave_link)
        except ValueError:
            raise ValidationError(_("« %s » n'est pas un lien marchand Wave (https://pay.wave.com/m/…).",
                                    self.comprendre_wave_link)) from None
        Param.set_param('comprendre.wave_link', link or False)
        main = self.env.ref('base.main_company')
        company = self.comprendre_company_id
        Param.set_param('comprendre.company_id', str(company.id) if company and company != main else False)

    def _comprendre_link(self):
        """(lien, origine, compte de l'opérateur ?) d'après ce qui est saisi dans le formulaire."""
        self.ensure_one()
        Wave = self.env['queue.wave.mixin'].sudo()
        company = self.comprendre_company_id or self.env['comprendre.ai.order']._selling_company()
        try:
            own = normalize_wave_link(self.comprendre_wave_link)
        except ValueError:
            own = ''
        if own:
            return own, _("le lien de Comprendre ci-dessus"), False
        if (company.wave_payment_link or '').strip():
            return company.wave_payment_link.strip(), _("le lien de la société %s", company.name), False
        if Wave._wave_default_link():
            return Wave._wave_default_link(), _("le lien par défaut de la plateforme"), False
        return (Wave._wave_operator_link(company),
                _("le compte Wave de l'opérateur de la plateforme (aucun autre lien n'est réglé)"), True)

    @api.depends('comprendre_wave_link', 'comprendre_company_id')
    def _compute_comprendre_collection(self):
        Wave = self.env['queue.wave.mixin'].sudo()
        for s in self:
            company = s.comprendre_company_id or self.env['comprendre.ai.order']._selling_company()
            link, source, operator = s._comprendre_link()
            confirm = (_("<b>automatique</b> : l'API Wave de la société confirme chaque paiement.")
                       if Wave._wave_api_configured(company)
                       else _("<b>par un agent</b>, dans Ventes de quiz IA → Achats à valider, après avoir vu "
                              "l'argent arriver."))
            warn = _("<p class='text-warning mb-1'>Réglez votre propre lien pour encaisser sur votre compte.</p>") \
                if operator else ''
            s.comprendre_collection = _(
                "<p class='mb-1'>Les achats sont payés sur <b>%(source)s</b> :<br/><code>%(link)s</code></p>"
                "<p class='mb-1'>Confirmation %(confirm)s</p>%(warn)s",
                source=escape(source), link=escape(link), confirm=confirm, warn=warn)

    def action_comprendre_open_wave_link(self):
        """Ouvre la page Wave du lien (100 F) : on vérifie le nom du marchand, sans payer."""
        self.ensure_one()
        link, _source, _operator = self._comprendre_link()
        url = self.env['queue.wave.mixin'].sudo()._wave_url_from_link(link, 100)
        return {'type': 'ir.actions.act_url', 'url': url, 'target': 'new'}
