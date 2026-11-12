# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo import fields
from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from odoo.addons.queue_payment.models.queue_wave import WaveError

LINK = 'https://pay.wave.com/m/M_TEST_COMPRENDRE/c/ci/'


@tagged('post_install', '-at_install')
class TestPurchase(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Order = self.env['comprendre.ai.order']
        self.Wave = type(self.env['queue.wave.mixin'])
        self.company = self.Order._selling_company()
        self.company.wave_payment_link = LINK
        self.pack = self.env['comprendre.ai.pack'].create(
            {'name': 'Étude', 'credits': 30, 'price': 2500, 'validity_days': 180})
        self.device = self.env['comprendre.device']._for_key('buyer-0123456789abcdef')

    def payments(self, order):
        return order._all_payments()

    # -- Lien marchand : validation par un agent ------------------------------
    def test_merchant_flow_delivers_code_on_validation(self):
        order, pay = self.Order._buy(self.device, self.pack.id, 'cli-1')
        self.assertEqual((pay['mode'], order.state, order.amount, order.credits), ('merchant', 'draft', 2500, 30))
        self.assertIn('M_TEST_COMPRENDRE', pay['url'])
        self.assertIn('amount=2500', pay['url'])
        payment = self.payments(order)
        self.assertEqual((len(payment), payment.state, payment.method), (1, 'to_validate', 'wave'))
        self.assertTrue(order.awaiting_validation)
        self.assertIn(order, self.Order.search([('awaiting_validation', '=', True)]))
        self.assertEqual(self.env['comprendre.ai.request']._quota_info(self.device)['code_remaining'], 0)

        payment.action_validate()
        self.assertEqual((order.state, order.payment_state), ('done', 'paid'))
        code = order.code_id
        self.assertEqual((code.credits, code.max_devices, code.device_count), (30, 1, 1))
        self.assertEqual(code.date_expiry, fields.Date.context_today(order) + __import__('datetime').timedelta(days=179))
        info = self.env['comprendre.ai.request']._quota_info(self.device)
        self.assertEqual((info['code_remaining'], info['codes'][0]['code']), (30, code.code))
        self.assertEqual(order._api_dict()['code'], code.code)
        self.assertFalse(order.awaiting_validation)

    def test_same_client_ref_is_one_order_one_payment(self):
        first, _p = self.Order._buy(self.device, self.pack.id, 'double-tap')
        again, _p = self.Order._buy(self.device, self.pack.id, 'double-tap')
        self.assertEqual(first, again)
        self.assertEqual(len(self.payments(first)), 1, "un achat renvoyé ne crée pas un second paiement")
        other, _p = self.Order._buy(self.device, self.pack.id, 'autre-achat')
        self.assertNotEqual(first, other)
        # Un achat payé renvoyé ne redemande pas de paiement.
        self.payments(first).action_validate()
        done, pay = self.Order._buy(self.device, self.pack.id, 'double-tap')
        self.assertEqual((done, pay), (first, {}))

    def test_revert_then_validate_again_reuses_code(self):
        order, _p = self.Order._buy(self.device, self.pack.id, 'r1')
        payment = self.payments(order)
        payment.action_validate()
        code = order.code_id
        payment.action_revert(reason="erreur de saisie")
        self.assertEqual((order.state, code.active), ('draft', False))
        self.assertEqual(self.env['comprendre.ai.request']._quota_info(self.device)['code_remaining'], 0)
        order._register_payment(method='wave')
        self.assertEqual((order.state, order.code_id, code.active), ('done', code, True))
        self.assertEqual(self.env['comprendre.ai.code'].search_count([('name', '=', 'Achat %s' % order.name)]), 1)

    def test_wave_ref_declaration(self):
        order, _p = self.Order._buy(self.device, self.pack.id, 'w1')
        with self.assertRaises(UserError) as ctx:
            order._declare_wave_ref('T12')
        self.assertEqual(ctx.exception.reason, 'bad_wave_ref')
        order._declare_wave_ref(' T_ABC 123456 ')
        self.assertEqual((order.wave_ref, self.payments(order).provider_ref), ('T_ABC123456', 'T_ABC123456'))
        other, _p = self.Order._buy(self.env['comprendre.device']._for_key('buyer-fedcba9876543210'), self.pack.id, 'w2')
        with self.assertRaises(UserError) as ctx:
            other._declare_wave_ref('T_ABC123456')
        self.assertEqual(ctx.exception.reason, 'wave_ref_used')

    def test_unavailable_pack(self):
        self.pack.active = False
        with self.assertRaises(UserError) as ctx:
            self.Order._buy(self.device, self.pack.id, 'x')
        self.assertEqual(ctx.exception.reason, 'pack_unavailable')
        with self.assertRaises(UserError):
            self.Order._buy(self.device, 'pas-un-id', 'y')

    # -- API Wave Checkout : confirmation par webhook -----------------------
    def _api(self, session=('sess_TEST_1', 'https://pay.wave.com/c/sess_TEST_1'), error=False):
        def checkout(_self, amount, currency, client_reference, return_url, company=None):
            self.sent = dict(amount=amount, currency=currency, client_reference=client_reference, return_url=return_url)
            if error:
                raise WaveError("boom")
            return session
        return (patch.object(self.Wave, '_wave_api_configured', lambda s, company=None: True),
                patch.object(self.Wave, '_wave_checkout_session', checkout))

    def test_checkout_flow_confirmed_by_webhook(self):
        p1, p2 = self._api()
        with p1, p2:
            order, pay = self.Order._buy(self.device, self.pack.id, 'api-1')
        self.assertEqual((pay['mode'], pay['url']), ('checkout', 'https://pay.wave.com/c/sess_TEST_1'))
        payment = self.payments(order)
        self.assertEqual((payment.state, payment.provider_ref), ('pending', 'sess_TEST_1'))
        self.assertEqual(self.sent['client_reference'], 'comprendre:%s' % order.name)
        self.assertIn('/comprendre/paiement/%s' % order.name, self.sent['return_url'])
        self.assertFalse(order.awaiting_validation, "rien à valider : Wave confirmera")
        event = {'type': 'checkout.session.completed',
                 'data': {'id': 'sess_TEST_1', 'checkout_status': 'complete', 'client_reference': payment.client_ref}}
        Wave = self.env['queue.wave.mixin'].sudo()
        self.assertTrue(Wave._wave_handle_webhook(event))
        self.assertEqual((order.state, payment.state), ('done', 'paid'))
        code = order.code_id
        self.assertTrue(Wave._wave_handle_webhook(event), "webhook rejoué : sans effet")
        self.assertEqual((order.code_id, code.credits, order.amount_paid), (code, 30, 2500))

    def test_checkout_failure_falls_back_to_merchant_link(self):
        p1, p2 = self._api(error=True)
        with p1, p2:
            order, pay = self.Order._buy(self.device, self.pack.id, 'api-2')
        self.assertEqual(pay['mode'], 'merchant')
        self.assertIn('M_TEST_COMPRENDRE', pay['url'])
        self.assertEqual(self.payments(order).state, 'to_validate')

    def test_failed_checkout_webhook_cancels_payment_not_order(self):
        p1, p2 = self._api()
        with p1, p2:
            order, _p = self.Order._buy(self.device, self.pack.id, 'api-3')
        payment = self.payments(order)
        self.env['queue.wave.mixin'].sudo()._wave_handle_webhook(
            {'data': {'id': 'sess_TEST_1', 'checkout_status': 'expired', 'client_reference': payment.client_ref}})
        self.assertEqual((payment.state, order.state), ('cancelled', 'draft'))

    # -- Entretien -----------------------------------------------------------
    def test_stale_orders_cancelled(self):
        order, _p = self.Order._buy(self.device, self.pack.id, 'old')
        self.env.cr.execute("UPDATE comprendre_ai_order SET create_date = now() - interval '40 days' WHERE id = %s",
                            [order.id])
        order.invalidate_recordset()
        self.assertEqual(self.Order._cron_cancel_stale(30), 1)
        self.assertEqual((order.state, self.payments(order).state), ('cancelled', 'cancelled'))
        self.assertFalse(self.Order._device_orders(self.device))

    def test_cancel_refused_when_paid(self):
        order, _p = self.Order._buy(self.device, self.pack.id, 'c1')
        self.payments(order).action_validate()
        order.action_cancel()  # sans effet sur un achat payé
        self.assertEqual(order.state, 'done')


@tagged('post_install', '-at_install')
class TestPurchaseApi(HttpCase):

    def rpc(self, path, **params):
        res = self.url_open('/comprendre/api/v1' + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 200)
        return res.json()['result']

    def test_shop_buy_orders(self):
        company = self.env['comprendre.ai.order']._selling_company()
        company.wave_payment_link = LINK
        Pack = self.env['comprendre.ai.pack']
        Pack.search([]).write({'active': False})
        pack = Pack.create({'name': 'Découverte', 'credits': 10, 'price': 1000})
        dev = 'api-buyer-0123456789'
        shop = self.rpc('/shop', device_id=dev)
        self.assertTrue(shop['payment_available'])
        self.assertFalse(shop['auto_confirm'])
        self.assertEqual([p['name'] for p in shop['packs']], ['Découverte'])
        bought = self.rpc('/buy', device_id=dev, pack_id=pack.id, client_ref='app-uuid-1')
        self.assertTrue(bought['success'], bought)
        self.assertEqual((bought['mode'], bought['order']['state']), ('merchant', 'draft'))
        self.assertTrue(bought['order']['awaiting_validation'])
        ref = bought['order']['ref']
        declared = self.rpc('/order/wave-ref', device_id=dev, ref=ref, wave_ref='T_9876543210')
        self.assertEqual(declared['order']['wave_ref'], 'T_9876543210')
        # Un autre appareil ne voit ni ne touche cet achat.
        stranger = self.rpc('/order/wave-ref', device_id='api-stranger-012345678', ref=ref, wave_ref='T_1111111111')
        self.assertEqual(stranger['error']['details']['reason'], 'order_unknown')
        self.assertEqual(self.rpc('/orders', device_id='api-stranger-012345678')['orders'], [])
        # L'agent valide : le code apparaît dans /orders et dans le quota.
        order = self.env['comprendre.ai.order'].search([('name', '=', ref)])
        order._all_payments().action_validate()
        after = self.rpc('/orders', device_id=dev)
        self.assertEqual(after['orders'][0]['state'], 'done')
        self.assertTrue(after['orders'][0]['code'])
        self.assertEqual(after['quota']['code_remaining'], 10)

    def test_return_page(self):
        res = self.url_open('/comprendre/paiement/ACH-2026-00001')
        self.assertEqual(res.status_code, 200)
        self.assertIn('Comprendre', res.text)
