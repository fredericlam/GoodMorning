# ed-export

Outil perso : export **lecture seule** des données scolaires (emploi du temps, devoirs, notes, vie scolaire) depuis EcoleDirecte via un **compte famille**, avec un dashboard local.

> API privée non documentée par Aplim. Tout peut casser sans préavis. Usage perso uniquement. Contexte complet : [`prompt_ecoledirecte.md`](prompt_ecoledirecte.md).

- [Installation](#installation)
- [Premier fetch](#premier-fetch)
- [Commandes](#commandes)
- [Configuration](#configuration)
- [Sorties](#sorties)
- [Dashboard](#dashboard)
- [Fonctionnement de l'API](#fonctionnement-de-lapi)
- [Dépannage](#dépannage)
- [Sécurité](#sécurité)
- [Suites](#suites)

## Installation

Python ≥ 3.9. Une seule dépendance : `requests`.

```sh
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```


## Données personnelles (jamais versionnées)

Tout ce qui concerne la famille reste hors du dépôt (`.gitignore`) :

| Fichier / dossier | Contenu |
|---|---|
| `.env.local` | identifiants EcoleDirecte, `ED_DASH_TOKEN`, `ED_PAPA_PASS` |
| `out/` | dumps API, `dashboard.json`, fichiers téléchargés |
| `sources/`, `archives/` | menus, devoirs importés, bulletins et livrets scannés |
| `espaces.json`, `garde.json`, `enseignants.json`, `evenements.json` | prénoms, garde, portraits, rendez-vous |
| `PROMPT.md` | notes de travail |

Pour démarrer : copier les modèles fictifs de `exemples/` à la racine puis les adapter
(`cp exemples/{espaces,garde,enseignants,evenements}.json .`). Les modèles d'import
(`exemples/modele_devoirs.csv`, `exemples/modele_menus.csv`) se copient dans le dossier d'import (`sources/`).
Le code ne contient aucun nom : prénoms, établissements et enseignants viennent uniquement de ces fichiers.

## Premier fetch

Le premier run déclenche en général le **QCM de double authentification** (code 250). Il faut le lancer **dans un vrai terminal** : si stdin n'est pas un TTY, le script s'arrête au lieu de répondre au QCM.

1. **Renseigner `.env.local`** (déjà créé, chmod 600, gitignoré) :

   ```sh
   ED_USER='identifiant-parent'
   ED_PASS='mot de passe'
   ```

   Utilise les identifiants du **compte parent**, pas ceux d'un enfant. Mets des guillemets simples si le mot de passe contient des caractères spéciaux.

2. **Lancer le fetch :**

   ```sh
   python ed_export.py fetch
   ```

3. **Répondre au QCM** s'il apparaît :

   ```
   QCM : Quelle est votre année de naissance ?
     1. 1978
     2. 1981
     ...
   Réponse (numéro) : 2
   ```

   Tape le numéro. Le script renvoie la proposition en base64 telle quelle (une réponse mal encodée peut **bloquer le compte**, avec un mail de déblocage). Une fois le QCM validé, `cn`/`cv` sont enregistrés dans `~/.config/ed-export/state.json` (chmod 600) et les runs suivants passent sans QCM.

4. **Vérifier le résultat :**

   ```sh
   ls -l ~/.config/ed-export/state.json   # -rw-------
   ls out/                                 # {prenom}_{notes,edt,devoirs,vie_scolaire}.json + dashboard.json
   python ed_export.py serve               # http://127.0.0.1:8765
   ```

   Pour chaque enfant, la sortie affiche `→ Prénom`, puis éventuellement `  <type>: [code] message` si un endpoint échoue. Les autres données sont quand même exportées, et le dashboard affiche un bandeau rouge pour ce qui manque.


**Au premier run, surveille surtout :**

| Symptôme | Cause probable | Action |
|---|---|---|
| `[505] identifiants refusés` | identifiant/mot de passe, ou encodage | vérifier `.env.local` ; le corps est entièrement URL-encodé, le mot de passe part en clair dans le JSON |
| `cookie GTK absent` | flow GTK modifié | vérifier [docsdirecte › GTK](https://github.com/Scolup/docsdirecte/blob/main/docs/connexion/gtk.md) |
| `aucun élève dans accounts[0].profile.eleves` | structure du compte famille différente | les clés `account`/`profile` sont affichées → adapter `ED.eleves()` |
| `[520] Token invalide` pendant le QCM | headers de la double auth (`2FA-Token`, `X-Gtk`) | voir [Séquence de login](#fonctionnement-de-lapi) ; comparer avec EcoleDirecteMCP |
| `[517]` | version `v=` obsolète | `ED_VERSION=x.y.z` (voir [Configuration](#configuration)) |
| Pas de repère « moyenne classe » dans le dashboard | champ `moyenneClasse` absent (non documenté) | rien à faire, ou adapter `norm_notes()` |

## Commandes

| Commande | Effet |
|---|---|
| `python ed_export.py fetch` | Login, récupération de tout pour chaque enfant, écriture de `out/` |
| `python ed_export.py vacances` | Rafraîchit seulement le calendrier des vacances (open data Éducation nationale), sans login ED |
| `python ed_export.py normalize` | Reconstruit `out/dashboard.json` depuis les dumps bruts, **sans réseau** (utile après une évolution de la normalisation) |
| `python ed_export.py demo` | Écrit `out/demo.json` (2 enfants factices), sans toucher aux vraies données |
| `python ed_export.py serve [--port 8765] [--demo] [--lan]` | Sert le dashboard sur `127.0.0.1` ; `--demo` sert `out/demo.json` ; `--lan` l'ouvre au réseau local (voir [iPad](#ipad--réseau-local)) |
| `python ed_export.py livret <pdf> <Prénom> [--dry-run]` | Importe un export « Livret scolaire » complet (PDF texte) dans `archives/<Prénom>/` : ajoute seulement les périodes du primaire manquantes (`bilan.json` + pages extraites), liste les doublons |

`serve` expose `/` → `dashboard/index.html` et `/data.json` → `out/dashboard.json`, plus quelques routes protégées par la même clé : `/papa.json`, `/files/…` et `/archives/…` (PDF indexés uniquement), `/revisions/…` (PDF listés dans `out/revisions/index.json`) et `POST /refresh` (bouton « Mettre à jour » : relance `fetch` ou `normalize`). Tout le reste renvoie 404.

## Configuration

Tout passe par variables d'environnement, lues depuis `.env.local` à la racine du projet (format `KEY=VALUE`, `#` pour les commentaires, guillemets optionnels). Une variable déjà définie dans le shell est prioritaire sur le fichier. Le script prévient si `.env.local` n'est pas en chmod 600.

| Variable | Défaut | Rôle |
|---|---|---|
| `ED_USER` / `ED_PASS` | — | Identifiants du compte parent (requis pour `fetch`, à mettre dans `.env.local`) |
| `ED_VERSION` | `7.12.1` | Paramètre `v=` des requêtes |
| `ED_UA` | UA Safari macOS | User-Agent, identique sur toute la session |
| `ED_STATE` | `~/.config/ed-export/state.json` | Fichier de persistance `cn`/`cv` |
| `ED_DASH_TOKEN` | générée | Clé d'accès du dashboard en `--lan` (créée et ajoutée à `.env.local` au premier lancement) |
| `ED_ACADEMIE` | détectée | Académie pour le calendrier des vacances (ex. `Lyon`) si la détection échoue |

Constantes dans le script : `DELAY = 0.3` (secondes entre requêtes), `MAX_DEVOIR_DAYS = 15` (nombre max de jours de cahier de texte détaillés).

## iPad / réseau local

```sh
python ed_export.py serve --lan
```

- Le serveur écoute sur toutes les interfaces. Il affiche l'URL réseau (`http://<ip>:8765/?k=<clé>`) et une page **QR code** : `http://127.0.0.1:8765/connect`, à ouvrir sur le Mac et à scanner avec l'appareil photo de l'iPad.
- **Accès protégé** : hors localhost, chaque requête doit porter la clé `ED_DASH_TOKEN`, soit dans l'URL (`?k=`), soit dans le cookie `HttpOnly` posé à la première visite. Sinon, réponse 401. `/connect` n'est servi qu'en localhost.
- Sur l'iPad : « Partager → Sur l'écran d'accueil » donne une web-app plein écran. L'URL garde la clé, parce que la web-app iOS n'a pas les cookies de Safari.
- ⚠️ HTTP non chiffré : la clé circule en clair sur le réseau. À réserver à un réseau domestique de confiance, **pas** un réseau d'entreprise ou un Wi-Fi public. Pour révoquer, supprimer `ED_DASH_TOKEN` de `.env.local` : une nouvelle clé est générée au lancement suivant.
- macOS peut demander d'autoriser Python à accepter les connexions entrantes (pare-feu).

## Événements (stage, examens…)

`evenements.json` (racine, gitignoré) liste des dates saisies à la main, affichées en comptes à rebours et sur la frise de l'année :

```json
[
  {"nom": "Stage d'observation", "debut": "2026-12-07", "fin": "2026-12-11", "heure": "08:00", "heureFin": "17:00", "icone": "briefcase", "enfant": "Léa"},
  {"nom": "Brevet", "debut": "2027-06-24", "heure": "08:00", "icone": "certificate", "note": "…", "enfant": "Léa"}
]
```

`icone` : un nom d'icône du dashboard (`briefcase`, `certificate`, `graduation-cap`…). `estimation: true` ajoute un badge « date estimée ». Après une modification, lancer `normalize`.

## Sorties

### Dumps bruts

`out/{prenom}_{type}.json` contient le `data` de la réponse API, sans transformation :

| Fichier | Endpoint | Portée |
|---|---|---|
| `{prenom}_notes.json` | `/eleves/{id}/notes.awp` | année en cours |
| `{prenom}_edt.json` | `/E/{id}/emploidutemps.awp` | lundi de la semaine en cours → +13 jours |
| `{prenom}_devoirs.json` | `/Eleves/{id}/cahierdetexte.awp` + `/cahierdetexte/{date}.awp` | `{index, jours}` : index des échéances + détail des 15 premières dates |
| `{prenom}_vie_scolaire.json` | `/eleves/{id}/viescolaire.awp` | année en cours |
| `{prenom}_eleve.json` | — | identité (id, classe, établissement), relue par `normalize` |
| `famille/messages_{received,sent}.json` | `/familles/{id}/messages.awp` | listes (structure réelle : `data.messages.{received,sent,…}`) |
| `famille/messages_detail.json` | `/familles/{id}/messages/{mid}.awp` | contenu des messages, **en cache** : seuls les nouveaux sont demandés (ce qui les marque probablement lus sur ED) |
| `famille/documents.json` | `/familledocuments.awp` | bulletins, administratif, vie scolaire, inscriptions, factures, pièces à verser / téléversements |
| `famille/factures.json` | `/factures.awp` | liste directe (≠ `{invoices}` documenté) |
| `files/` + `files/index.json` | `/telechargement.awp` | documents, factures et PJ téléchargés **une seule fois** ; type réel détecté (ED renvoie `application/force-download`) |
| `calendrier.json` | open data `fr-en-calendrier-scolaire` | vacances de l'académie (année en cours + suivante) |

### `out/dashboard.json` (normalisé)

Base64 décodé, HTML des devoirs converti en texte, notes converties en nombres (`"12,5"` → `12.5`).

```jsonc
{
  "generated_at": "2026-09-30T10:16:45",
  "semaine": ["2026-09-28", "2026-10-11"],
  "demo": true,                       // présent uniquement en mode démo
  "children": [{
    "id": 1234, "prenom": "…", "classe": "…", "etablissement": "…",
    "erreurs": ["vie_scolaire"],      // endpoints en échec
    "notes": {
      "niveaux": [{ "niveau": 1, "libelle": "Maîtrise insuffisante", "couleur": "#ff0000" }],   // compétences
      "periodes": [{
        "code": "A001", "libelle": "1er Trimestre", "debut": "…", "fin": "…", "annuel": false,
        "releve": false,                 // true pour A001R001… (relevés intermédiaires)
        "cloture": false, "conseil": "YYYY-MM-DD", "heureConseil": "17:30", "pp": "…",
        "moyenne": null, "moyenneClasse": null, "min": null, "max": null,   // null sauf parametrage.moyenneGenerale
        "disciplines": [{ "matiere": "…", "code": "MATHS", "groupe": false, "moyenne": 14.2, "moyenneClasse": 11.8,
                          "min": 4.5, "max": 18, "coef": 1, "rang": 12, "effectif": 26, "profs": ["…"] }]
      }],
      "notes": [{
        "id": 1, "date": "YYYY-MM-DD", "matiere": "…", "codeMatiere": "…", "devoir": "…", "periode": "A001",
        "valeur": 12.5,               // null si non numérique (Abs, Disp…) → voir "brut"
        "type": "DS", "brut": "12,5", "sur": 20, "coef": 1, "nonSignificatif": false,
        "moyenneClasse": 11.2, "minClasse": 4, "maxClasse": 18,   // null si absents
        "commentaire": "",
        "competences": [{ "libelle": "…", "descriptif": "…", "niveau": 3 }]
      }]
    },
    "edt": [{ "start": "YYYY-MM-DD HH:MM", "end": "…", "matiere": "…", "code": "MATHS", "prof": "…", "salle": "…",
              "groupe": "…", "couleur": "#rrggbb", "type": "COURS|CONGE", "annule": false, "modifie": false,
              "dispense": false, "devoir": false, "seance": false }],
    "devoirs": [{ "date": "échéance", "matiere": "…", "code": "…", "donneLe": "…", "effectue": false,
                  "interrogation": false, "rendreEnLigne": false, "prof": "…",
                  "contenu": "texte", "seance": "contenu de séance (texte)", "documents": ["nom.pdf"] }],
    "vie": [{ "type": "Absence|Retard|…", "date": "…", "detail": "…", "libelle": "…", "motif": "…",
              "justifie": true }]      // null pour sanctions/encouragements
  }]
}
```

Règles de normalisation :
- Les périodes `examenBlanc` sont exclues. Les relevés intermédiaires sont gardés mais marqués `releve`.
- Un `coef` à 0 (ou absent) devient 1 : ED renvoie 0 quand l'établissement n'affiche pas les coefficients (`parametrage.coefficientNote = false`).
- `sur` vaut 20 par défaut si `noteSur` est illisible.

## Dashboard

`dashboard/index.html` : un seul fichier, sans framework. Il charge `/data.json`, et les fichiers via `/files/…` (seuls ceux indexés par `fetch` sont servis). Les polices viennent de Google Fonts (repli système hors ligne).

**Icônes** : [Phosphor Icons](https://phosphoricons.com) (MIT), style duotone. Les SVG utilisés sont embarqués dans la page par `scripts/build_icons.py`, qui prend la liste `ICONS` du script : ajouter un nom puis relancer (`npm pack @phosphor-icons/core && tar xzf phosphor-icons-core-*.tgz && python scripts/build_icons.py package/`). Les langues vivantes utilisent des drapeaux ronds en SVG maison (`FLAGS`).

Direction visuelle pop et douce : fond crème avec dégradés blush et lilas, cartes en verre dépoli, mode sombre « night pop » (bascule mémorisée). Chaque matière a un nom court, une icône et une couleur fixes, reconnus par mots-clés (tableau `SUBJECTS`).

**Onglets** (mémorisés, adressables par `#messagerie`, `#documents`, `#scolarite/notes`…) :
- **Scolarité**, en trois sous-rubriques :
  - **Aperçu** : salut, cours en cours ou prochain cours, tuiles **J-X** de même taille, triées par échéance (prochain contrôle, week-end, vacances, conseil de classe, événements), frise et liste des vacances ;
  - **Emploi du temps & devoirs** ;
  - **Notes & moyennes** : moyenne générale et sélecteur de période, moyennes par matière, dernières notes, courbe, compétences, vie scolaire.
- **Messagerie** : reçus / envoyés / avec PJ, recherche plein texte, lecture avec liens cliquables et pièces jointes. Sur mobile : liste puis lecture.
- **Documents**, en deux sous-rubriques :
  - **Reçus** : documents publiés par l'établissement par catégorie, plus toutes les pièces jointes reçues ;
  - **Mes dépôts & envois** : pièces à verser et téléversements, documents à signer, messages envoyés avec leurs PJ.
- **Factures** : liste, montant si fourni, aperçu PDF.

Une visionneuse intégrée affiche les PDF et les images (Échap pour fermer), avec « Ouvrir » et « Télécharger ».

Responsive : ordinateur, iPad (onglets en icônes, messagerie en liste puis lecture en portrait) et téléphone. Sur écran tactile, les info-bulles s'affichent au toucher.

**Comptes à rebours à la seconde**, avec un sablier animé qui se vide : cours en cours ou prochain cours, prochain contrôle, conseil de classe, prochain week-end (fin du dernier cours de la semaine d'après l'EDT), prochaines vacances, et les événements de `evenements.json`. La carte « Vacances, stage & examens » ajoute une frise de l'année scolaire et la liste des vacances de l'académie.

Sections :
- **Accueil** : « Coucou {prénom} », puces résumé (cours restants, devoirs pour demain, prochain contrôle, cours annulés), et carte **En ce moment / Prochain cours** avec compte à rebours et barre de progression. Rafraîchi chaque minute.
- **Indicateurs** : anneau de moyenne générale (écart à la classe), devoirs à faire avec répartition sur les 5 prochains jours, prochain contrôle, compte à rebours avant le conseil de classe (date, heure, prof principal·e).
- **Emploi du temps**, deux vues (choix mémorisé) :
  - **Jour** : sélecteur des 10 jours d'école chargés (pastilles de couleur des matières), timeline avec cours doubles fusionnés, pauses (🧃 pause, 🍜 pause déj, ☁️ heure libre), cours en cours surligné avec progression, cours passés estompés. Par défaut le jour courant, ou le suivant si la journée est finie.
  - **Semaine** : grille lun–ven « candy », cette semaine ou la suivante, trait de l'heure actuelle, badges ✕ annulé / ✦ modifié / 📝 devoir, détail au survol.
- **Devoirs** : filtres À faire / Contrôles / Tout, groupés par échéance (« Pour demain » mis en avant), avec contenu, « En classe » (contenu de séance), pièces jointes (noms), prof, date de don.
- **Dernières notes** : note, type (DS, oral…), coef, et une réglette qui place la note face au min–max et à la moyenne de la classe. Compétences évaluées en pastilles colorées par niveau.
- **Moyennes par matière** (pleine largeur) : une réglette par matière, avec une étiquette pour la moyenne de l'élève, un trait pour la moyenne de classe, une bande pour le min–max, un repère à 10 et l'écart à la classe à droite. Moyennes officielles quand l'établissement les publie, sinon estimées depuis les notes.
- **Ma courbe** et **Compétences**, côte à côte : moyenne cumulée au fil des notes, et répartition par niveau de maîtrise (libellés ED).
- **Vie scolaire** : absences, retards, sanctions/encouragements. Le code ED 210 (« aucune donnée ») est traité comme « rien à signaler ».

Le sélecteur de période masque les relevés intermédiaires (`A001R001`…). Le rang dans la classe n'est **pas** affiché, même s'il est présent dans les données : l'établissement le masque (`parametrage.moyenneRang = false`).

Calcul des moyennes :
- par matière : moyenne officielle si présente, sinon moyenne pondérée par `coef` des notes ramenées sur 20 (hors `nonSignificatif`) ;
- générale : moyenne officielle de la période **uniquement si** `parametrage.moyenneGenerale = true`. Sinon, les champs `moyenne*` de la période ne sont pas des moyennes (observé : nombre de matières notées / nombre de matières) et on calcule la moyenne des moyennes par matière, pondérée par le coef matière.

Pour modifier le dashboard, il suffit de recharger la page : `serve` relit les fichiers à chaque requête.

## Fonctionnement de l'API

Référence : [Scolup/docsdirecte](https://github.com/Scolup/docsdirecte) (docs v7.12.1, dernier commit juillet 2026, lu le 2026-09-30). [PapillonApp/Papillon-ED-Core](https://github.com/PapillonApp/Papillon-ED-Core) est plus ancien (v6.15.1, avril 2024) : utile pour la structure, pas pour les détails.

**Format commun** : `POST https://api.ecoledirecte.com/v3/<route>?verbe=get&v=<V>`, `Content-Type: application/x-www-form-urlencoded`, corps `data=<json URL-encodé>`. Tout le JSON est encodé : sinon un `+` (base64 du QCM, mot de passe) serait lu comme un espace. Réponse : `{code, token, message, data}`.

**Séquence de login** (`ED.login()`). Alignée sur l'appli web telle que reproduite par [EcoleDirecteMCP](https://github.com/jeromeboivin/EcoleDirecteMCP) (commit du 2026-09-12), la doc docsdirecte étant incomplète sur la double auth :

1. **Reset** : cookies, token et `2FA-Token` sont vidés.
2. **GTK** : `GET /login.awp?gtk=1&v=<V>`. La valeur est lue dans le header `X-GTK`, sinon dans `token` du corps, sinon dans le cookie `GTK`. Elle est renvoyée en `X-Gtk`.
3. **Login** : `POST /login.awp` avec `{identifiant, motdepasse, isReLogin:false, uuid:"", fa:<cn/cv stockés ou []>}`.
4. **Code 250 → double auth** (`ED._double_auth()`) :
   - `doubleauth.awp?verbe=get` puis `?verbe=post` avec `{"choix": <proposition base64 telle quelle>}`, **sans** `X-Gtk` mais avec `X-Token` et `2FA-Token` ;
   - on récupère `cn`/`cv`, puis nouveau bootstrap GTK ;
   - re-login avec `cn`/`cv` **à plat et** dans `fa: [{cn, cv, uniq:false}]` ;
   - `fa` n'est persisté qu'après un re-login réussi (code 200).

   Si `cn`/`cv` stockés étaient envoyés et qu'on reçoit quand même 250, ils sont considérés comme invalidés : nouveau QCM.
5. **Headers suivis sur chaque réponse** : `x-token` (sinon champ `token`), `2FA-Token`, `X-GTK`. Ils sont renvoyés sur toutes les requêtes suivantes.
6. **Expiration** : un code 520/525/526 (token invalide ou expiré) déclenche un re-login automatique, une seule fois par requête.

**Codes connus** : 200 OK · 250 QCM requis · 505 identifiants · 512 corps invalide · 517 version obsolète · 520 token invalide · 525/526 session expirée · 403 interdit · 210 objet introuvable.

**Base64 décodé** : contenus du cahier de texte (`aFaire.contenu`), question et propositions du QCM. Seuls ces champs connus sont décodés, parce qu'un décodage « au jugé » corromprait des chaînes ASCII qui se trouvent être du base64 valide.

**Hypothèses non vérifiées** (à confirmer au premier fetch réel) :
- enfants dans `data.accounts[0].profile.eleves[]` (d'après le prompt ; la page « parents » de docsdirecte montre une autre forme) ;
- `moyenneClasse` sur les notes (absent de la doc) ;
- champs de `sanctionsEncouragements` (tableau vide dans la doc) ;
- `classe.libelle` / `nomEtablissement` sur les objets élève.

## Dépannage

- **Réponse non-JSON** : l'URL ou la version ne sont probablement plus valides. Le message affiche les 200 premiers caractères de la réponse.
- **Le QCM revient à chaque run** : `cn`/`cv` ne sont pas acceptés. Vérifie que `ED_STATE` pointe toujours au même endroit et que le User-Agent n'a pas changé entre les runs.
- **Repartir de zéro** : `rm ~/.config/ed-export/state.json` (le prochain run redemandera le QCM).
- **Nouvelle version d'API** : récupérer la valeur dans docsdirecte ou dans les requêtes du site web (DevTools → Network → `login.awp?v=…`), puis `ED_VERSION=x.y.z`.
- **Dashboard « Pas encore de données »** : lancer `fetch` avant `serve` (ou `demo` puis `serve --demo`).

## Sécurité

- **Lecture seule** : uniquement `verbe=get` sur les données. Les seuls `POST` « actifs » sont le login et la réponse au QCM.
- **Rien de secret dans le dépôt** : identifiants dans `.env.local` (gitignoré, chmod 600), `cn`/`cv` hors du repo (`~/.config/ed-export/`, dossier 700, fichier 600), `out/` gitignoré (données des enfants).
- Aucun identifiant, token ou `cn`/`cv` n'est affiché ni loggé.
- `serve` écoute sur `127.0.0.1` uniquement. Le dashboard échappe tout contenu issu de l'API avant de l'insérer dans le DOM.
- ~300 ms entre requêtes, fenêtre d'EDT limitée à 2 semaines (la doc met en garde contre les plages larges).

## Suites

- Export ICS de l'emploi du temps (ou utiliser l'URL iCal officielle : `/ical/E/{id}/url.awp`).
- Devoirs en liste de tâches, export CSV.
- Exécution planifiée (cron / conteneur), avec `cn`/`cv` dans un secret store. Un QCM invalidé bloquera le run non interactif : prévoir une alerte.
- Renouvellement de token via `accessToken` + `uuid` fixe (docsdirecte › renew-token), pour ne plus stocker le mot de passe.
