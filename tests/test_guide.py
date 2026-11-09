# -*- coding: utf-8 -*-
"""Le guide de Comprendre, servis par /apps.

Ce qui est éprouvé ici est la plomberie, pas la prose : une capture manquante
ou un lien de sommaire qui n'aboutit pas ne lèvent aucune erreur — la page
s'affiche, simplement fausse. Les gabarits sont produits depuis le Markdown des
applications par `docs/md_to_guide_qweb.py` (dossier sunday_jobs).
"""
import re

from odoo.tests import HttpCase, tagged
from odoo.tools.misc import file_path

GUIDES = (
    # (xmlid de l'application, slug, un titre attendu dans la page)
    ('comprendre_backend.mobile_app_comprendre', 'comprendre', 'Quiz hors ligne et quiz IA'),
)

CAPTURE = re.compile(r"t-set=\"chemin\" t-value=\"'([^']+)'\"")


@tagged('post_install', '-at_install')
class TestComprendreGuide(HttpCase):

    def test_les_guides_sont_relies(self):
        for xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                app = self.env.ref(xmlid)
                self.assertEqual(app.slug, slug)
                self.assertTrue(self.env.ref(app.guide_template_xmlid))
                report = self.env.ref(app.guide_report_xmlid)
                # Le socle refuse un rapport déclaré sur un autre modèle.
                self.assertEqual(report.model, 'mobile.app')
                self.assertEqual(app.guide_link, f'/apps/{slug}/guide')

    def test_la_page_du_guide_est_publique(self):
        for _xmlid, slug, titre in GUIDES:
            with self.subTest(app=slug):
                page = self.url_open(f'/apps/{slug}/guide')
                self.assertEqual(page.status_code, 200)
                self.assertIn(titre, page.text)
                self.assertIn(f'/apps/{slug}/guide.pdf', page.text)

    def test_chaque_lien_du_sommaire_aboutit(self):
        for _xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                page = self.url_open(f'/apps/{slug}/guide').text
                self.assertIn('class="sommaire"', page)
                liens = set(re.findall(r'href="#([^"]+)"', page))
                ancres = set(re.findall(r'id="([^"]+)"', page))
                self.assertGreater(len(liens), 5)
                self.assertFalse(liens - ancres, 'liens internes sans ancre')

    def test_le_pdf_est_servi(self):
        for _xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                response = self.url_open(f'/apps/{slug}/guide.pdf')
                self.assertEqual(response.status_code, 200)
                self.assertEqual(response.headers['Content-Type'], 'application/pdf')

    def test_le_pdf_est_un_vrai_pdf(self):
        """En mode test, Odoo rend du HTML au lieu d'appeler wkhtmltopdf ;
        `force_report_rendering` impose la vraie impression."""
        for xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                app = self.env.ref(xmlid)
                report = self.env.ref(app.guide_report_xmlid)
                pdf, _type = report.with_context(force_report_rendering=True) \
                    ._render_qweb_pdf(report.report_name, app.ids)
                self.assertTrue(pdf.startswith(b'%PDF'))

    def test_le_pdf_declare_son_encodage(self):
        """Sans `web.basic_layout`, chaque accent sortait abîmé du PDF."""
        for xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                app = self.env.ref(xmlid)
                report = self.env.ref(app.guide_report_xmlid)
                html = report._render_qweb_html(report.report_name, app.ids)[0]
                bodies = report._prepare_html(html, report_model=report.model)[0]
                self.assertTrue(bodies)
                for body in bodies:
                    self.assertIn('charset="utf-8"',
                                  body.lower().replace("'", '"'))

    def test_chaque_capture_citee_existe(self):
        """Un guide peut n'avoir aucune capture ; celles qu'il cite existent."""
        for xmlid, slug, _titre in GUIDES:
            with self.subTest(app=slug):
                app = self.env.ref(xmlid)
                template = self.env.ref(app.guide_template_xmlid)
                for chemin in sorted(set(CAPTURE.findall(template.arch_db or ''))):
                    self.assertTrue(file_path(chemin), f"capture absente : {chemin}")
