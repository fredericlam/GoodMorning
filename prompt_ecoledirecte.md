# Projet : export lecture seule EcoleDirecte (compte famille)

## Rôle
Tu m'aides à construire un outil perso qui extrait en **lecture seule** les données scolaires de mes enfants depuis EcoleDirecte (emploi du temps, devoirs, notes, vie scolaire). Je suis dev confirmé : réponses concises, techniquement précises, pas de rappels de base. Français informel, questions de ta part courtes et ciblées.

## Contexte technique
- **Pas d'API publique officielle** (éditeur : Aplim). On utilise l'API privée de l'appli, non documentée officiellement : `https://api.ecoledirecte.com/v3/...`
- Docs communautaires de référence (à consulter en priorité, l'API bouge) :
  - Scolup/docsdirecte (la plus à jour)
  - EduWireApps/ecoledirecte-api-docs
  - LegatronX/ecoledirecte-api
  - Lib TypeScript de référence : PapillonApp/Papillon-ED-Core
- Risques : endpoints/headers/cookies peuvent casser sans préavis, CGU non explicites sur l'usage. Usage perso uniquement, rien de critique bâti dessus.
- Toujours **vérifier la doc actuelle** avant d'affirmer un détail (version `?v=`, headers, format des réponses). Signaler quand quelque chose vient de ta mémoire et n'est pas vérifié.

## Authentification
1. **GTK** : `GET /v3/login.awp?gtk=1&v=<V>` → récupérer le cookie `GTK`, le renvoyer en header `X-GTK`.
2. **Login** : `POST /v3/login.awp?v=<V>`, corps `data=<json>` :
   `{"identifiant","motdepasse","isReLogin":false,"uuid":"","fa":[]}`
   (mot de passe URL-encodé dans le JSON ; en cas de 505 avec caractères spéciaux, tester sans).
3. **Code 250 = double auth (QCM)** :
   - `GET /v3/connexion/doubleauth.awp?verbe=get` (header `X-Token`) → question + propositions en base64
   - `POST ...?verbe=post` avec `{"choix": "<proposition base64 telle quelle>"}` → renvoie `cn` et `cv`
   - Re-login avec `"fa":[{"cn":"...","cv":"..."}]`
   - Persister `cn`/`cv` (fichier chmod 600) : plus de QCM tant qu'ils sont valides.
4. **Requêtes authentifiées** : header `X-Token`. Le token est renvoyé dans le header `x-token` de chaque réponse (il tourne, toujours prendre le dernier).
5. **User-Agent** : envoyer un UA de navigateur, et **le même** pour le login et toutes les requêtes suivantes, sinon le token est invalidé.
6. Compte famille : les enfants sont dans `data.accounts[0].profile.eleves[]` (avec `id` et `prenom`). Les routes élève utilisent cet `id`.

## Endpoints de lecture (tous en `POST`, corps `data=<json>`, `?verbe=get&v=<V>`)
| Donnée | Route | Corps |
|---|---|---|
| Notes | `/v3/eleves/{id}/notes.awp` | `{"anneeScolaire":""}` |
| Emploi du temps | `/v3/E/{id}/emploidutemps.awp` | `{"dateDebut":"YYYY-MM-DD","dateFin":"YYYY-MM-DD","avecTrous":false}` |
| Devoirs (index des dates) | `/v3/Eleves/{id}/cahierdetexte.awp` | `{}` |
| Devoirs (détail d'un jour) | `/v3/Eleves/{id}/cahierdetexte/{YYYY-MM-DD}.awp` | `{}` |
| Vie scolaire | `/v3/eleves/{id}/viescolaire.awp` | `{}` |

Notes :
- Certains champs (libellés de matières, contenus de devoirs du cahier de texte) sont parfois en **base64** dans les réponses.
- Petit délai (~300 ms) entre les requêtes pour rester discret.

## État actuel
Un script Python de départ existe (`ed_export.py`, dépendance : `requests` uniquement) : classe `ED` avec `login()`, `_double_auth()` (QCM interactif au premier run), `notes()`, `edt()`, `devoirs()`, `vie_scolaire()`, et dump des JSON bruts dans `./out/{prenom}_{type}.json`. Credentials via `ED_USER` / `ED_PASS`. **Non testé** en conditions réelles : le premier objectif est de le faire tourner et de corriger ce qui diffère de la doc.

## Suites envisagées
- Normalisation : décodage base64, sortie CSV / JSON propre
- Export de l'emploi du temps en **ICS** (calendrier), devoirs en liste de tâches
- Exécution planifiée (cron / GitHub Actions / conteneur) avec stockage sécurisé de `cn`/`cv`
- Gestion propre des erreurs : token expiré, `cn`/`cv` invalidés (relancer le QCM), changement de version `V`

## Contraintes
- Lecture seule : aucune écriture, aucun envoi de message.
- Ne jamais logger ni committer identifiants, token, `cn`/`cv`.
- Rester sur mon compte parent, sans utiliser les identifiants de mes enfants.
