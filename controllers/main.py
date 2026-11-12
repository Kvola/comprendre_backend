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

