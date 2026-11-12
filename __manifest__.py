# -*- coding: utf-8 -*-
{
    'name': "Comprendre — quiz de compréhension par IA",
    'version': '19.0.1.3.0',
    'category': 'Productivity',
    'summary': "Serveur de l'application Comprendre : génère un quiz de compréhension "
               "à partir d'un texte avec Claude, quota quotidien par appareil",
    'description': """
Comprendre — le serveur
=======================

L'application Comprendre crée un quiz pour aider à comprendre un texte. Elle
le fait d'abord sur le téléphone, hors ligne ; quand le réseau le permet et
que le quota du jour n'est pas épuisé, elle demande ici un quiz de meilleure
qualité, rédigé par Claude (Anthropic).

* La clé API reste sur le serveur, jamais dans l'application.
* Quota gratuit par appareil et par jour (identifiant anonyme, sans compte),
  plus un plafond par adresse IP contre les abus.
* Chaque question doit citer mot pour mot un passage du texte : une citation
  introuvable fait écarter la question (pas d'invention).
* Chaque appel est journalisé (jetons, durée, résultat) pour suivre le coût.
* Le texte n'est pas conservé : seule sa longueur est journalisée.

Réglages (Paramètres système) : ``comprendre.anthropic_api_key`` (ou la
variable d'environnement ``ANTHROPIC_API_KEY``), ``comprendre.model``,
``comprendre.daily_quota``, ``comprendre.ip_daily_cap``, ``comprendre.max_chars``,
``comprendre.enabled``.
    """,
    'author': 'ICP',
    'website': 'https://ivorycocoa.ci',
    'depends': ['base', 'otp_auth', 'mobile_app_release'],
    'external_dependencies': {'python': ['anthropic']},
    'data': [
        'security/comprendre_security.xml',
        'security/ir.model.access.csv',
        'reports/user_guide.xml',
        'data/comprendre_data.xml',
        'views/comprendre_views.xml',
        'views/ai_code_views.xml',
    ],
    'installable': True,
    'application': True,
    'license': 'LGPL-3',
}
