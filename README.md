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

## Guide d'utilisation

`/apps/comprendre/guide` et `/apps/comprendre/guide.pdf`. Source unique : `comprendre/docs/GUIDE_UTILISATEUR.md` ; `reports/user_guide.xml` est **produit**. Après une modification du Markdown, depuis le dossier `sunday_jobs` :

```bash
python3 docs/md_to_guide_qweb.py comprendre
```

## Réglages (Paramètres système)

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
