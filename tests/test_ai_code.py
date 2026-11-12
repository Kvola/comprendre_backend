# -*- coding: utf-8 -*-
import json
from datetime import timedelta
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError, ValidationError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.ai_code import CODE_ALPHABET, code_key
from .test_comprendre import TEXT, fake_quiz


@tagged('post_install', '-at_install')
class TestAiCode(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Req = self.env['comprendre.ai.request']
        self.Code = self.env['comprendre.ai.code']
        self.Device = self.env['comprendre.device']
        self.phone = self.Device._for_key('phone-0123456789abcdef')
        self.other = self.Device._for_key('phone-fedcba9876543210')
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_quota', 1)

    def _patch(self, data=None, error=None):
        def call(_self, text, lang, count):
            if error:
                raise UserError(error)
            return data or fake_quiz(), 1000, 500, 'claude-opus-5-5'
        return patch.object(type(self.Req), '_call_claude', call)

    def quiz(self, device, ip='ip1'):
        with self._patch():
            return self.Req._generate(device, ip, TEXT, 'fr', 10)

    # -- Codes -------------------------------------------------------------
    def test_generated_codes(self):
        codes = self.Code.create([{} for _i in range(50)])
        self.assertEqual(len(set(codes.mapped('key'))), 50)
        for c in codes:
            self.assertRegex(c.code, r'^[%s]{4}-[%s]{4}$' % (CODE_ALPHABET, CODE_ALPHABET))
            self.assertEqual(c.state, 'new')
        self.assertEqual(code_key(' ab-cd ef12 '), 'ABCDEF12')
        with self.assertRaises(ValidationError):
            self.Code.create({'code': 'AB-12'})
        self.Code.create({'code': 'ECOLE-2026'})
        with self.assertRaises(ValidationError):
            self.Code.create({'code': 'ecole 2026'})  # même clé : message clair, pas une erreur de la base

    def test_redeem_refusals(self):
        def reason(text, device=None):
            with self.assertRaises(UserError) as ctx:
                self.Code._redeem(device or self.phone, text)
            return ctx.exception.reason
        self.assertEqual(reason('NOPE-NOPE'), 'code_unknown')
        self.assertEqual(reason(''), 'code_unknown')
        self.Code.create({'code': 'OFF-CODE1', 'active': False})
        self.assertEqual(reason('off-code1'), 'code_disabled')
        self.Code.create({'code': 'OLD-CODE1', 'date_expiry': fields.Date.today() - timedelta(days=1)})
        self.assertEqual(reason('OLD-CODE1'), 'code_expired')
        self.Code.create({'code': 'ZERO-CODE', 'credits': 0})
        self.assertEqual(reason('ZERO-CODE'), 'code_exhausted')
        self.assertFalse(self.env['comprendre.ai.code.activation'].search([]), "un code refusé n'occupe aucune place")

    def test_redeem_binds_one_device(self):
        code = self.Code.create({'credits': 3})
        self.assertEqual(self.Code._redeem(self.phone, code.code.lower().replace('-', ' ')), code)
        self.assertEqual(self.Code._redeem(self.phone, code.code), code, "le même appareil peut le saisir à nouveau")
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.other, code.code)
        self.assertEqual(ctx.exception.reason, 'code_taken')
        # Changement de téléphone : le gestionnaire libère la place.
        code.activation_ids.action_release()
        self.assertEqual(self.Code._redeem(self.other, code.code), code)

    # -- Consommation --------------------------------------------------------
    def test_free_quota_first_then_code(self):
        code = self.Code.create({'credits': 2})
        self.Code._redeem(self.phone, code.code)
        info = self.Req._quota_info(self.phone)
        self.assertEqual((info['remaining'], info['free_remaining'], info['code_remaining']), (3, 1, 2))
        self.assertEqual(info['codes'][0]['code'], code.code)
        r1 = self.quiz(self.phone)
        self.assertFalse(r1['from_code'], "le quota gratuit passe d'abord")
        self.assertEqual(r1['remaining'], 2)
        r2 = self.quiz(self.phone)
        self.assertTrue(r2['from_code'])
        self.assertEqual((r2['remaining'], code.remaining, code.used), (1, 1, 1))
        self.quiz(self.phone)
        self.assertEqual((code.remaining, code.state), (0, 'exhausted'))
        with self._patch(), self.assertRaises(UserError) as ctx:
            self.Req._generate(self.phone, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(ctx.exception.reason, 'quota')
        self.assertAlmostEqual(code.cost, 2 * (1000 * 4 + 500 * 20) / 1e6, places=6)

    def test_failures_do_not_consume_credits(self):
        code = self.Code.create({'credits': 1})
        self.Code._redeem(self.phone, code.code)
        self.quiz(self.phone)  # quota gratuit
        with self._patch(error="indisponible"):
            self.Req._generate(self.phone, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(code.remaining, 1, "un échec du service ne coûte rien au code")
        self.assertTrue(self.quiz(self.phone)['from_code'])

    def test_daily_limit_and_shared_code(self):
        code = self.Code.create({'credits': 10, 'daily_limit': 1, 'max_devices': 2})
        self.Code._redeem(self.phone, code.code)
        self.Code._redeem(self.other, code.code)
        self.quiz(self.phone)  # gratuit
        self.assertTrue(self.quiz(self.phone)['from_code'])
        self.assertEqual(self.Req._quota_info(self.phone)['code_remaining'], 0, "plafond du jour atteint pour ce téléphone")
        with self._patch(), self.assertRaises(UserError):
            self.Req._generate(self.phone, 'ip1', TEXT, 'fr', 10)
        # L'autre téléphone partage les crédits du code.
        self.quiz(self.other)
        self.assertTrue(self.quiz(self.other)['from_code'])
        self.assertEqual(code.remaining, 8)

    def test_code_expiring_first_is_used_first(self):
        later = self.Code.create({'credits': 5, 'date_expiry': fields.Date.today() + timedelta(days=30)})
        sooner = self.Code.create({'credits': 5, 'date_expiry': fields.Date.today() + timedelta(days=2)})
        forever = self.Code.create({'credits': 5})
        for c in (later, forever, sooner):
            self.Code._redeem(self.phone, c.code)
        self.quiz(self.phone)
        self.quiz(self.phone)
        self.assertEqual((sooner.used, later.used, forever.used), (1, 0, 0))
        self.assertEqual([c['code'] for c in self.Req._quota_info(self.phone)['codes']],
                         [sooner.code, later.code, forever.code])

    def test_disabled_code_stops_immediately(self):
        code = self.Code.create({'credits': 5})
        self.Code._redeem(self.phone, code.code)
        code.action_disable()
        self.quiz(self.phone)
        with self._patch(), self.assertRaises(UserError):
            self.Req._generate(self.phone, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(self.Req._quota_info(self.phone)['codes'], [])

    def test_network_cap_and_codes(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.ip_daily_cap', 1)
        code = self.Code.create({'credits': 5})
        self.Code._redeem(self.phone, code.code)
        self.quiz(self.phone, ip='school')  # gratuit : atteint le plafond du réseau
        self.assertTrue(self.quiz(self.phone, ip='school')['from_code'], "un code n'est pas bloqué par le plafond réseau")
        third = self.Device._for_key('phone-aaaaaaaaaaaaaaaa')
        with self._patch(), self.assertRaises(UserError) as ctx:
            self.Req._generate(third, 'school', TEXT, 'fr', 10)
        self.assertEqual(ctx.exception.reason, 'network_quota')
        # Les quiz payés par le code ne comptent pas dans ce plafond.
        self.assertEqual(self.Req._count_today([('ip_hash', '=', 'school'), ('code_id', '=', False)], ['done']), 1)

    def test_generate_wizard(self):
        wizard = self.env['comprendre.ai.code.generate'].create(
            {'quantity': 5, 'name': 'Lycée moderne', 'credits': 30, 'max_devices': 1})
        action = wizard.action_generate()
        codes = self.Code.search(action['domain'])
        self.assertEqual(len(codes), 5)
        self.assertEqual(set(codes.mapped('name')), {'Lycée moderne'})
        self.assertEqual(set(codes.mapped('credits')), {30})
        with self.assertRaises(UserError):
            self.env['comprendre.ai.code.generate'].create({'quantity': 0}).action_generate()


@tagged('post_install', '-at_install')
class TestAiCodeApi(HttpCase):

    def rpc(self, path, **params):
        res = self.url_open('/comprendre/api/v1' + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 200)
        return res.json()['result']

    def test_redeem_then_quota(self):
        code = self.env['comprendre.ai.code'].create({'credits': 7, 'name': 'Test'})
        r = self.rpc('/redeem', device_id='api-device-code-0123456', code=code.code.lower())
        self.assertTrue(r['success'], r)
        self.assertEqual(r['redeemed']['code'], code.code)
        self.assertEqual(r['code_remaining'], 7)
        q = self.rpc('/quota', device_id='api-device-code-0123456')
        self.assertEqual((q['code_remaining'], q['codes'][0]['remaining']), (7, 7))
        bad = self.rpc('/redeem', device_id='api-device-code-0123456', code='XXXX-XXXX')
        self.assertEqual(bad['error']['details']['reason'], 'code_unknown')

    def test_redeem_is_rate_limited(self):
        reasons = [self.rpc('/redeem', device_id='api-device-guess-0123456', code='GUES-S%03d' % i)
                   for i in range(11)]
        self.assertEqual(reasons[0]['error']['details']['reason'], 'code_unknown')
        self.assertEqual(reasons[-1]['error']['code'], 'AUTH_010')
