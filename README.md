# comprendre_backend

Module Odoo 19 : serveur de l'application **Comprendre** (quiz de compréhension d'un texte). L'application crée ses quiz hors ligne ; ce module lui fournit, quand le réseau et le quota le permettent, un quiz de meilleure qualité rédigé par Claude (Anthropic).

## Fonctionnement

- `POST /comprendre/api/v1/quiz` (JSON-RPC) : `device_id` (identifiant anonyme tiré au hasard par l'application), `text`, `lang` (`fr`/`en`), `count` (5 à 15). Réponse : `title`, `summary`, `questions` (`kind`, `question`, `choices`, `answer`, `explanation`, `quote`, `skill`), `remaining`.
- `POST /comprendre/api/v1/quota` : quiz IA restants aujourd'hui pour l'appareil.
- **Quota** par appareil et par jour (défaut 5), vérifié sous verrou ; **plafond par adresse IP** (défaut 40/jour) et limitation de débit (20 demandes / 10 min / IP). Seuls les quiz réussis comptent.
- **Pas d'invention** : chaque question doit citer mot pour mot un passage du texte ; sinon elle est écartée. Moins de 3 questions valides : le quiz est refusé et l'application garde son quiz hors ligne.
- **Journal** (Comprendre > Demandes de quiz IA) : jetons consommés, durée, résultat, y compris les échecs. Le texte lui-même n'est pas conservé.

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
| `comprendre.ip_daily_cap` | 40 | Quiz IA par adresse IP et par jour |
| `comprendre.max_chars` | 30000 | Longueur maximale d'un texte |
| `comprendre.enabled` | 1 | `0` coupe le service (l'application reste en hors ligne) |

Coût indicatif avec `claude-opus-5-5` : environ 0,10 $ pour un texte de 30 000 caractères. Un modèle moins cher se règle avec `comprendre.model`.

## Tests

```bash
cd ../odoo_dev && ./test.sh comprendre_backend
```

L'appel à Claude est simulé dans les tests : aucun appel réel, aucun coût.

## Mise en production

Le SDK `anthropic` est déjà dans l'image Odoo de production. Sur le serveur : `./pull_addons.sh --clone comprendre_backend` puis `./update_module.sh comprendre_backend iyf.ebng.world install prod`, et renseigner la clé API.
