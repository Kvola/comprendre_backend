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
        self.assertEqual(self.Code._redeem(self.phone, code.code.lower().replace('-', ' ')), (code, False))
        self.assertEqual(self.Code._redeem(self.phone, code.code), (code, True), "le même appareil peut le saisir à nouveau")
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.other, code.code)
        self.assertEqual(ctx.exception.reason, 'code_taken')
        # Changement de téléphone : le gestionnaire libère la place.
        code.activation_ids.action_release()
        self.assertEqual(self.Code._redeem(self.other, code.code), (code, False))

    def test_transfer_by_holder(self):
        code = self.Code.create({'credits': 5, 'transfers_allowed': 1})
        self.Code._redeem(self.phone, code.code)
        self.quiz(self.phone)  # gratuit
        self.quiz(self.phone)  # sur le code
        # Une application 1.4 ne sait pas transférer : ancien motif.
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.other, code.code)
        self.assertEqual(ctx.exception.reason, 'code_taken')
        # 1.5 : on propose, sans rien changer tant que ce n'est pas confirmé.
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.other, code.code, can_transfer=True)
        self.assertEqual((ctx.exception.reason, ctx.exception.details), ('code_transferable', {'transfers_left': 1}))
        self.assertEqual(code.activation_ids.device_id, self.phone)
        self.assertEqual(self.Code._redeem(self.other, code.code, transfer=True, can_transfer=True), (code, False))
        self.assertEqual(code.activation_ids.device_id, self.other)
        self.assertEqual((code.transfer_count, code.used, code.remaining), (1, 1, 4), "le crédit suit le code")
        self.assertEqual(self.Req._quota_info(self.phone)['codes'], [], "l'ancien téléphone n'a plus le code")
        self.assertEqual(self.Req._quota_info(self.other)['all_codes'][0]['used_by_me'], 0)
        # Plus de transfert permis : refus net.
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.phone, code.code, transfer=True, can_transfer=True)
        self.assertEqual(ctx.exception.reason, 'code_taken')
        # Un code partagé (classe) ne se transfère pas : il est plein.
        shared = self.Code.create({'credits': 5, 'max_devices': 1})
        shared.max_devices = 2
        self.Code._redeem(self.phone, shared.code)
        self.Code._redeem(self.other, shared.code)
        with self.assertRaises(UserError) as ctx:
            self.Code._redeem(self.Device._for_key('phone-bbbbbbbbbbbbbbbb'), shared.code, transfer=True,
                              can_transfer=True)
        self.assertEqual(ctx.exception.reason, 'code_full')
        with self.assertRaises(ValidationError):
            shared.max_devices = 1  # deux téléphones l'ont déjà

    def test_release_by_user(self):
        code = self.Code.create({'credits': 5})
        self.Code._redeem(self.phone, code.code)
        self.quiz(self.phone)
        self.quiz(self.phone)
        with self.assertRaises(UserError):
            self.Code._release(self.other, code.code)  # pas le sien
        self.Code._release(self.phone, code.code.lower())
        self.assertFalse(code.activation_ids)
        self.assertEqual(code.remaining, 4, "le crédit reste sur le code")
        self.assertEqual(self.Code._redeem(self.other, code.code), (code, False), "la place est libre : on peut le donner")

    def test_all_codes_lists_ended_ones(self):
        live = self.Code.create({'credits': 5})
        off = self.Code.create({'credits': 5})
        for c in (live, off):
            self.Code._redeem(self.phone, c.code)
        off.action_disable()
        info = self.Req._quota_info(self.phone)
        self.assertEqual([c['code'] for c in info['codes']], [live.code], "1.4 : comme avant")
        states = {c['code']: c['state'] for c in info['all_codes']}
        self.assertEqual(states, {live.code: 'active', off.code: 'disabled'})
        first = info['all_codes'][0]
        self.assertEqual((first['code'], first['source'], first['transfers_left'], first['max_devices']),
                         (live.code, 'gift', 3, 1))
        self.assertTrue(first['activated'])

    def test_state_search_and_stored_usage(self):
        today = fields.Date.today()
        fresh = self.Code.create({'credits': 2})
        used = self.Code.create({'credits': 2})
        empty = self.Code.create({'credits': 1})
        old = self.Code.create({'credits': 2, 'date_expiry': today - timedelta(days=1)})
        off = self.Code.create({'credits': 2, 'active': False})
        self.Code._redeem(self.phone, used.code)
        self.Code._redeem(self.phone, empty.code)
        self.quiz(self.phone)  # gratuit
        self.quiz(self.phone)  # sur un code (sans date : le plus ancien d'abord)
        consumed = (used | empty).filtered('used')
        self.assertEqual(len(consumed), 1)
        Code = self.Code.with_context(active_test=False)
        ids = lambda state: set(Code.search([('state', '=', state), ('id', 'in', (fresh | used | empty | old | off).ids)]).ids)
        self.assertEqual(ids('new'), {fresh.id})
        self.assertEqual(ids('expired'), {old.id})
        self.assertEqual(ids('disabled'), {off.id})
        self.assertEqual(ids('exhausted') | ids('active'), {used.id, empty.id})
        self.assertEqual(consumed.last_used.date(), today)
        self.assertEqual(consumed.usage_percent, 100.0 * consumed.used / consumed.credits)

    def test_update_wizard(self):
        today = fields.Date.today()
        a = self.Code.create({'credits': 2, 'date_expiry': today - timedelta(days=5)})
        b = self.Code.create({'credits': 3})
        self.Code._redeem(self.phone, b.code)
        self.quiz(self.phone)
        self.quiz(self.phone)  # b.used = 1
        Wizard = self.env['comprendre.ai.code.update'].with_context(active_model='comprendre.ai.code',
                                                                   active_ids=(a | b).ids)
        Wizard.create({'add_credits': 10, 'expiry_action': 'extend', 'extend_days': 30}).action_apply()
        self.assertEqual((a.credits, b.credits), (12, 13))
        self.assertEqual(a.date_expiry, today + timedelta(days=29), "un code expiré repart d'aujourd'hui")
        self.assertEqual(a.state, 'new')
        self.assertFalse(b.date_expiry, "sans limite : rien à prolonger")
        Wizard.create({'add_credits': -50, 'activity': 'disable'}).action_apply()
        self.assertEqual((a.credits, b.credits), (0, 1), "jamais moins que ce qui est déjà utilisé")
        self.assertEqual((a.active, b.active), (False, False))
        with self.assertRaises(UserError):
            Wizard.create({'expiry_action': 'set', 'date_expiry': today - timedelta(days=1)}).action_apply()

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

    def test_generate_wizard_makes_a_batch(self):
        wizard = self.env['comprendre.ai.code.generate'].create(
            {'quantity': 5, 'batch_name': 'Lycée moderne — 1re A', 'name': 'Lycée moderne', 'credits': 30,
             'max_devices': 1, 'prefix': 'ly-c', 'expiry_mode': 'days', 'validity_days': 30})
        self.assertIn('5 code(s)', wizard.preview)
        action = wizard.action_generate()
        batch = self.env['comprendre.ai.code.batch'].browse(action['res_id'])
        codes = batch.code_ids
        self.assertEqual(len(codes), 5)
        self.assertTrue(all(c.code.startswith('LYC-') for c in codes))
        self.assertEqual(set(codes.mapped('name')), {'Lycée moderne'})
        self.assertEqual(set(codes.mapped('credits')), {30})
        self.assertEqual(set(codes.mapped('origin')), {'batch'})
        self.assertEqual(set(codes.mapped('batch_id.name')), {'Lycée moderne — 1re A'})
        self.assertEqual(set(codes.mapped('date_expiry')), {fields.Date.today() + timedelta(days=29)})
        self.assertEqual((batch.code_count, batch.credits, batch.activated_count), (5, 150, 0))
        self.Code._redeem(self.phone, codes[0].code)
        self.quiz(self.phone)
        self.quiz(self.phone)
        batch.invalidate_recordset()
        self.assertEqual((batch.used, batch.remaining, batch.activated_count), (1, 149, 1))
        batch.action_disable()
        self.assertFalse(any(codes.mapped('active')))
        batch.action_enable()
        self.assertTrue(all(codes.mapped('active')))
        self.assertEqual(batch.action_print_cards()['report_name'], 'comprendre_backend.report_code_cards')
        # Un seul code : pas de lot.
        single = self.env['comprendre.ai.code.generate'].create({'quantity': 1, 'expiry_mode': 'none'}).action_generate()
        self.assertFalse(self.Code.browse(single['res_id']).batch_id)
        with self.assertRaises(UserError):
            self.env['comprendre.ai.code.generate'].create({'quantity': 0}).action_generate()
        with self.assertRaises(UserError):
            self.env['comprendre.ai.code.generate'].create({'quantity': 2, 'prefix': 'x'}).action_generate()

    def test_code_cards_report(self):
        codes = self.Code.create([{'credits': 10, 'date_expiry': fields.Date.today() + timedelta(days=10)},
                                  {'credits': 20}])
        html, _type = self.env['ir.actions.report']._render_qweb_html('comprendre_backend.report_code_cards', codes.ids)
        html = html.decode()
        for c in codes:
            self.assertIn(c.code, html)
        self.assertIn('data:image/png;base64,', html, "QR vers la page du code")


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
        again = self.rpc('/redeem', device_id='api-device-code-0123456', code=code.code)
        self.assertTrue(again['already'])
        # Transfert proposé (avec le nombre restant), puis confirmé ; ensuite retrait.
        offer = self.rpc('/redeem', device_id='api-device-new-phone-01', code=code.code, can_transfer=True)
        self.assertEqual(offer['error']['details'], {'reason': 'code_transferable', 'transfers_left': 3})
        moved = self.rpc('/redeem', device_id='api-device-new-phone-01', code=code.code, can_transfer=True, transfer=True)
        self.assertTrue(moved['success'], moved)
        self.assertEqual(self.rpc('/quota', device_id='api-device-code-0123456')['all_codes'], [])
        released = self.rpc('/code/release', device_id='api-device-new-phone-01', code=code.code)
        self.assertEqual(released['all_codes'], [])
        self.assertEqual(self.rpc('/code/release', device_id='api-device-new-phone-01', code=code.code)['error']['details'],
                         {'reason': 'code_unknown'})

    def test_code_page_reveals_nothing(self):
        page = self.url_open('/comprendre/code/abcd-efgh<script>')
        self.assertEqual(page.status_code, 200)
        self.assertIn('ABCD-EFGH', page.text)
        self.assertNotIn('<script>', page.text.split('<body>')[1].split('<button')[0])
        self.assertIn('/apps/comprendre', page.text)

    def test_redeem_is_rate_limited(self):
        reasons = [self.rpc('/redeem', device_id='api-device-guess-0123456', code='GUES-S%03d' % i)
                   for i in range(11)]
        self.assertEqual(reasons[0]['error']['details']['reason'], 'code_unknown')
        self.assertEqual(reasons[-1]['error']['code'], 'AUTH_010')
