# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from types import SimpleNamespace as NS

from ..models.comprendre import API_RETRIES, API_TIMEOUT, Refusal, normalize, price_of

TEXT = (
    "Le baobab est un arbre emblématique des savanes africaines. Il peut vivre plus de mille ans. "
    "Son tronc stocke de grandes quantités d'eau, ce qui lui permet de survivre aux longues saisons sèches. "
    "Ses fruits, appelés pain de singe, sont riches en vitamine C. Les feuilles sont consommées en sauce "
    "dans plusieurs pays. Pourtant, des chercheurs ont observé que plusieurs très vieux baobabs sont morts "
    "depuis 2005, peut-être à cause du changement climatique."
)


def fake_quiz(valid=4, invented=1):
    qs = [{
        'kind': 'choice', 'question': "Combien de temps peut vivre un baobab ?",
        'choices': ["Plus de mille ans", "Cent ans", "Dix ans", "Cinq cents ans"], 'answer': 0,
        'explanation': "Le texte dit qu'il peut vivre plus de mille ans.", 'quote': "Il peut vivre plus de mille ans.",
        'skill': 'detail'} for _ in range(valid)]
    qs += [{
        'kind': 'truefalse', 'question': "Le baobab pousse en Europe.", 'choices': [], 'answer': 0,
        'explanation': "Inventé.", 'quote': "Le baobab pousse surtout en Europe du Nord.", 'skill': 'detail'}
        for _ in range(invented)]
    return {'title': "Le baobab", 'summary': "Un arbre résistant mais menacé.", 'questions': qs}


@tagged('post_install', '-at_install')
class TestComprendre(TransactionCase):

    def setUp(self):
        super().setUp()
        self.Req = self.env['comprendre.ai.request']
        self.device = self.env['comprendre.device']._for_key('device-0123456789abcdef')

    def _patch(self, data=None, error=None):
        def call(_self, text, lang, count):
            if error:
                raise UserError(error)
            return data or fake_quiz(), 1200, 800, 'claude-opus-5-5'
        return patch.object(type(self.Req), '_call_claude', call)

    def test_normalize(self):
        self.assertEqual(normalize("L’Été, déjà !"), 'l ete deja')

    def test_invented_quotes_are_dropped_and_logged(self):
        with self._patch():
            r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(len(r['questions']), 4)
        log = self.Req.search([('device_id', '=', self.device.id)])
        self.assertEqual((log.state, log.questions, log.dropped, log.input_tokens), ('done', 4, 1, 1200))
        self.assertEqual(r['remaining'], 4)

    def test_too_few_valid_questions_is_rejected_but_logged(self):
        with self._patch(fake_quiz(valid=2, invented=3)):
            r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertIn('error', r)
        log = self.Req.search([('device_id', '=', self.device.id)])
        self.assertEqual(log.state, 'rejected', "le coût d'un quiz écarté reste visible")
        self.assertEqual(self.Req._remaining(self.device), 5, "un quiz écarté ne compte pas dans le quota")

    def test_api_failure_is_logged_not_counted(self):
        with self._patch(error="indisponible"):
            r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(r['error'], "indisponible")
        self.assertEqual(self.Req.search([('device_id', '=', self.device.id)]).state, 'failed')
        self.assertEqual(self.Req._remaining(self.device), 5)

    def test_daily_quota(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_quota', 2)
        with self._patch():
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
            with self.assertRaises(UserError) as ctx:
                self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
            self.assertEqual(ctx.exception.reason, 'quota')
        other = self.env['comprendre.device']._for_key('device-fedcba9876543210')
        with self._patch():
            self.assertIn('questions', self.Req._generate(other, 'ip2', TEXT, 'fr', 10))

    def test_ip_cap_and_blocked_device(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.ip_daily_cap', 1)
        other = self.env['comprendre.device']._for_key('device-aaaaaaaaaaaaaaaa')
        with self._patch():
            self.Req._generate(self.device, 'same-ip', TEXT, 'fr', 10)
            with self.assertRaises(UserError):
                self.Req._generate(other, 'same-ip', TEXT, 'fr', 10)
        other.blocked = True
        with self._patch(), self.assertRaises(UserError):
            self.Req._generate(other, 'ip3', TEXT, 'fr', 10)

    def test_text_limits_and_device_key(self):
        with self._patch():
            with self.assertRaises(UserError):
                self.Req._generate(self.device, 'ip1', "Trop court.", 'fr', 10)
            self.env['ir.config_parameter'].sudo().set_param('comprendre.max_chars', 300)
            with self.assertRaises(UserError):
                self.Req._generate(self.device, 'ip1', TEXT * 2, 'fr', 10)
        with self.assertRaises(UserError):
            self.env['comprendre.device']._for_key('court')
        self.assertEqual(self.env['comprendre.device']._for_key('device-0123456789abcdef'), self.device)

    def test_disabled(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.enabled', '0')
        with self._patch(), self.assertRaises(UserError):
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)

    def test_validation_rejects_malformed(self):
        data = fake_quiz(valid=1, invented=0)
        bad = dict(data['questions'][0], choices=["A", "A", "B", "C"])
        wrong_index = dict(data['questions'][0], answer=7)
        kept, dropped = self.Req._validate({'questions': data['questions'] + [bad, wrong_index, {'kind': 'x'}]}, TEXT)
        self.assertEqual((len(kept), dropped), (1, 3))

    def test_no_api_key_gives_clear_error(self):
        with patch.dict('os.environ', {'ANTHROPIC_API_KEY': ''}):
            r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertIn("pas configuré", r['error'])
        self.assertEqual(r['reason'], 'not_configured')


    # -- 1.2.0 : réservation, échecs facturés, coût, langues ----------------
    def test_pending_reservation_counts_during_call(self):
        seen = {}

        def call(_self, text, lang, count):
            log = self.Req.search([('device_id', '=', self.device.id)])
            seen['state'] = log.state
            seen['remaining'] = self.Req._remaining(self.device)
            return fake_quiz(), 10, 10, 'claude-opus-5-5'
        with patch.object(type(self.Req), '_call_claude', call):
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(seen, {'state': 'pending', 'remaining': 4}, "la demande en cours compte déjà")
        self.assertEqual(self.Req._remaining(self.device), 4)

    def _billed_refusal(self, reason='bad_response'):
        def call(_self, text, lang, count):
            raise Refusal("illisible", reason, (5000, 3000, 'claude-opus-5-5'))
        return patch.object(type(self.Req), '_call_claude', call)

    def test_billed_failures_are_capped(self):
        self.env['ir.config_parameter'].sudo().set_param('comprendre.daily_rejected_cap', 2)
        with self._billed_refusal():
            for _i in range(2):
                r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
                self.assertEqual(r['reason'], 'bad_response')
        logs = self.Req.search([('device_id', '=', self.device.id)])
        self.assertEqual(set(logs.mapped('state')), {'rejected'})
        self.assertAlmostEqual(sum(logs.mapped('cost')), 2 * (5000 * 4 + 3000 * 20) / 1e6, places=6)
        self.assertEqual(self.Req._remaining(self.device), 5, "un échec ne consomme pas le quota de quiz")
        with self._patch(), self.assertRaises(UserError) as ctx:
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(ctx.exception.reason, 'failures')
        info = self.Req._quota_info(self.device)
        self.assertEqual((info['remaining'], info['limited']), (0, 'failures'))

    def test_unexpected_error_frees_reservation(self):
        def call(_self, text, lang, count):
            raise TypeError("boom")
        with patch.object(type(self.Req), '_call_claude', call), \
                self.assertLogs('odoo.addons.comprendre_backend.models.comprendre', level='ERROR'):
            r = self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        self.assertEqual(r['reason'], 'ai_error')
        self.assertEqual(self.Req.search([('device_id', '=', self.device.id)]).state, 'failed')
        self.assertEqual(self.Req._remaining(self.device), 5)

    def test_cost_and_prices(self):
        with self._patch():
            self.Req._generate(self.device, 'ip1', TEXT, 'fr', 10)
        log = self.Req.search([('device_id', '=', self.device.id)])
        self.assertAlmostEqual(log.cost, (1200 * 4 + 800 * 20) / 1e6, places=6)
        self.assertEqual(price_of('claude-sonnet-5-5'), (2.0, 10.0))
        self.assertEqual(price_of('claude-opus-5'), (5.0, 25.0), "opus-5 ne doit pas prendre le prix d'opus-5-5")
        self.assertIsNone(price_of('inconnu'))

    def test_languages(self):
        langs = []

        def call(_self, text, lang, count):
            langs.append(lang)
            return fake_quiz(), 10, 10, 'claude-opus-5-5'
        with patch.object(type(self.Req), '_call_claude', call):
            for lang in ('es', 'de', 'xx', None):
                self.Req._generate(self.device, 'ip1', TEXT, lang, 'beaucoup')
        self.assertEqual(langs, ['es', 'de', 'fr', 'fr'])
        self.assertIn('pt', self.Req._quota_info(self.device)['languages'])

    def test_gc_pending(self):
        log = self.Req.create({'device_id': self.device.id, 'state': 'pending'})
        self.env.cr.execute("UPDATE comprendre_ai_request SET create_date = now() - interval '2 hours' WHERE id = %s",
                            [log.id])
        log.invalidate_recordset()
        self.assertEqual(self.Req._gc_pending(), 1)
        self.assertEqual(log.state, 'failed')

    def test_call_claude_with_fake_client(self):
        """Le vrai _call_claude, avec un client simulé : paramètres envoyés et refus facturés."""
        sent = {}

        def response(stop='end_turn', text=None):
            return NS(stop_reason=stop, model='claude-opus-5-5', usage=NS(input_tokens=7, output_tokens=9),
                      content=[NS(type='text', text=json.dumps(fake_quiz()) if text is None else text)])

        def client_for(resp):
            def create(**kw):
                sent.update(kw)
                return resp
            return NS(beta=NS(messages=NS(create=create)))
        Req = type(self.Req)
        with patch.object(Req, '_client', lambda s: client_for(response())):
            data, tin, tout, model = self.Req._call_claude(TEXT, 'es', 8)
        self.assertEqual((len(data['questions']), tin, tout, model), (5, 7, 9, 'claude-opus-5-5'))
        self.assertIn('Language: Spanish', sent['messages'][0]['content'])
        self.assertEqual(sent['fallbacks'], 'default')
        for stop, text, reason in (('max_tokens', None, 'too_long'), ('refusal', None, 'refused_content'),
                                   ('end_turn', 'pas du json', 'bad_response')):
            with patch.object(Req, '_client', lambda s, r=response(stop, text): client_for(r)):
                with self.assertRaises(Refusal) as ctx:
                    self.Req._call_claude(TEXT, 'fr', 8)
            self.assertEqual((ctx.exception.reason, ctx.exception.usage), (reason, (7, 9, 'claude-opus-5-5')))

    def test_timeouts_fit_the_app(self):
        """L'application attend 240 s : tous les essais doivent tenir dedans."""
        self.assertLess(API_TIMEOUT * (API_RETRIES + 1) + 10, 240)


@tagged('post_install', '-at_install')
class TestComprendreApi(HttpCase):

    def rpc(self, path, **params):
        res = self.url_open('/comprendre/api/v1' + path, data=json.dumps({'jsonrpc': '2.0', 'method': 'call', 'params': params}),
                            headers={'Content-Type': 'application/json'})
        self.assertEqual(res.status_code, 200)
        return res.json()['result']

    def test_quiz_and_quota(self):
        Req = self.env['comprendre.ai.request']
        with patch.object(type(Req), '_call_claude', lambda s, t, l, c: (fake_quiz(), 10, 10, 'claude-opus-5-5')):
            q = self.rpc('/quota', device_id='api-device-0123456789')
            self.assertEqual(q['remaining'], 5)
            r = self.rpc('/quiz', device_id='api-device-0123456789', text=TEXT, lang='fr', count=8)
            self.assertTrue(r['success'], r)
            self.assertEqual(len(r['questions']), 4)
            self.assertEqual(r['remaining'], 4)
            bad = self.rpc('/quiz', device_id='x', text=TEXT)
            self.assertEqual(bad['error']['code'], 'REFUSED')
            self.assertEqual(bad['error']['details']['reason'], 'bad_device')
            short = self.rpc('/quiz', device_id='api-device-0123456789', text='Trop court.')
            self.assertEqual(short['error']['details']['reason'], 'too_short')
            q = self.rpc('/quota', device_id='api-device-0123456789')
            self.assertEqual((q['remaining'], q['limited']), (4, ''))
            self.assertIn('es', q['languages'])
