# -*- coding: utf-8 -*-
"""Réglages de Comprendre dans Paramètres : plus besoin d'aller dans les paramètres système."""
import os

from odoo import api, fields, models


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

    @api.depends('comprendre_api_key')
    def _compute_comprendre_key_from_env(self):
        for s in self:
            s.comprendre_key_from_env = bool(os.environ.get('ANTHROPIC_API_KEY'))

    @api.model
    def get_values(self):
        res = super().get_values()
        value = self.env['ir.config_parameter'].sudo().get_param('comprendre.enabled', '1')
        res['comprendre_enabled'] = value not in ('0', 'False', 'false')
        return res

    def set_values(self):
        super().set_values()
        self.env['ir.config_parameter'].sudo().set_param('comprendre.enabled', '1' if self.comprendre_enabled else '0')
