# -*- coding: utf-8 -*-
import json
from unittest.mock import patch

from odoo.exceptions import UserError
from odoo.tests import HttpCase, TransactionCase, tagged

from ..models.comprendre import normalize

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
