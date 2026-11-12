# -*- coding: utf-8 -*-
from unittest.mock import patch

from odoo.exceptions import UserError
import json

from odoo.tests import HttpCase, TransactionCase, tagged

from .test_comprendre import TEXT, fake_quiz


@tagged('post_install', '-at_install')
class TestPricing(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Pack = self.env['comprendre.ai.pack'].with_context(active_test=False)
        self.Req = self.env['comprendre.ai.request']
        self.company = self.env['comprendre.ai.order']._selling_company()
        xof = self.env.ref('base.XOF')
        xof.active = True
        self.company.currency_id = xof
        self.Pack.search([]).unlink()  # offres d'exemple : chaque test part de zéro
        self.env['ir.config_parameter'].sudo().set_param('comprendre.usd_xof_rate', '600')
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_quota', 1)
        self.device = self.env['comprendre.device']._for_key('pricing-0123456789abcd')

    def refused(self, n=1):
        """Demandes au-delà du quota, comme l'API (pas de assertRaises : il annule les écritures du bloc)."""
        for _i in range(n):
            try:
                self.quiz()
            except UserError as e:
                self.assertEqual(e.reason, 'quota')
            else:
                self.fail("quota attendu")

    def quiz(self, device=None):
        def call(_self, text, lang, count):
            return fake_quiz(), 1000, 500, 'claude-opus-5-5'  # 0,014 $ par quiz
        with patch.object(type(self.Req), '_call_claude', call):
            return self.Req._generate(device or self.device, 'ip', TEXT, 'fr', 10)

    # -- Assistant -----------------------------------------------------------
    def test_wizard_prices_from_measured_cost(self):
        for _i in range(3):
            try:
                self.quiz()
            except UserError:
                pass  # au-delà du quota : refusé (aucun coût)
        w = self.env['comprendre.ai.pricing'].create({})
        self.assertEqual(w.measured_quizzes, 1)
        self.assertAlmostEqual(w.measured_cost, 0.014, places=6)
        self.assertEqual(w.cost_usd, max(w.measured_cost, w.estimated_cost), "trop peu de mesures : prudence")
        w.write({'cost_usd': 0.1, 'safety_percent': 0, 'wave_fee_percent': 0, 'margin_percent': 50, 'rounding': 50})
        # 0,1 $ × 600 = 60 F ; marge 50 % → 120 F le quiz.
        self.assertAlmostEqual(w.unit_price, 120.0)
        self.assertEqual([l.name for l in w.line_ids], ['Découverte', 'Étude', 'Intensif'])
        disc, study, intense = w.line_ids
        self.assertEqual((disc.price, study.price, intense.price), (1200, 3250, 9600))  # 1200 ; 3240→3250 ; 9600
        self.assertEqual(study.margin_percent, round(100 * (3250 - 1800) / 3250))
        # Remise trop forte : prix remonté au plancher (le coût).
        intense.discount = 80
        self.assertTrue(intense.below_floor)
        self.assertEqual(intense.price, 6000)
        self.assertIn('Intensif', w.summary)
        intense.discount = 20
        w.activate = True
        w.action_apply()
        packs = self.Pack.search([('company_id', '=', self.company.id)], order='sequence, credits')
        self.assertEqual([(p.name, p.credits, p.price, p.active) for p in packs],
                         [('Découverte', 10, 1200, True), ('Étude', 30, 3250, True), ('Intensif', 100, 9600, True)])
        self.assertTrue(packs[1].highlight)
        # Relancé : l'assistant repart des offres existantes (remises retrouvées) et les met à jour.
        again = self.env['comprendre.ai.pricing'].create({})
        self.assertEqual(again.line_ids.mapped('pack_id'), packs)
        self.assertEqual(again.line_ids.mapped('discount'), [0, 10, 20])

    def test_estimate_without_measure(self):
        w = self.env['comprendre.ai.pricing'].create({})
        self.assertEqual(w.measured_quizzes, 0)
        self.assertAlmostEqual(w.estimated_cost, (6000 * 4 + 3000 * 20) / 1e6)
        self.assertEqual(w.cost_usd, w.estimated_cost)
        self.assertTrue(all(l.price > 0 and l.price % 50 == 0 for l in w.line_ids))
        self.assertTrue(all(l.margin > 0 for l in w.line_ids))

    # -- Offres dans l'application -------------------------------------------
    def make_packs(self):
        return self.Pack.create([
            {'name': 'Découverte', 'credits': 10, 'price': 1000, 'validity_days': 90, 'sequence': 1},
            {'name': 'Étude', 'credits': 30, 'price': 2700, 'validity_days': 180, 'sequence': 2, 'highlight': True},
            {'name': 'Intensif', 'credits': 100, 'price': 8000, 'validity_days': 365, 'sequence': 3},
        ])

    def test_offer_savings_badges_and_no_advice_without_need(self):
        packs = self.make_packs()
        self.quiz()  # dans le quota gratuit
        offers, advice = self.Pack._offer(packs, self.device)
        by = {o['name']: o for o in offers}
        self.assertEqual((by['Découverte']['price_per_quiz'], by['Étude']['saving_percent'],
                          by['Intensif']['saving_percent']), (100.0, 10, 20))
        self.assertEqual(by['Découverte']['saving_percent'], 0)
        self.assertTrue(by['Intensif']['best_value'])
        self.assertTrue(by['Étude']['highlight'])
        self.assertFalse(any(o['recommended'] for o in offers), "le gratuit suffit : aucun conseil d'achat")
        self.assertEqual((advice['monthly'], advice['extra_monthly'], advice['free_enough'], advice['daily_free']),
                         (1, 0, True, 1))

    def test_advice_follows_need_beyond_free(self):
        packs = self.make_packs()
        self.quiz()  # gratuit
        self.refused(4)  # 4 demandes refusées : quota atteint
        self.assertEqual(self.Req.search_count([('device_id', '=', self.device.id), ('state', '=', 'refused')]), 4)
        _o, advice = self.Pack._offer(packs, self.device)
        # Besoin 4 / mois → 12 sur 3 mois : la Découverte (10) ; l'Étude (30) dépasserait.
        self.assertEqual((advice['extra_monthly'], advice['recommended_id']), (4, packs[0].id))
        # Gros besoin : 40 / mois → l'Intensif (100, consommé en moins d'un an).
        self.Req.create([{'device_id': self.device.id, 'state': 'refused'} for _i in range(36)])
        _o, advice = self.Pack._offer(packs, self.device)
        self.assertEqual(advice['recommended_id'], packs[2].id)
        # Un pack qui expirerait avant d'être consommé n'est jamais conseillé.
        packs[2].validity_days = 30
        _o, advice = self.Pack._offer(packs, self.device)
        self.assertEqual(advice['recommended_id'], packs[1].id)

    def test_refused_requests_cost_nothing_and_stay_out_of_quota(self):
        self.quiz()
        self.refused()
        refused = self.Req.search([('state', '=', 'refused')])
        self.assertEqual((len(refused), refused.cost), (1, 0.0))
        self.assertEqual(self.Req._remaining(self.device), 0)
        self.assertEqual(self.env['comprendre.dashboard'].get_data(7)['usage']['refused'], 1)


@tagged('post_install', '-at_install')
class TestShopApi(HttpCase):

    def rpc(self, path, **params):
        res = self.url_open('/comprendre/api/v1' + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'})
        return res.json()['result']

    def test_quota_refusal_is_recorded_and_shop_advises(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_quota', 0)
        self.env['ir.config_parameter'].sudo().set_param('comprendre.anthropic_api_key', 'sk-test')
        company = self.env['comprendre.ai.order']._selling_company()
        company.wave_payment_link = 'https://pay.wave.com/m/M_TEST/c/ci/'
        self.env['comprendre.ai.pack'].with_context(active_test=False).search([]).unlink()
        small, big = self.env['comprendre.ai.pack'].create([
            {'name': 'Petit', 'credits': 5, 'price': 600, 'validity_days': 90},
            {'name': 'Grand', 'credits': 50, 'price': 5000, 'validity_days': 365},
        ])
        device = 'shop-api-device-01234567'
        for _i in range(2):
            r = self.rpc('/quiz', device_id=device, text=TEXT, lang='fr', count=10)
            self.assertEqual(r['error']['details']['reason'], 'quota')
        shop = self.rpc('/shop', device_id=device)
        self.assertEqual((shop['advice']['extra_monthly'], shop['advice']['recommended_id']), (2, small.id))
        by = {p['name']: p for p in shop['packs']}
        self.assertEqual((by['Grand']['saving_percent'], by['Grand']['best_value'], by['Petit']['recommended']),
                         (17, True, True))
