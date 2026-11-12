# -*- coding: utf-8 -*-
"""API de l'application Comprendre (JSON-RPC, enveloppe success/error comme les autres applications Kvola)."""
import hashlib
import logging

from odoo import _
from odoo.exceptions import UserError
from odoo.http import Controller, request, route

from odoo.addons.otp_auth.tools.client_ip import client_ip

_logger = logging.getLogger(__name__)

API = '/comprendre/api/v1'
RL_QUIZ = (20, 600, 600)  # 20 demandes / 10 min / IP, avant même le quota
RL_BUY = (10, 600, 600)  # 10 achats préparés / 10 min / IP
RL_REDEEM = (10, 600, 900)  # 10 essais de code IA / 10 min / IP (et par appareil) : pas de recherche à l'aveugle


def ok(**payload):
    return dict(payload, success=True)


def fail(code, message, **details):
    response = {'success': False, 'error': {'code': code, 'message': message}, 'error_code': code, 'message': message}
    if details:
        response['error']['details'] = details
    return response


def _ip_hash():
    """Empreinte de l'adresse IP (salée avec le secret de la base) : on compte sans stocker l'adresse."""
    secret = request.env['ir.config_parameter'].sudo().get_param('database.secret', '')
    return hashlib.sha256(('%s|%s' % (secret, client_ip(request))).encode()).hexdigest()[:32]


class ComprendreApi(Controller):

    @route(API + '/quota', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def quota(self, **kw):
        try:
            device = request.env['comprendre.device'].sudo()._for_key(kw.get('device_id'))
        except UserError as e:
            return fail('BAD_REQUEST', str(e.args[0]), reason=getattr(e, 'reason', 'bad_request'))
        return ok(**request.env['comprendre.ai.request'].sudo()._quota_info(device))

    @route(API + '/quiz', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def quiz(self, **kw):
        ip = _ip_hash()
        blocked, seconds = request.env['otp.rate.limit'].sudo().check_and_record('comprendre:quiz:%s' % ip, *RL_QUIZ)
        if blocked:
            minutes = max(1, round(seconds / 60))
            return fail('AUTH_010', _("Trop de demandes. Réessayez dans %s minute(s).", minutes), reason='rate_limited',
                        minutes=minutes)
        Req = request.env['comprendre.ai.request'].sudo()
        try:
            device = request.env['comprendre.device'].sudo()._for_key(kw.get('device_id'))
            result = Req._generate(device, ip, kw.get('text'), kw.get('lang'), kw.get('count'))
        except UserError as e:
            return fail('REFUSED', str(e.args[0]), reason=getattr(e, 'reason', 'refused'))
        if result.get('error'):
            return fail('AI_FAILED', result['error'], reason=result.get('reason', 'ai_error'))
        return ok(**result)

    @route(API + '/redeem', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def redeem(self, **kw):
        """Active un code IA sur l'appareil. Réponse : le code et le quota à jour (comme /quota)."""
        RateLimit = request.env['otp.rate.limit'].sudo()
        ip = _ip_hash()
        device_key = str(kw.get('device_id') or '')[:128]
        for key in ('comprendre:redeem:ip:%s' % ip, 'comprendre:redeem:dev:%s' % device_key):
            blocked, seconds = RateLimit.check_and_record(key, *RL_REDEEM)
            if blocked:
                minutes = max(1, round(seconds / 60))
                return fail('AUTH_010', _("Trop d'essais. Réessayez dans %s minute(s).", minutes), reason='rate_limited',
                            minutes=minutes)
        try:
            device = request.env['comprendre.device'].sudo()._for_key(kw.get('device_id'))
            code = request.env['comprendre.ai.code'].sudo()._redeem(device, kw.get('code'))
        except UserError as e:
            return fail('REFUSED', str(e.args[0]), reason=getattr(e, 'reason', 'refused'))
        return ok(redeemed=code._info(device), **request.env['comprendre.ai.request'].sudo()._quota_info(device))


    # ------------------------------------------------------------------ #
    # Achat de quiz IA (Wave, socle queue_payment)
    # ------------------------------------------------------------------ #
    def _device_or_fail(self, kw):
        return request.env['comprendre.device'].sudo()._for_key(kw.get('device_id'))

    def _shop_payload(self, device):
        Order = request.env['comprendre.ai.order'].sudo()
        company = Order._selling_company()
        packs = request.env['comprendre.ai.pack'].sudo().search([('company_id', '=', company.id)])
        wave = request.env['queue.wave.mixin'].sudo()
        return {
            'packs': [p._api_dict() for p in packs],
            'payment_available': bool(packs) and bool(
                wave._wave_api_configured(company) or wave._wave_operator_link(company)),
            'auto_confirm': bool(wave._wave_api_configured(company)),
            'orders': [o._api_dict() for o in Order._device_orders(device)],
        }

    @route(API + '/shop', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def shop(self, **kw):
        """Offres en vente, et achats récents de l'appareil."""
        try:
            device = self._device_or_fail(kw)
        except UserError as e:
            return fail('BAD_REQUEST', str(e.args[0]), reason=getattr(e, 'reason', 'bad_request'))
        return ok(**self._shop_payload(device))

    @route(API + '/buy', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def buy(self, **kw):
        """Achète une offre : renvoie l'achat et l'URL Wave à ouvrir. ``client_ref`` rend l'appel rejouable."""
        blocked, seconds = request.env['otp.rate.limit'].sudo().check_and_record(
            'comprendre:buy:%s' % _ip_hash(), *RL_BUY)
        if blocked:
            minutes = max(1, round(seconds / 60))
            return fail('AUTH_010', _("Trop de demandes. Réessayez dans %s minute(s).", minutes), reason='rate_limited',
                        minutes=minutes)
        try:
            device = self._device_or_fail(kw)
            order, pay = request.env['comprendre.ai.order'].sudo()._buy(device, kw.get('pack_id'), kw.get('client_ref'))
        except UserError as e:
            return fail('REFUSED', str(e.args[0]), reason=getattr(e, 'reason', 'refused'))
        return ok(order=order._api_dict(), mode=pay.get('mode', False), url=pay.get('url', False))

    @route(API + '/orders', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def orders(self, **kw):
        """Achats de l'appareil et quota à jour (après un paiement confirmé, le code y figure)."""
        try:
            device = self._device_or_fail(kw)
        except UserError as e:
            return fail('BAD_REQUEST', str(e.args[0]), reason=getattr(e, 'reason', 'bad_request'))
        Order = request.env['comprendre.ai.order'].sudo()
        return ok(orders=[o._api_dict() for o in Order._device_orders(device)],
                  quota=request.env['comprendre.ai.request'].sudo()._quota_info(device))

    @route(API + '/order/wave-ref', type='jsonrpc', auth='public', methods=['POST'], csrf=False)
    def order_wave_ref(self, **kw):
        """L'acheteur donne l'identifiant de sa transaction Wave (paiement par lien marchand)."""
        try:
            device = self._device_or_fail(kw)
            order = request.env['comprendre.ai.order'].sudo().search(
                [('device_id', '=', device.id), ('name', '=', str(kw.get('ref') or ''))], limit=1)
            if not order:
                return fail('REFUSED', _("Achat introuvable."), reason='order_unknown')
            order._declare_wave_ref(kw.get('wave_ref'))
        except UserError as e:
            return fail('REFUSED', str(e.args[0]), reason=getattr(e, 'reason', 'refused'))
        return ok(order=order._api_dict())

    @route('/comprendre/paiement/<string:ref>', type='http', auth='public', methods=['GET'], csrf=False,
           save_session=False)
    def payment_return(self, ref, **kw):
        """Page de retour après Wave : rien de personnel, juste « revenez dans l'application »."""
        html = """<!doctype html><html lang="fr"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Comprendre</title>
<style>body{font-family:system-ui,sans-serif;max-width:32rem;margin:3rem auto;padding:0 1rem;line-height:1.5;color:#0e3a44}
h1{font-size:1.4rem}</style></head><body>
<h1>Merci&nbsp;!</h1>
<p>Revenez dans l'application <b>Comprendre</b>&nbsp;: dès que Wave confirme le paiement, vos quiz IA y sont ajoutés
automatiquement (Réglages → Quiz IA).</p>
<p lang="en"><i>Thank you! Go back to the <b>Understand</b> app: as soon as Wave confirms the payment, your AI quizzes
are added automatically.</i></p></body></html>"""
        return request.make_response(html, headers=[('Content-Type', 'text/html; charset=utf-8')])
