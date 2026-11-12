# comprendre_backend

Module Odoo 19 : serveur de l'application **Comprendre** (quiz de compréhension d'un texte). L'application crée ses quiz hors ligne ; ce module lui fournit, quand le réseau et le quota le permettent, un quiz de meilleure qualité rédigé par Claude (Anthropic).

## Fonctionnement

- `POST /comprendre/api/v1/quiz` (JSON-RPC) : `device_id` (identifiant anonyme tiré au hasard par l'application), `text`, `lang` (`fr`, `en`, `es`, `pt`, `de`, `it` ; autre valeur : `fr`), `count` (5 à 15). Réponse : `title`, `summary`, `questions` (`kind`, `question`, `choices`, `answer`, `explanation`, `quote`, `skill`), `remaining`.
- `POST /comprendre/api/v1/quota` : `remaining`, `daily`, `max_chars`, `enabled`, `limited` (`failures`, `blocked` ou vide), `languages`.
- **Quota** par appareil et par jour (défaut 5) : la demande est **réservée** (`En cours`) sous verrou, puis la transaction est validée et le verrou relâché **avant** l'appel à Claude ; la réservation compte déjà dans le quota. **Plafond par adresse IP** (défaut 40/jour) et limitation de débit (20 demandes / 10 min / IP).
- **Échecs facturés plafonnés** : un appel où Claude a répondu sans quiz utilisable (citations non vérifiées, JSON illisible, refus, réponse tronquée) est `Écartée` et compté à part ; au-delà de `comprendre.daily_rejected_cap` (3) par jour, l'appareil n'a plus d'IA jusqu'au lendemain (`failures`). Un échec du service (indisponible, délai, réseau) ne coûte rien et ne compte pas.
- **Délais** : 100 s par essai, 1 nouvel essai (≤ 200 s) ; l'application attend 240 s (1.2.0). Une demande restée `En cours` plus d'une heure (processus interrompu) est libérée par une tâche planifiée horaire.
- **Pas d'invention** : chaque question doit citer mot pour mot un passage du texte ; sinon elle est écartée. Moins de 3 questions valides : le quiz est refusé et l'application garde son quiz hors ligne.
- **Journal** (Comprendre > Demandes de quiz IA) : jetons consommés, **coût estimé en dollars** (prix public du modèle réellement utilisé, table `PRICES` de `models/comprendre.py`), durée, résultat, y compris les échecs. Vues liste, **tableau croisé** (coût par mois et par résultat) et **graphique** (coût par jour) ; regroupements par jour, mois, modèle. Le texte lui-même n'est pas conservé.

## Codes IA

Un **code IA** donne des quiz IA en plus du quota gratuit du jour, à une personne ou à un groupe (école, entreprise, partenaire).

- **Créer** : Comprendre → **Créer des codes IA** (par lots : nombre de codes, bénéficiaire, quiz offerts, plafond par jour, appareils par code, date limite), ou Comprendre → **Codes IA** → Nouveau (un code choisi, par exemple `ECOLE-2026` : 6 caractères au moins, majuscules/espaces/tirets indifférents). Les codes tirés au hasard ont 8 caractères sans 0/O ni 1/I/L (`7KQ3-M9PX`, 40 bits). Pour les distribuer : liste → sélection → Exporter.
- **Activer** (application) : Réglages → Quiz IA → J'ai un code IA → `POST /comprendre/api/v1/redeem` (`device_id`, `code`). Le code est lié à l'appareil anonyme ; au plus `max_devices` appareils (1 par défaut). Un code inconnu, désactivé, expiré ou épuisé est refusé (`code_unknown`, `code_disabled`, `code_expired`, `code_exhausted`) sans occuper de place ; un code plein, `code_taken`. 10 essais / 10 min par adresse IP et par appareil (`AUTH_010`).
- **Consommation** : le quota gratuit d'abord, puis le code valable qui expire le plus tôt (verrou sur le code : un code partagé ne se dépense pas deux fois). Seuls les quiz réussis (ou en cours) consomment un crédit. Les quiz payés par un code ne comptent ni dans le quota gratuit ni dans le plafond par réseau (une classe sur le même Wi-Fi n'est pas bloquée). Le plafond d'échecs facturés (`daily_rejected_cap`) s'applique toujours.
- **Suivre** : sur la fiche d'un code, appareils activés (bouton **Libérer** quand quelqu'un change de téléphone ; les quiz faits restent comptés), quiz IA et leur coût. Dans le journal, colonne et regroupement **Code IA**. **Désactiver** arrête le code tout de suite.
- `/quota` renvoie aussi `free_remaining`, `code_remaining` et `codes` (`remaining` = gratuits + codes, lu tel quel par les applications 1.2) ; depuis 19.0.1.5.0, `all_codes` (terminés compris, 90 jours) avec pour chaque code `state`, `used`, `used_by_me`, `max_devices`, `devices`, `activated`, `last_used`, `source` (`purchase`/`gift`), `transfers_left`.
- **Transfert par le détenteur** (19.0.1.5.0) : un code à un seul appareil, saisi sur un autre téléphone par une application qui envoie `can_transfer`, est refusé avec `code_transferable` (+ `transfers_left`) ; renvoyé avec `transfer: true`, il passe sur le nouveau téléphone (l'ancien le perd, trace dans l'historique du code). Borné par **Transferts permis** (3 par défaut) ; au-delà, `code_taken`, et un gestionnaire libère la place. Un code partagé plein répond `code_full`. Les applications 1.3/1.4 reçoivent `code_taken` comme avant.
- **Retrait par l'utilisateur** : `POST /code/release` (`device_id`, `code`) libère la place de ce téléphone ; le crédit reste sur le code (pour le donner).
- **Lots** (`comprendre.ai.code.batch`) : **Créer des codes IA** réunit les codes dans un lot (nom, contact, préfixe facultatif `LYC-7KQ3-M9PX`, validité en jours ou jusqu'à une date, aperçu avant création). Le lot suit codes activés, quiz utilisés, coût, et permet **Ajouter des codes**, **Modifier tous les codes**, **Imprimer les cartes**, **Désactiver le lot**.
- **Modifier des codes** (liste ou lot, assistant `comprendre.ai.code.update`) : ajouter des quiz, prolonger (un code expiré repart d'aujourd'hui), fixer ou retirer la date, plafond du jour, appareils, remettre les transferts à zéro, (dés)activer. Chaque code garde l'historique de ses modifications (chatter).
- **Cartes à imprimer** (PDF, 6 par page) : code, quiz, date limite, marche à suivre et un QR vers `/comprendre/code/<code>`, page publique qui affiche le code (bouton Copier) et les liens d'installation, sans consulter la base (elle ne dit pas si un code existe).
- Filtres : en cours, pas encore utilisés, épuisés, expirés, expirent sous 14 jours, presque épuisés, partagés, achetés/offerts ; regroupements par lot, origine, date limite.

## Vente de quiz IA (Wave, module `queue_payment`)

Les utilisateurs achètent des quiz IA dans l'application ; le paiement confirmé **délivre un code IA activé sur leur téléphone** (rien à saisir).

- **Offres** : Comprendre → Ventes de quiz IA → **Offres** (nom, quiz IA, prix, validité en jours, plafond par jour). Trois offres d'exemple sont livrées **désactivées** : fixez les prix d'après le coût réel (Demandes de quiz IA → tableau croisé), puis activez-les.
- **Société qui encaisse** : paramètre `comprendre.company_id`, sinon la société principale ; devise = celle de la société (XOF en production).
- **Paiement** (`comprendre.ai.order`, encaissable par `queue.payable.mixin`) :
  - **API Wave Checkout** activée sur la société (Paramètres de la société → Wave : clé API, secret webhook) : session Wave, confirmation par webhook signé sur `/queue/wave/webhook/<id société>` → code délivré tout seul. À déclarer côté compte marchand Wave.
  - Sinon **lien marchand** (de la société, de la plateforme, ou celui de l'opérateur en dernier recours, car Comprendre est un produit de la plateforme) : l'acheteur paie, puis un agent **valide** dans Comprendre → Ventes de quiz IA → **Achats à valider** (bouton Paiements → Valider) après avoir vu l'argent sur le compte Wave ; l'acheteur peut donner l'identifiant de sa transaction (colonne *Transaction Wave*).
  - Une session Wave impossible retombe sur le lien marchand.
- **Garanties** : un achat renvoyé avec la même clé (`client_ref`) est le même achat, avec le même paiement ; un webhook rejoué ne crédite pas deux fois ; une validation défaite désactive le code, une nouvelle validation le réactive (jamais deux codes) ; achats non payés annulés au bout de 30 jours (tâche quotidienne) ; un appareil ne voit et ne touche que ses achats.
- **API** : `/shop` (offres, achats de l'appareil), `/buy` (`pack_id`, `client_ref` → achat + URL Wave ; 10 / 10 min / IP), `/orders` (achats + quota), `/order/wave-ref` ; page de retour `/comprendre/paiement/<réf>`.
- **Lien marchand Wave de Comprendre** (Réglages → *Encaissement des achats de quiz IA*, paramètre `comprendre.wave_link`) : ne sert qu'aux achats de quiz IA (crochet `_wave_merchant_link` de `queue_payment`) et passe avant le lien de la société, qui peut servir à d'autres produits. Le lien est vérifié (`https://pay.wave.com/m/…`) et nettoyé (montant retiré, il est ajouté à chaque achat). Ordre : lien de Comprendre → lien de la société → lien par défaut de la plateforme → compte de l'opérateur (alerte sur le tableau de bord). Les réglages résument où va l'argent et comment il est confirmé (API Wave : automatique ; sinon un agent), et ouvrent la page Wave (100 F, sans payer) pour vérifier le nom du marchand. La **société qui encaisse** s'y choisit aussi (`comprendre.company_id`).
- **Tarifs** (19.0.1.6.0) : `/shop` donne pour chaque offre `price_per_quiz`, `saving_percent` (par rapport à l'offre la plus chère par quiz ; affiché dès 5 %), `highlight` (« Le plus choisi », coché sur l'offre), `best_value`, `recommended`, et `advice` (`extra_monthly` : quiz voulus au-delà du gratuit sur 30 jours, c.-à-d. payés par un code ou refusés faute de quota ; `free_enough`). Conseil : la plus grosse offre ≤ 3 mois de ce besoin et consommée avant sa date limite, sinon la plus petite ; aucun conseil sans besoin. Les demandes refusées faute de quota sont journalisées (état *Quota atteint*, sans coût) ; le tableau de bord les montre (« Demande au-delà du gratuit »).
- **Calculer les prix** (Ventes de quiz IA, ou bouton sur les Offres ; assistant `comprendre.ai.pricing`) : coût mesuré par quiz réussi (échecs facturés compris) sur 90 jours, ou estimation d'après le modèle (6 000 + 3 000 jetons) s'il y a moins de 20 quiz ; prix d'un quiz = coût × (1 + sécurité 30 %) ÷ ((1 − commission Wave 1 %) × (1 − marge 40 %)) ; remise par offre, arrondi au palier supérieur (50 F), jamais sous le plancher (coût + sécurité + commission). Affiche prix, prix par quiz, coût, marge ; **Appliquer aux offres** crée ou met à jour les offres (et les met en vente si coché).
- Dépend de `queue_payment` (déjà installé en production pour Kavola).

## Tableau de bord

Page d'accueil de l'application Comprendre (action client OWL `comprendre_dashboard`, données de `comprendre.dashboard.get_data(jours)` en quelques requêtes SQL agrégées) :

- **Alertes** : service fermé, clé API absente, beaucoup d'échecs aujourd'hui (crédit Anthropic épuisé ?), achats à valider.
- **Quiz IA** (7, 30 ou 90 jours) : réussis (dont aujourd'hui), gratuits / payés par un code, coût estimé ($ et F CFA, par quiz), taux d'échec, appareils actifs ; graphiques par jour (quota gratuit / codes ; coût), ou tableau.
- **Codes IA** : en cours, pas encore utilisés (dont distribués depuis plus de 30 jours), quiz IA encore dus (et leur coût IA à venir), expirent sous 14 jours, épuisés / expirés ; lots les plus actifs ; codes à surveiller (expirent bientôt, presque épuisés).
- **Ventes** : encaissé, coût IA des codes vendus et marge, achats à valider, offres en vente.

Chaque chiffre ouvre la liste correspondante. Réservé au groupe *Comprendre — Gestionnaire*.

## Guide d'utilisation

`/apps/comprendre/guide` et `/apps/comprendre/guide.pdf`. Source unique : `comprendre/docs/GUIDE_UTILISATEUR.md` ; `reports/user_guide.xml` est **produit**. Après une modification du Markdown, depuis le dossier `sunday_jobs` :

```bash
python3 docs/md_to_guide_qweb.py comprendre
```

## Réglages

**Comprendre → Configuration → Réglages** (Paramètres → Comprendre) : service ouvert, clé API (masquée ; indique si la variable d'environnement la fournit), modèle, taux du dollar, quotas et protections. Ces réglages écrivent les paramètres système ci-dessous.

| Clé | Défaut | Rôle |
|---|---|---|
| `comprendre.usd_xof_rate` | 600 | F CFA pour 1 $ (tableau de bord : coût IA comparé aux ventes) |
| `comprendre.wave_link` | — | Lien marchand Wave des achats de quiz IA (sinon celui de la société) |
| `comprendre.company_id` | société principale | Société qui encaisse les achats |

## Paramètres système

| Clé | Défaut | Rôle |
|---|---|---|
| `comprendre.anthropic_api_key` | — | Clé API Anthropic (sinon variable d'environnement `ANTHROPIC_API_KEY`) |
| `comprendre.model` | `claude-opus-5-5` | Modèle Claude utilisé |
| `comprendre.daily_quota` | 5 | Quiz IA par appareil et par jour |
| `comprendre.ip_daily_cap` | 40 | Quiz IA par adresse IP et par jour (réussis, en cours et écartés) |
| `comprendre.daily_rejected_cap` | 3 | Appels facturés sans quiz utilisable, par appareil et par jour |
| `comprendre.max_chars` | 30000 | Longueur maximale d'un texte |
| `comprendre.enabled` | 1 | `0` coupe le service (l'application reste en hors ligne) |

Le coût réel se lit dans le journal (colonne et tableau croisé **Coût estimé**). Un modèle moins cher se règle avec `comprendre.model` (`claude-sonnet-5-5`, `claude-haiku-5-5`) ; les prix de la table `PRICES` sont à tenir à jour si Anthropic les change.

## Tests

```bash
cd ../odoo_dev && ./test.sh comprendre_backend
```

L'appel à Claude est simulé dans les tests : aucun appel réel, aucun coût.

## Mise en production

Le SDK `anthropic` est déjà dans l'image Odoo de production. Sur le serveur : `./pull_addons.sh --clone comprendre_backend` puis `./update_module.sh comprendre_backend iyf.ebng.world install prod`, et renseigner la clé API.
