# -*- coding: utf-8 -*-
from datetime import timedelta

from odoo import fields
from odoo.exceptions import AccessError
from odoo.tests import TransactionCase, new_test_user, tagged

from .test_comprendre import TEXT, fake_quiz
from unittest.mock import patch


@tagged('post_install', '-at_install')
class TestDashboard(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Req = self.env['comprendre.ai.request']
        self.Code = self.env['comprendre.ai.code']
        self.phone = self.env['comprendre.device']._for_key('dash-0123456789abcdef')
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_quota', 1)
        self.env['ir.config_parameter'].sudo().set_param('comprendre.usd_xof_rate', '500')
        xof = self.env.ref('base.XOF')
        xof.active = True
        self.env['comprendre.ai.order']._selling_company().currency_id = xof

    def quiz(self):
        def call(_self, text, lang, count):
            return fake_quiz(), 1000, 500, 'claude-opus-5-5'
        with patch.object(type(self.Req), '_call_claude', call):
            return self.Req._generate(self.phone, 'ip', TEXT, 'fr', 10)

    def test_numbers(self):
        soon = self.Code.create({'credits': 5, 'date_expiry': fields.Date.today() + timedelta(days=3), 'name': 'Awa'})
        self.Code.create({'credits': 5})
        self.Code._redeem(self.phone, soon.code)
        self.quiz()  # gratuit
        self.quiz()  # code
        data = self.env['comprendre.dashboard'].get_data(7)
        self.assertEqual(data['days'], 7)
        self.assertEqual(len(data['series']), 7)
        self.assertEqual((data['series'][-1]['free'], data['series'][-1]['coded']), (1, 1))
        u = data['usage']
        self.assertEqual((u['done'], u['free'], u['coded'], u['done_today'], u['devices']), (2, 1, 1, 2, 1))
        self.assertAlmostEqual(u['cost'], 2 * 0.014, places=6)
        self.assertAlmostEqual(u['cost_per_quiz'], 0.014, places=6)
        c = data['codes']
        self.assertGreaterEqual(c['new'], 1)
        self.assertGreaterEqual(c['active'], 1)
        self.assertEqual(c['expiring'][0]['code'], soon.code)
        self.assertEqual((c['expiring'][0]['days_left'], c['expiring'][0]['remaining']), (3, 4))
        # Chaque chiffre ouvre la bonne liste.
        found = self.Code.search(c['domains']['expiring'])
        self.assertIn(soon, found)
        self.assertEqual(data['rate'], 500.0)
        self.assertEqual(data['sales']['currency'], 'XOF')
        self.env['comprendre.ai.order']._selling_company().currency_id = self.env.ref('base.USD')
        self.assertEqual(self.env['comprendre.dashboard'].get_data(7)['rate'], 1.0, "en dollars : rien à convertir")
        self.assertEqual(self.env['comprendre.dashboard'].get_data(12345)['days'], 30, "période inconnue : 30 jours")

    def test_alerts_and_access(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.enabled', '0')
        texts = [a['text'] for a in self.env['comprendre.dashboard'].get_data(30)['alerts']]
        self.assertTrue(any('désactivé' in t for t in texts))
        user = new_test_user(self.env, login='dash-nobody', groups='base.group_user')
        with self.assertRaises(AccessError):
            self.env['comprendre.dashboard'].with_user(user).get_data(30)

    def test_settings_roundtrip(self):
        Settings = self.env['res.config.settings']
        s = Settings.create({'comprendre_enabled': False, 'comprendre_daily_quota': 7})
        s.execute()
        Param = self.env['ir.config_parameter'].sudo()
        self.assertEqual((Param.get_param('comprendre.enabled'), Param.get_param('comprendre.daily_quota')), ('0', '7'))
        self.assertFalse(Settings.create({}).comprendre_enabled)
        Settings.create({'comprendre_enabled': True}).execute()
        self.assertTrue(Settings.create({}).comprendre_enabled)
