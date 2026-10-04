#!/usr/bin/env python3
"""Good Morning — export lecture seule EcoleDirecte (compte famille) + dashboard local.

Usage :
  python ed_export.py fetch       # login (identifiants dans .env.local), dump brut + out/dashboard.json
  python ed_export.py normalize   # reconstruit out/dashboard.json depuis les dumps, sans réseau
  python ed_export.py demo        # données factices → out/demo.json
  python ed_export.py serve [--port 8765] [--demo]   # dashboard sur 127.0.0.1

Réf. API : Scolup/docsdirecte (v7.12.1, vérifié 2026-09-30).
"""
import argparse
import base64
import html
import json
import os
import random
import re
import sys
import time
from datetime import date, datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote_plus, unquote


def load_env_file(path):
    """KEY=VALUE par ligne, # commentaires, guillemets optionnels. L'env réel reste prioritaire."""
    if not path.exists():
        return
    if path.stat().st_mode & 0o077:
        print(f"attention : {path.name} est lisible par d'autres (chmod 600 {path.name})", file=sys.stderr)
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        k, v = k.removeprefix("export ").strip(), v.strip()
        if len(v) >= 2 and v[0] == v[-1] and v[0] in "'\"":
            v = v[1:-1]
        os.environ.setdefault(k, v)


load_env_file(Path(__file__).resolve().parent / ".env.local")

API ="https://api.ecoledirecte.com/v3"
V = os.environ.get("ED_VERSION", "7.12.1")
# Même UA pour le login et toutes les requêtes suivantes, sinon le token saute.
UA = os.environ.get(
    "ED_UA",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/605.1.15 "
    "(KHTML, like Gecko) Version/18.0 Safari/605.1.15",
)
STATE = Path(os.environ.get("ED_STATE", Path.home() / ".config" / "ed-export" / "state.json")).expanduser()
ROOT = Path(__file__).resolve().parent
OUT = ROOT / "out"
DELAY = 0.3
MAX_DEVOIR_DAYS = 15


class EDError(Exception):
    def __init__(self, code, message=""):
        super().__init__(f"[{code}] {message}")
        self.code = code


# --- état persistant (cn/cv) ---------------------------------------------------

def load_state():
    try:
        return json.loads(STATE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_state(state):
    STATE.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd = os.open(STATE, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(state, f)
    os.chmod(STATE, 0o600)


# --- helpers de décodage -------------------------------------------------------

def b64d(s):
    if not s:
        return ""
    try:
        return base64.b64decode(s, validate=True).decode("utf-8")
    except (ValueError, UnicodeDecodeError):
        return s


def html_to_text(s):
    s = re.sub(r"(?i)<br\s*/?>|</p>|</li>|</div>", "\n", s or "")
    s = re.sub(r"(?i)<li[^>]*>", "• ", s)
    s = html.unescape(re.sub(r"<[^>]+>", "", s))
    return re.sub(r"\n\s*\n+", "\n", s).strip()


def to_float(v):
    if v is None:
        return None
    try:
        return float(str(v).replace(",", ".").strip())
    except ValueError:
        return None


# --- client --------------------------------------------------------------------

class ED:
    def __init__(self, user, password):
        import requests  # import tardif : `demo`/`serve` n'en ont pas besoin

        self.user, self.password = user, password
        self.s = requests.Session()
        self.s.headers.update({
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/x-www-form-urlencoded",
        })
        self.token = None
        self.gtk = None
        self.twofa = None  # header 2FA-Token, à renvoyer tel quel une fois reçu
        self.account = None

    def _capture(self, r):
        self.gtk = r.headers.get("x-gtk") or self.gtk
        self.twofa = r.headers.get("2fa-token") or self.twofa

    def _headers(self, gtk=True):
        headers = {}
        if self.token:
            headers["X-Token"] = self.token
        if self.twofa:
            headers["2FA-Token"] = self.twofa
        if gtk and self.gtk:
            headers["X-Gtk"] = self.gtk
        return headers

    def _post(self, path, payload, verbe=None, gtk=True, params=None):
        params = {**(params or {}), "v": V}
        if verbe:
            params["verbe"] = verbe
        headers = self._headers(gtk)
        time.sleep(DELAY)
        # JSON entièrement URL-encodé : sinon un « + » (base64 du QCM, mot de passe) devient un espace.
        r = self.s.post(API + path, params=params, headers=headers,
                        data="data=" + quote_plus(json.dumps(payload)), timeout=30)
        self._capture(r)
        tok = r.headers.get("x-token")
        try:
            j = r.json()
        except ValueError:
            raise EDError(r.status_code, f"réponse non-JSON sur {path}: {r.text[:200]!r}")
        self.token = tok or j.get("token") or self.token
        return j

    @staticmethod
    def _ok(j):
        if j.get("code") != 200:
            raise EDError(j.get("code"), j.get("message", ""))
        return j.get("data")

    def _gtk(self):
        time.sleep(DELAY)
        self.gtk = None
        r = self.s.get(API + "/login.awp", params={"gtk": 1, "v": V}, timeout=30)
        self._capture(r)
        try:  # certaines réponses portent le GTK dans le corps plutôt qu'en cookie
            self.gtk = r.json().get("token") or self.gtk
        except ValueError:
            pass
        self.gtk = self.gtk or r.cookies.get("GTK") or self.s.cookies.get("GTK")
        if not self.gtk:
            raise EDError(r.status_code, "cookie GTK absent")

    def login(self):
        state = load_state()
        fa = state.get("fa", [])
        body = {"identifiant": self.user, "motdepasse": self.password, "isReLogin": False, "uuid": "", "fa": fa}

        self.s.cookies.clear()
        self.token = self.twofa = None
        self._gtk()
        j = self._post("/login.awp", body)
        if j.get("code") == 250:
            if fa:
                print("cn/cv refusés, nouveau QCM.", file=sys.stderr)
            cn, cv = self._double_auth()
            # Comme l'appli web : cn/cv à plat ET dans fa, GTK re-bootstrappé avant le re-login.
            body.update(cn=cn, cv=cv, fa=[{"cn": cn, "cv": cv, "uniq": False}])
            self._gtk()
            j = self._post("/login.awp", body)
            if j.get("code") == 200:
                save_state({**load_state(), "fa": body["fa"]})
        if j.get("code") == 505:
            raise EDError(505, "identifiants refusés")
        data = self._ok(j)
        self.account = data["accounts"][0]
        return self.account

    def _double_auth(self):
        d = self._ok(self._post("/connexion/doubleauth.awp", {}, verbe="get", gtk=False))
        if not sys.stdin.isatty():
            raise EDError(250, "QCM requis : relancer en interactif une fois pour obtenir cn/cv")
        props = d["propositions"]
        print("\nQCM :", b64d(d["question"]))
        for i, p in enumerate(props):
            print(f"  {i + 1}. {b64d(p)}")
        while True:
            try:
                idx = int(input("Réponse (numéro) : ")) - 1
                if 0 <= idx < len(props):
                    break
            except ValueError:
                pass
        # Renvoyer la proposition base64 telle quelle (sinon risque de blocage du compte).
        r = self._ok(self._post("/connexion/doubleauth.awp", {"choix": props[idx]}, verbe="post", gtk=False))
        return r["cn"], r["cv"]

    def get(self, path, payload=None, params=None, _retry=True):
        j = self._post(path, payload or {}, verbe="get", params=params)
        if j.get("code") in (520, 525, 526) and _retry:  # token invalide/expiré
            self.login()
            return self.get(path, payload, params, _retry=False)
        return self._ok(j)

    def eleves(self):
        prof = self.account.get("profile", {})
        return prof.get("eleves") or []

    def notes(self, eid):
        return self.get(f"/eleves/{eid}/notes.awp", {"anneeScolaire": ""})

    def notes_annee(self, eid, annee):
        return self.get(f"/eleves/{eid}/notes.awp", {"anneeScolaire": annee})

    def edt(self, eid, d1, d2):
        return self.get(f"/E/{eid}/emploidutemps.awp",
                        {"dateDebut": d1, "dateFin": d2, "avecTrous": False})

    def devoirs(self, eid):
        index = self.get(f"/Eleves/{eid}/cahierdetexte.awp") or {}
        days = {}
        for d in sorted(index)[:MAX_DEVOIR_DAYS]:
            days[d] = self.get(f"/Eleves/{eid}/cahierdetexte/{d}.awp")
        return {"index": index, "jours": days}

    def vie_scolaire(self, eid):
        try:
            return self.get(f"/eleves/{eid}/viescolaire.awp")
        except EDError as err:
            if err.code == 210:  # « Aucune donnée à afficher » : rien à signaler, pas une erreur
                return {}
            raise

    # --- compte famille ---

    def family_id(self):
        return self.account.get("id") or self.account.get("idLogin")

    def messages(self, fid, box="received"):
        params = {"force": "false", "typeRecuperation": box, "idClasseur": 0, "orderBy": "date", "order": "desc",
                  "query": "", "onlyRead": "", "page": 0, "itemsPerPage": 100, "getAll": 0}
        return self.get(f"/familles/{fid}/messages.awp", params=params)

    def message(self, fid, mid, mode="destinataire"):
        # Probablement marqué « lu » côté ED dès qu'on récupère le contenu.
        return self.get(f"/familles/{fid}/messages/{mid}.awp", {"anneeMessages": annee_scolaire()},
                        params={"mode": mode})

    def documents(self):
        return self.get("/familledocuments.awp", params={"archive": ""})

    def factures(self):
        return self.get("/factures.awp")

    def download(self, file_id, file_type, extra=None):
        """Renvoie (bytes, content_type, nom_de_fichier) ; lève EDError si ED répond du JSON d'erreur."""
        params = {"verbe": "get", "fichierId": file_id, "leTypeDeFichier": file_type, "v": V, **(extra or {})}
        payload = {"forceDownload": 0, "archive": False, "anneeArchive": ""}
        time.sleep(DELAY)
        r = self.s.post(API + "/telechargement.awp", params=params, headers=self._headers(gtk=False),
                        data="data=" + quote_plus(json.dumps(payload)), timeout=60)
        self._capture(r)
        self.token = r.headers.get("x-token") or self.token
        ctype = (r.headers.get("content-type") or "").split(";")[0].strip()
        if r.status_code != 200 or ctype in ("application/json", "text/html"):
            try:
                j = r.json()
                raise EDError(j.get("code"), j.get("message", ""))
            except ValueError:
                raise EDError(r.status_code, f"téléchargement {file_id} : réponse {ctype or '?'}")
        m = re.search(r'filename\*?=(?:UTF-8'')?"?([^";]+)', r.headers.get("content-disposition", ""))
        return r.content, ctype, (m.group(1) if m else "")


# --- normalisation -------------------------------------------------------------

def norm_notes(raw):
    raw = raw or {}
    param = raw.get("parametrage") or {}
    niveaux = [{"niveau": i, "libelle": b64d(param.get(f"libelleEval{i}", "")), "couleur": param.get(f"couleurEval{i}", "")}
               for i in range(1, 5) if param.get(f"libelleEval{i}")]
    # Hors parametrage.moyenneGenerale, les champs moyenne* de la période ne sont pas des moyennes
    # (observé : nb de matières notées / nb de matières).
    generale = bool(param.get("moyenneGenerale"))
    periodes = []
    for p in raw.get("periodes", []):
        if p.get("examenBlanc"):
            continue
        code = p.get("codePeriode") or p.get("idPeriode") or ""
        em = p.get("ensembleMatieres") or {}
        periodes.append({
            "code": code,
            "libelle": p.get("periode"),
            "debut": p.get("dateDebut"),
            "fin": p.get("dateFin"),
            "annuel": bool(p.get("annuel")),
            "releve": bool(re.search(r"R\d+$", code)),  # relevés intermédiaires (A001R001…)
            "cloture": bool(p.get("cloture")),
            "conseil": p.get("dateConseil") or "",
            "heureConseil": p.get("heureConseil") or "",
            "pp": em.get("nomPP") or "",
            "moyenne": to_float(em.get("moyenneGenerale")) if generale else None,
            "moyenneClasse": to_float(em.get("moyenneClasse")) if generale else None,
            "min": to_float(em.get("moyenneMin")) if generale else None,
            "max": to_float(em.get("moyenneMax")) if generale else None,
            "disciplines": [{
                "matiere": d.get("discipline"),
                "code": d.get("codeMatiere"),
                "groupe": bool(d.get("groupeMatiere")),
                "moyenne": to_float(d.get("moyenne")),
                "moyenneClasse": to_float(d.get("moyenneClasse")),
                "min": to_float(d.get("moyenneMin")),
                "max": to_float(d.get("moyenneMax")),
                "coef": to_float(d.get("coef")),
                "rang": d.get("rang") or None,
                "effectif": d.get("effectif") or None,
                "profs": [x.get("nom") for x in d.get("professeurs") or [] if x.get("nom")],
            } for d in em.get("disciplines", [])],
        })
    notes = []
    for n in raw.get("notes", []):
        coef = to_float(n.get("coef")) or 0
        notes.append({
            "id": n.get("id"),
            "date": n.get("date"),
            "saisie": n.get("dateSaisie") or None,  # date de saisie par l'enseignant (≠ date du devoir)
            "matiere": n.get("libelleMatiere") or n.get("codeMatiere"),
            "codeMatiere": n.get("codeMatiere"),
            "devoir": n.get("devoir"),
            "type": n.get("typeDevoir") or "",
            "periode": n.get("codePeriode"),
            "valeur": to_float(n.get("valeur")),
            "brut": n.get("valeur"),
            "sur": to_float(n.get("noteSur")) or 20,
            # coef 0 quand l'établissement n'affiche pas les coefs (parametrage.coefficientNote=false)
            "coef": coef if coef > 0 else 1,
            "nonSignificatif": bool(n.get("nonSignificatif")),
            "moyenneClasse": to_float(n.get("moyenneClasse")),
            "minClasse": to_float(n.get("minClasse")),
            "maxClasse": to_float(n.get("maxClasse")),
            "commentaire": n.get("commentaire") or "",
            "competences": [{
                "libelle": e.get("libelleCompetence") or "",
                "descriptif": e.get("descriptif") or "",
                "niveau": int(e["valeur"]) if str(e.get("valeur", "")).isdigit() else None,
            } for e in n.get("elementsProgramme") or []],
        })
    return {"periodes": periodes, "notes": notes, "niveaux": niveaux}


def norm_edt(raw):
    out = []
    for c in raw or []:
        out.append({
            "start": c.get("start_date"),
            "end": c.get("end_date"),
            "matiere": c.get("matiere") or c.get("text"),
            "code": c.get("codeMatiere", ""),
            "prof": c.get("prof", ""),
            "salle": c.get("salle", ""),
            "groupe": c.get("groupe", ""),
            "couleur": c.get("color", ""),
            "type": c.get("typeCours", "COURS"),
            "annule": bool(c.get("isAnnule")),
            "modifie": bool(c.get("isModifie")),
            "dispense": bool(c.get("dispense")),
            "devoir": bool(c.get("devoirAFaire")),
            "seance": bool(c.get("contenuDeSeance")),
        })
    return sorted(out, key=lambda c: c["start"] or "")


def norm_devoirs(raw):
    raw = raw or {}
    out = []
    for d, items in sorted((raw.get("index") or {}).items()):
        detail = {m.get("id"): m for m in ((raw.get("jours") or {}).get(d) or {}).get("matieres", [])}
        for it in items:
            m = detail.get(it.get("idDevoir"), {})
            af = m.get("aFaire") or {}
            seance = m.get("contenuDeSeance") or {}
            out.append({
                "date": d,
                "matiere": it.get("matiere"),
                "code": it.get("codeMatiere", ""),
                "donneLe": it.get("donneLe"),
                "effectue": bool(af.get("effectue", it.get("effectue"))),
                "interrogation": bool(it.get("interrogation")),
                "rendreEnLigne": bool(it.get("rendreEnLigne")),
                "prof": m.get("nomProf", ""),
                "contenu": html_to_text(b64d(af.get("contenu", ""))),
                "seance": html_to_text(b64d(seance.get("contenu", ""))),
                "documents": [x.get("libelle") for x in af.get("documents", [])],
            })
    return out


def norm_vie(raw):
    raw = raw or {}
    ev = []
    for a in raw.get("absencesRetards", []):
        ev.append({
            "type": a.get("typeElement"),
            "date": a.get("date"),
            "detail": a.get("displayDate", ""),
            "libelle": a.get("libelle", ""),
            "motif": a.get("motif", ""),
            "justifie": bool(a.get("justifie")),
        })
    for a in raw.get("sanctionsEncouragements", []):
        ev.append({
            "type": a.get("typeElement") or "Sanction",
            "date": a.get("date"),
            "detail": a.get("displayDate", ""),
            "libelle": a.get("libelle", ""),
            "motif": a.get("motif", ""),
            "justifie": None,
        })
    return sorted(ev, key=lambda e: e["date"] or "", reverse=True)


def annee_scolaire(today=None):
    today = today or date.today()
    y = today.year if today.month >= 8 else today.year - 1
    return f"{y}-{y + 1}"


def box_messages(raw, box):
    """messages.awp renvoie data.messages.{received,sent,draft,archived} (≠ doc communautaire)."""
    msgs = (raw or {}).get("messages") or {}
    return msgs.get(box, []) if isinstance(msgs, dict) else msgs


ROLES = {"A": "Administration", "P": "Professeur", "E": "Élève", "F": "Famille", "C": "Vie scolaire"}


def person_name(p):
    if not isinstance(p, dict):
        return str(p or "")
    if p.get("name"):
        return p["name"]
    return " ".join(x for x in (p.get("civilite"), p.get("prenom"), p.get("particule"), p.get("nom")) if x).strip()


DOC_CATEGORIES = {  # catégorie familledocuments → (libellé, leTypeDeFichier)
    "notes": ("Bulletins & compétences", "Note"),
    "viescolaire": ("Vie scolaire", "VieScolaire"),
    "administratifs": ("Administratif", ""),
    "inscriptions": ("Inscriptions & signatures", None),  # None → champ `type` de l'entrée
    "entreprises": ("Stages & entreprises", ""),
    "factures": ("Factures", "Facture"),
}


def norm_famille(raw, files):
    """raw : dumps famille ; files : index des fichiers téléchargés {clé: {path, name, mime, size}}."""
    raw = raw or {}
    details = raw.get("messages_detail") or {}
    messages = []
    for box in ("received", "sent"):
        for m in box_messages(raw.get(f"messages_{box}"), box):
            d = details.get(str(m.get("id"))) or {}
            html_content = d.get("contentHtml") or b64d(d.get("content") or m.get("content") or "")
            atts = d.get("files") or m.get("files") or m.get("attachments") or []
            messages.append({
                "id": m.get("id"),
                "box": box,
                "date": m.get("date"),
                "subject": m.get("subject") or "(sans objet)",
                "from": person_name(m.get("from")),
                "fromRole": ROLES.get((m.get("from") or {}).get("role", ""), ""),
                "to": [person_name(t) for t in m.get("to") or []],
                "read": bool(m.get("read")),
                "answered": bool(m.get("answered")),
                "content": html_to_text(html_content) if html_content else None,
                "attachments": [{"id": a.get("id"), "name": a.get("libelle") or a.get("name") or "pièce jointe",
                                 "file": files.get(f"pj/{a.get('id')}")} for a in atts],
            })
    messages.sort(key=lambda m: m["date"] or "", reverse=True)
    stats = (raw.get("messages_received") or {}).get("pagination") or {}
    fact_raw = raw.get("factures")
    fact_raw = fact_raw if isinstance(fact_raw, list) else (fact_raw or {}).get("invoices") or []

    docs = raw.get("documents") or {}
    documents = []
    for cat, (label, _) in DOC_CATEGORIES.items():
        for d in docs.get(cat) or []:
            documents.append({
                "cat": cat, "catLabel": label, "id": d.get("id"), "libelle": d.get("libelle") or "Document",
                "date": d.get("date") or "", "type": d.get("type") or "",
                "signatureDemandee": bool(d.get("signatureDemandee")),
                "signatures": d.get("etatSignatures") or [],
                "file": files.get(f"doc/{cat}/{d.get('id')}"),
            })
    factures = []
    seen = set()
    for f in fact_raw + (docs.get("factures") or []):
        if f.get("id") in seen:
            continue
        seen.add(f.get("id"))
        factures.append({"id": f.get("id"), "libelle": f.get("libelle") or "Facture", "date": f.get("date") or "",
                         "type": f.get("type") or "", "montant": to_float(f.get("montant")),
                         "file": files.get(f"doc/factures/{f.get('id')}")})
    factures.sort(key=lambda f: f["date"], reverse=True)

    pav = docs.get("listesPiecesAVerser") or {}
    personnes = {p.get("id"): " ".join(x for x in (p.get("prenom"), p.get("nom")) if x) for p in pav.get("personnes") or []}
    return {
        "messages": messages,
        "messagesStats": {"recus": stats.get("messagesRecusCount", stats.get("messagesReceivedCount")),
                          "nonLus": stats.get("messagesRecusNotReadCount", stats.get("messagesUnreadCount")),
                          "envoyes": stats.get("messagesEnvoyesCount", stats.get("messagesSentCount"))},
        "documents": sorted(documents, key=lambda d: d["date"], reverse=True),
        "factures": factures,
        # Structure des pièces à verser non documentée : on garde le brut + les personnes, le front s'adapte.
        "pieces": {"listes": pav.get("listesPieces") or [], "pieces": pav.get("pieces") or [],
                   "televersements": pav.get("televersements") or [], "personnes": personnes},
        "erreurs": [k for k in ("messages_received", "documents", "factures") if raw.get(k) is None],
    }


def write_json(path, obj):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, ensure_ascii=False, indent=2))


def week_bounds(today=None):
    today = today or date.today()
    monday = today - timedelta(days=today.weekday())
    return monday, monday + timedelta(days=13)


# --- commandes -----------------------------------------------------------------

def cmd_fetch(_args):
    user, pw = os.environ.get("ED_USER"), os.environ.get("ED_PASS")
    if not user or not pw:
        sys.exit("ED_USER / ED_PASS manquants : renseigne .env.local")
    ed = ED(user, pw)
    ed.login()
    eleves = ed.eleves()
    if not eleves:
        sys.exit("aucun élève dans accounts[0].profile.eleves (compte famille ?)\n"
                 f"  clés account : {sorted(ed.account)}\n"
                 f"  clés profile : {sorted(ed.account.get('profile') or {})}")
    d1, d2 = week_bounds()
    children = []
    for e in eleves:
        eid, prenom = e["id"], e.get("prenom", str(e["id"]))
        print(f"→ {prenom}", file=sys.stderr)
        raw = {}
        for kind, fn in (("notes", lambda: ed.notes(eid)),
                         ("edt", lambda: ed.edt(eid, d1.isoformat(), d2.isoformat())),
                         ("devoirs", lambda: ed.devoirs(eid)),
                         ("vie_scolaire", lambda: ed.vie_scolaire(eid))):
            try:
                raw[kind] = fn()
                write_json(OUT / f"{prenom}_{kind}.json", raw[kind])
            except EDError as err:
                print(f"  {kind}: {err}", file=sys.stderr)
                raw[kind] = None
        ident = {"id": eid, "prenom": prenom, "classe": (e.get("classe") or {}).get("libelle", ""),
                 "etablissement": e.get("nomEtablissement", "")}
        write_json(OUT / f"{prenom}_eleve.json", ident)
        fetch_past_notes(ed, eid, prenom)  # notes des années passées seulement (pas d'EDT, messages ni documents archivés)
        children.append(build_child(ident, raw))
    famille = fetch_famille(ed)
    calendrier = None
    academie, source = find_academie(ed.s, eleves)
    if academie:
        try:
            calendrier = {"academie": academie, "source": source, "vacances": fetch_vacances(ed.s, academie)}
            write_json(OUT / "calendrier.json", calendrier)
        except Exception as err:
            print(f"  vacances : {err}", file=sys.stderr)
    write_dashboard(children, [d1.isoformat(), d2.isoformat()], famille=famille,
                    calendrier=calendrier or load_json(OUT / "calendrier.json"))


OPENDATA = "https://data.education.gouv.fr/api/explore/v2.1/catalog/datasets"


def find_academie(session, children_raw):
    """ED_ACADEMIE > RNE de l'établissement > nom (si non ambigu). Données publiques de l'annuaire Éducation."""
    if os.environ.get("ED_ACADEMIE"):
        return os.environ["ED_ACADEMIE"], "config"
    for e in children_raw:
        rne = next((e.get(k) for k in ("rneEtablissement", "codeRNE", "rne", "uai") if e.get(k)), None)
        # Le nom ED (« Centre scolaire Saint-X ») ≠ annuaire (« Collège Saint-X ») : on cherche les mots
        # distinctifs et on ne garde que les établissements qui les contiennent tous.
        tokens = [w for w in re.findall(r"[\w'-]+", fold(e.get("nomEtablissement", ""))) if len(w) > 3 and w not in STOP]
        if rne:
            where = f'identifiant_de_l_etablissement="{rne}"'
        elif tokens:
            where = f'search(nom_etablissement, "{" ".join(tokens)}")'
        else:
            continue
        try:
            r = session.get(f"{OPENDATA}/fr-en-annuaire-education/records", timeout=20,
                            params={"where": where, "select": "libelle_academie,nom_etablissement", "limit": 100}).json()
        except Exception as err:  # réseau, JSON…
            print(f"  annuaire : {err}", file=sys.stderr)
            continue
        acads = {x.get("libelle_academie") for x in r.get("results", []) if x.get("libelle_academie")
                 and (rne or all(t in re.findall(r"[\w'-]+", fold(x.get("nom_etablissement", ""))) for t in tokens))}
        if len(acads) == 1:
            return acads.pop(), "rne" if rne else "nom"
        print(f"  académie ambiguë ({len(acads)} candidates) : renseigne ED_ACADEMIE dans .env.local "
              f"(clés élève : {sorted(e)})", file=sys.stderr)
    return None, None


STOP = {"centre", "scolaire", "college", "lycee", "ecole", "groupe", "institution", "ensemble", "prive", "privee",
        "general", "generale", "technologique", "professionnel", "polyvalent", "saint", "sainte", "notre", "dame"}


def fold(txt):
    import unicodedata
    return unicodedata.normalize("NFD", txt or "").encode("ascii", "ignore").decode().lower()


def cmd_vacances(_args):
    """Rafraîchit uniquement le calendrier des vacances (sans login ED)."""
    import requests
    eleves = [{"nomEtablissement": json.loads(f.read_text()).get("etablissement", "")} for f in OUT.glob("*_eleve.json")]
    s = requests.Session()
    academie, source = find_academie(s, eleves)
    if not academie:
        sys.exit("académie introuvable : renseigne ED_ACADEMIE dans .env.local")
    cal = {"academie": academie, "source": source, "vacances": fetch_vacances(s, academie)}
    write_json(OUT / "calendrier.json", cal)
    print(f"{len(cal['vacances'])} périodes de vacances · académie de {academie} ({source})", file=sys.stderr)
    cmd_normalize(_args)


def fetch_vacances(session, academie):
    """Calendrier scolaire officiel (année en cours + suivante) filtré sur l'académie."""
    y = int(annee_scolaire()[:4])
    years = [f"{y}-{y + 1}", f"{y + 1}-{y + 2}"]
    where = f'location="{academie}" and (' + " or ".join(f'annee_scolaire="{a}"' for a in years) + ")"
    r = session.get(f"{OPENDATA}/fr-en-calendrier-scolaire/records", timeout=20,
                    params={"where": where, "limit": 100, "order_by": "start_date"}).json()
    out, seen = [], set()
    for x in r.get("results", []):
        if x.get("population") not in ("-", "Élèves", "Eleves", None):
            continue  # on ignore les lignes « Enseignants »
        if x["description"].lower().startswith("début"):
            continue  # simple repère (« Début des Vacances d'Été ») sans date de reprise
        start, end = utc_to_local_date(x["start_date"]), utc_to_local_date(x["end_date"])
        if end <= start:  # pont (ex. Ascension) publié avec une durée nulle : reprise au jour ouvré suivant
            d = date.fromisoformat(start) + timedelta(days=1)
            while d.weekday() >= 5:
                d += timedelta(days=1)
            end = d.isoformat()
        key = (x["description"], start)
        if key in seen:
            continue
        seen.add(key)
        out.append({"nom": x["description"], "debut": start, "reprise": end, "zone": x.get("zones", ""),
                    "annee": x.get("annee_scolaire")})
    return out


def utc_to_local_date(s):
    from zoneinfo import ZoneInfo
    return datetime.fromisoformat(s).astimezone(ZoneInfo("Europe/Paris")).date().isoformat()


FAM = OUT / "famille"
FILES = OUT / "files"


MAGIC = [(b"%PDF", "application/pdf", ".pdf"), (b"\xff\xd8\xff", "image/jpeg", ".jpg"),
         (b"\x89PNG", "image/png", ".png"), (b"GIF8", "image/gif", ".gif"), (b"PK\x03\x04", "application/zip", ".zip")]


def sniff(content, ctype, name=""):
    """ED sert tout en application/force-download : on déduit le vrai type du contenu (puis de l'extension)."""
    for magic, mime, ext in MAGIC:
        if content[:len(magic)] == magic:
            if mime == "application/zip" and name.lower().endswith((".docx", ".xlsx", ".pptx", ".odt")):
                break
            return mime, ext
    if content[:12].startswith(b"RIFF") and content[8:12] == b"WEBP":
        return "image/webp", ".webp"
    import mimetypes
    guess = mimetypes.guess_type(name)[0]
    ext = Path(name).suffix.lower()
    return (guess or (ctype if ctype and "force-download" not in ctype else "application/octet-stream")), ext or ".bin"


def load_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def fetch_famille(ed):
    fid = ed.family_id()
    print("→ famille", file=sys.stderr)
    raw = {}
    for kind, fn in (("messages_received", lambda: ed.messages(fid, "received")),
                     ("messages_sent", lambda: ed.messages(fid, "sent")),
                     ("documents", ed.documents),
                     ("factures", ed.factures)):
        try:
            raw[kind] = fn()
            write_json(FAM / f"{kind}.json", raw[kind])
        except EDError as err:
            print(f"  {kind}: {err}", file=sys.stderr)
            raw[kind] = None

    # Contenu des messages : cache, on ne redemande que les nouveaux.
    details = load_json(FAM / "messages_detail.json", {})
    for box, mode in (("received", "destinataire"), ("sent", "expediteur")):
        for m in box_messages(raw.get(f"messages_{box}"), box):
            key = str(m.get("id"))
            if key in details:
                continue
            try:
                details[key] = ed.message(fid, m["id"], mode)
            except EDError as err:
                print(f"  message {key}: {err}", file=sys.stderr)
    write_json(FAM / "messages_detail.json", details)
    raw["messages_detail"] = details

    # Fichiers : téléchargés une seule fois, indexés dans out/files/index.json.
    index = load_json(FILES / "index.json", {})
    wanted = []
    docs = raw.get("documents") or {}
    for cat, (_, ftype) in DOC_CATEGORIES.items():
        for d in docs.get(cat) or []:
            wanted.append((f"doc/{cat}/{d.get('id')}", d.get("id"), d.get("type", "") if ftype is None else ftype, None,
                           d.get("libelle")))
    fact = raw.get("factures")
    for f in (fact if isinstance(fact, list) else (fact or {}).get("invoices") or []):
        wanted.append((f"doc/factures/{f.get('id')}", f.get("id"), "Facture", None, f.get("libelle")))
    for d in details.values():
        for a in (d or {}).get("files") or []:
            wanted.append((f"pj/{a.get('id')}", a.get("id"), "PIECE_JOINTE", {"anneeMessages": annee_scolaire()},
                           a.get("libelle")))
    for key, file_id, ftype, extra, label in wanted:
        if not file_id or key in index:
            continue
        try:
            content, ctype, fname = ed.download(file_id, ftype, extra)
        except EDError as err:
            print(f"  fichier {key} ({label}): {err}", file=sys.stderr)
            continue
        ctype, ext = sniff(content, ctype, fname or label or "")
        rel = f"{key.replace('/', '_')}{ext}"
        FILES.mkdir(parents=True, exist_ok=True)
        (FILES / rel).write_bytes(content)
        index[key] = {"path": rel, "name": fname or label or rel, "mime": ctype, "size": len(content)}
    write_json(FILES / "index.json", index)
    write_json(FAM / "famille_id.json", {"id": fid})
    return norm_famille(raw, index)


MENU_BANDS = ["entrees", "plats", "accompagnements", "laitages", "desserts"]
MENU_LEGEND = re.compile(r"^(produits? (locaux|bio|frais)|le produit maison|la s[ée]lection du chef)$", re.I)
JOURS = ["LUNDI", "MARDI", "MERCREDI", "JEUDI", "VENDREDI"]
MOIS = ["janvier", "fevrier", "mars", "avril", "mai", "juin", "juillet", "aout", "septembre", "octobre", "novembre", "decembre"]


def parse_menu_pdf(path, published):
    """Menu cantine (tableau LUNDI…VENDREDI, une bande par catégorie) → {date ISO: {catégorie: [plats]}}.
    Mots positionnés via `pdftotext -bbox`, bandes via les filets horizontaux du tableau (`pdftocairo -svg`).
    Nécessite poppler ; renvoie {} si le document ne ressemble pas au format attendu."""
    import subprocess
    import tempfile
    from collections import defaultdict
    try:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdftotext", "-f", "1", "-l", "1", "-bbox", str(path), f"{tmp}/w.html"], check=True, capture_output=True, timeout=30)
            subprocess.run(["pdftocairo", "-f", "1", "-l", "1", "-svg", str(path), f"{tmp}/p.svg"], check=True, capture_output=True, timeout=30)
            words_html, svg = Path(f"{tmp}/w.html").read_text(), Path(f"{tmp}/p.svg").read_text()
    except (OSError, subprocess.SubprocessError):
        return {}
    words = [(float(x0), float(y0), float(x1), html.unescape(w)) for x0, y0, x1, w in
             re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="[\d.]+">([^<]*)</word>', words_html)]
    heads = {w: (x0 + x1) / 2 for x0, y0, x1, w in words if w in JOURS}
    # « du 28 septembre au 2 octobre » ou « du 5 AU 9 OCTOBRE » (mois seulement après le 2e jour)
    m = re.search(r"du\s+(\d{1,2})(?:er)?(?:\s+(?:au|-)\s+\d{1,2}(?:er)?)?\s+(\w+)", " ".join(w for *_, w in words), re.I)
    if len(heads) < 5 or not m or fold(m.group(2)) not in MOIS:
        return {}
    head_y = min(y0 for x0, y0, x1, w in words if w in JOURS)
    # Filets horizontaux traversant tout le tableau = limites des bandes (entrées | plats | …).
    span = max(heads.values()) - min(heads.values())
    lines = []
    for d in re.findall(r'<path[^>]*? d="([^"]+)"', svg):
        nums = list(map(float, re.findall(r"-?[\d.]+", d)))
        xs, ys = nums[0::2], nums[1::2]
        if len(nums) >= 4 and max(xs) - min(xs) > span and max(ys) - min(ys) < 3 and min(ys) > head_y:
            lines.append(min(ys))
    bounds = sorted({round(y) for y in lines})
    # Date du lundi : jour + mois du titre, année la plus proche de la publication.
    pub = date.fromisoformat(published[:10])
    day, month = int(m.group(1)), MOIS.index(fold(m.group(2))) + 1
    monday = min((date(y, month, day) for y in (pub.year - 1, pub.year, pub.year + 1)), key=lambda d: abs((d - pub).days))
    monday -= timedelta(days=monday.weekday())
    cells = defaultdict(lambda: defaultdict(list))  # jour → y → mots
    for x0, y0, x1, w in words:
        # entre l'en-tête et le bas de la dernière bande (la légende en dessous est ignorée)
        if y0 <= head_y + 5 or (bounds and y0 > bounds[min(len(bounds) - 1, len(MENU_BANDS))]):
            continue
        col = min(heads, key=lambda j: abs(heads[j] - (x0 + x1) / 2))
        cells[col][round(y0)].append((x0, w))
    out = {}
    for i, jour in enumerate(JOURS):
        rows = [" ".join(w for _, w in sorted(ws)) for _, ws in sorted(cells[jour].items())]
        ys = sorted(cells[jour])
        if not rows:
            continue
        d = (monday + timedelta(days=i)).isoformat()
        # Texte vertical (« A N I M A T I O N ») : journée à thème, pas de plats détaillés.
        if sum(len(r) == 1 for r in rows) >= 4:
            note = "".join(r if len(r) == 1 else f" {r} " for r in rows).split()
            out[d] = {"note": " ".join(w if len(w) <= 3 and i else w.capitalize() if not i else w.lower()
                                       for i, w in enumerate(note))}
            continue
        menu = {}
        for y, txt in zip(ys, rows):
            band = sum(1 for b in bounds if b < y) - 1 if bounds else -1
            if bounds and not 0 <= band < len(MENU_BANDS) or MENU_LEGEND.match(txt):
                continue  # légende sous le tableau (« Produit locaux… »), parfois sans filet qui la sépare
            menu.setdefault(MENU_BANDS[band] if bounds else "plats", []).append(txt.capitalize())
        if menu:
            out[d] = menu
    return out


def cantine_menus():
    """Menus trouvés dans les documents et pièces jointes (nom contenant « menu »)."""
    index = load_json(FILES / "index.json", {})
    docs = load_json(FAM / "documents.json") or {}
    sources = [(f"doc/{cat}/{d.get('id')}", d.get("libelle") or "", d.get("date") or "")
               for cat in DOC_CATEGORIES for d in docs.get(cat) or []]
    for m in (load_json(FAM / "messages_detail.json", {}) or {}).values():
        for a in (m or {}).get("files") or []:
            sources.append((f"pj/{a.get('id')}", a.get("libelle") or "", (m.get("date") or "")[:10]))
    # cumul dans out/cantine.json : EcoleDirecte retire le menu de la semaine passée quand il publie le suivant
    menus = load_json(OUT / "cantine.json", {}) or {}
    for key, label, published in sources:
        f = index.get(key)
        if not f or not re.search(r"menu", label, re.I) or f.get("mime") != "application/pdf" or not published:
            continue
        for d, menu in parse_menu_pdf(FILES / f["path"], published).items():
            menus[d] = {**menu, "source": label}
    menus = dict(sorted(menus.items()))
    write_json(OUT / "cantine.json", menus)
    return menus


def _bbox_words(path, first=None, last=None):
    """Mots d'un PDF avec position : [(page, x0, y0, x1, y1, texte)] (pdftotext -bbox)."""
    import subprocess
    import tempfile
    args = ["pdftotext", "-bbox"] + (["-f", str(first)] if first else []) + (["-l", str(last)] if last else [])
    with tempfile.TemporaryDirectory() as tmp:
        subprocess.run(args + [str(path), f"{tmp}/w.html"], check=True, capture_output=True, timeout=60)
        pages = Path(f"{tmp}/w.html").read_text().split("<page ")[1:]
    return [(pi, float(a), float(b), float(c), float(d), html.unescape(t)) for pi, page in enumerate(pages, 1)
            for a, b, c, d, t in re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', page)]


def _lines(words, ytol=3):
    """Regroupe des mots (x0, y0, x1, y1, t) en lignes [(y, [mots triés par x])]."""
    out = []
    for w in sorted(words, key=lambda w: (w[1], w[0])):
        if out and abs(out[-1][0] - w[1]) <= ytol:
            out[-1][1].append(w)
        else:
            out.append([w[1], [w]])
    return [(y, sorted(ws)) for y, ws in out]


PROF = re.compile(r"^(M\.|Mme|Mlle|Mr)$")
MENTIONS = ["Félicitations", "Compliments", "Encouragements", "Mise en garde", "Avertissement"]


def parse_bulletin_college(path):
    """Bulletin trimestriel EcoleDirecte (export Excel, 1 page) → matières (moyennes élève/classe/min/max, prof,
    appréciation), absences, avis du conseil, mention, décision. Identité/adresse volontairement ignorées."""
    words = [w[1:] for w in _bbox_words(path) if w[0] == 1]
    text = " ".join(w[4] for w in sorted(words, key=lambda w: (round(w[1]), w[0])))
    an = re.search(r"\b(20\d\d)-(20\d\d)\b", text)
    tri = re.search(r"\b(\d)(?:er|ème|e)\s+Trimestre", text)
    cls = re.search(r"Classe\s+(\S+)", text)
    if not (an and tri):
        return None
    head = next((w for w in words if w[4] == "Elève"), None)  # en-tête du tableau
    if not head:
        return None
    top = head[3] + 2
    # fin du tableau : ligne « VIE SCOLAIRE » (pas « SCIENCES VIE & TERRE »), sinon « Demi-journées »
    end = min((w[1] for i, w in enumerate(words) if w[4] == "VIE" and w[1] > top and i + 1 < len(words)
               and words[i + 1][4] == "SCOLAIRE" and abs(words[i + 1][1] - w[1]) < 3), default=None) \
        or min((w[1] for w in words if w[4].startswith("Demi-journ") and w[1] > top), default=1e9)
    left = _lines([w for w in words if w[0] < 190 and top < w[1] < end - 2])
    # blocs matière : nom (1 ou 2 lignes serrées) puis, après le contenu, la ligne du professeur
    # (« Mme X », « M. Y »… ou parfois le nom seul : on se fie à la position, pas à la civilité)
    blocks, cur = [], None
    for y, ws in left:
        t = " ".join(w[4] for w in ws)
        if cur and y - cur["y_last"] < 9 and not PROF.match(ws[0][4]):
            cur["matiere"] += " " + t
            cur["y_last"] = y
        elif cur:
            cur["prof"] = t
            blocks.append(cur)
            cur = None
        else:
            cur = {"matiere": t, "y0": y, "y_last": y}
    if cur:
        blocks.append(cur)
    num = lambda s: to_float(s) if re.fullmatch(r"\d+(?:[.,]\d+)?", s) else None
    COLS = [("eleve", 190, 222), ("classe", 222, 250), ("min", 250, 278), ("max", 278, 300)]
    matieres = []
    for i, b in enumerate(blocks):
        y0 = b["y0"] - 4
        y1 = blocks[i + 1]["y0"] - 4 if i + 1 < len(blocks) else end - 2
        ws = [w for w in words if y0 <= w[1] < y1]
        m = {"matiere": b["matiere"], "prof": b.get("prof", "")}
        for k, xa, xb in COLS:
            v = [w for w in ws if xa <= w[0] < xb and num(w[4]) is not None]
            m[k] = num(v[0][4]) if v else None
            if k == "eleve" and not v:  # « Abs », « NE », « Disp »…
                other = [w[4] for w in ws if xa <= w[0] < xb]
                if other:
                    m["statut"] = " ".join(other)
        m["appreciation"] = re.sub(r"\s+([.,!?:])", r"\1", " ".join(" ".join(w[4] for w in l) for _, l in _lines([w for w in ws if w[0] >= 300])))
        matieres.append(m)
    tail = " ".join(" ".join(w[4] for w in l) for _, l in _lines([w for w in words if w[1] >= end - 1]))
    absn = lambda label: int(m.group(1)) if (m := re.search(label + r"\D{0,6}?(\d+)", tail)) else None
    avis_words = [w for w in words if w[1] >= end and w[0] < 470]
    avis_txt = " ".join(" ".join(w[4] for w in l) for _, l in _lines(avis_words))
    avis = re.search(r"AVIS DU CONSEIL DE CLASSE\s*(.*?)(?:Le professeur principal|$)", avis_txt, re.S)
    avis = (avis.group(1) if avis else "").strip()
    avis = re.sub(r"\bLe\s+(?=[A-ZÀ-Ý])|Le Chef d\S+tablissement", "", avis, count=1).strip()  # signature du chef d'établissement
    mention = next((x for x in MENTIONS if re.search(x + r"\w*\s+(?:du|de la|des)", avis, re.I)), None) \
        or ("Félicitations" if re.search(r"conseil de classe (?:te|vous|la|le|l')\s*félicite", avis, re.I) else None)
    decision = re.search(r"((?:Passage|Maintien|Redoublement|Orientation)[^.\n]*?)(?=\s*$|\.)", avis)
    pp = re.search(r"Le professeur principal\s*:?\s*(.+)$", tail)
    return {
        "annee": f"{an.group(1)}-{an.group(2)}", "trimestre": int(tri.group(1)), "classe": cls.group(1) if cls else "",
        "matieres": matieres,
        "absences": {"demiJournees": absn(r"Demi-journ\S+ d\S+absence"), "nonJustifiees": absn(r"non justifiée\(s\)"),
                     "retards": absn(r"Retard\(s\)")},
        "avis": re.sub(r"\s+", " ", avis).strip(), "mention": mention,
        "decision": decision.group(1).strip() if decision else None,
        "pp": pp.group(1).strip() if pp else "",
    }


LSU_DISCIPLINES = [("fran", "Français"), ("math", "Mathématiques"), ("langue", "Langue vivante"),
                   ("education physique", "EPS"), ("sciences", "Sciences et technologie"), ("questionner", "Questionner le monde"),
                   ("histoire", "Histoire et géographie"), ("enseignements artistiques", "Enseignements artistiques"),
                   ("enseignement moral", "Enseignement moral et civique")]
LSU_NIVEAUX = ["Non atteints", "Partiellement atteints", "Atteints", "Dépassés"]


def ocr_fix(s):
    """Corrections des confusions OCR courantes (OmniPage) : r6sultats → résultats, 1'eleve → l'eleve, Frangais…"""
    s = re.sub(r"616v", "élèv", s)  # « 616ves » → élèves
    s = re.sub(r"(?<=[A-Za-z])61(?=[a-z])", "él", s)  # « F61icitations » → Félicitations
    s = re.sub(r"(?<=[A-Za-zé])6(?=[a-zé])|(?<![\w'])6(?=[a-z]{2})|(?<=[a-z]{2})6\b", "é", s)
    s = re.sub(r"\b1'(?=\w)|\bI'(?=\w)|\('(?=\w)", "l'", s)
    s = s.replace("Frangais", "Français").replace("frangais", "français").replace("clans", "dans")
    return re.sub(r"\s+([.,!?;:])", r"\1", re.sub(r"\s+", " ", s)).strip()


def parse_lsu(path):
    """Livret scolaire unique (primaire, scanné + OCR) → semestres : objectifs par discipline avec positionnement
    (colonne de la croix), « Acquisitions, progrès… » par discipline, appréciation générale."""
    words = _bbox_words(path)
    W = 595.0
    lvl = lambda xc: 0 if xc < 0.875 * W else 1 if xc < 0.910 * W else 2 if xc < 0.946 * W else 3
    semestres, cur, disc = {}, None, None
    meta = {}
    for page in sorted({w[0] for w in words}):
        pw = [w[1:] for w in words if w[0] == page]
        lines = _lines(pw, ytol=4)
        txt = "\n".join(" ".join(w[4] for w in ws) for _, ws in lines)
        if m := re.search(r"Semestre (\d) du (\d\d/\d\d/\d{4}) au (\d\d/\d\d/\d{4})", txt):
            cur = semestres.setdefault(int(m.group(1)), {"semestre": int(m.group(1)), "du": m.group(2), "au": m.group(3),
                                                         "objectifs": [], "commentaires": {}, "appreciation": ""})
            disc = None
            meta.setdefault("annee", (re.search(r"(20\d\d)/(20\d\d)", txt) or [None])[0])
            meta.setdefault("classe", (re.search(r"Classe de ([^\n]+)", txt) or [None, ""])[1].strip())
            meta.setdefault("enseignant", (re.search(r"Enseignant\S*\s*:\s*([^\n]+)", txt) or [None, ""])[1].strip())
        if not cur:
            continue
        # zone « socle » (grille différente) : ignorée jusqu'à l'en-tête « Acquis scolaires »
        socle = next((y for y, ws in lines if "socle" in fold(" ".join(w[4] for w in ws))), None)
        acquis = next((y for y, ws in lines if fold(" ".join(w[4] for w in ws)).startswith("acquis scolaires")), None)
        skip = (socle, acquis) if socle is not None and acquis is not None and acquis > socle else None
        if skip:  # maîtrise des composantes du socle : 4 colonnes (insuffisante → très bonne)
            SOCLE = ["Maîtrise insuffisante", "Maîtrise fragile", "Maîtrise satisfaisante", "Très bonne maîtrise"]
            for m in [w for w in pw if w[4] in ("X", "x") and skip[0] < w[1] < skip[1]]:
                xc = (m[0] + m[2]) / 2
                lab = " ".join(" ".join(w[4] for w in ws) for y, ws in _lines([w for w in pw if w[0] < 0.62 * W and abs(w[1] - m[1]) <= 11
                                                                           and skip[0] < w[1] < skip[1]], ytol=4))
                if lab:
                    cur.setdefault("socle", []).append({"composante": ocr_fix(lab), "niveau": SOCLE[0 if xc < 0.716 * W else 1 if xc < 0.806 * W else 2 if xc < 0.889 * W else 3]})
        marks = [w for w in pw if w[4] in ("X", "x") and w[0] > 0.8 * W and not (skip and skip[0] <= w[1] <= skip[1])]
        marks += [w for w in pw if fold(w[4]).startswith("evalu") and w[0] > 0.8 * W]  # « Non évalué »
        obj_lines = [(y, ws) for y, ws in _lines([w for w in pw if 0.27 * W <= w[0] < 0.82 * W], ytol=4)]
        mode, buf = None, []
        for y, ws in lines:
            left = [w for w in ws if w[0] < 0.27 * W]
            t = fold(" ".join(w[4] for w in left)).strip()
            full = " ".join(w[4] for w in ws)
            hit = next((label for key, label in LSU_DISCIPLINES if t.startswith(key)), None)
            if hit and not [w for w in ws if w[0] >= 0.27 * W]:
                disc, mode = hit, None
                continue
            ff = fold(ocr_fix(full))
            if ff.startswith("acquisitions, progres"):
                mode = "com"
                cur["commentaires"][disc or "?"] = ocr_fix(full.split(":", 1)[-1])
                continue
            if "ciation generale" in ff:
                mode = "app"
                continue
            # pied de page « NOM PRÉNOM  3/11 » : générique, aucun nom en dur
            if ff.startswith("communication avec") or re.match(r"^[a-z' -]{3,60}\s\d{1,2}/\d{1,2}$", ff):
                mode = None
                continue
            if mode == "com" and not hit:
                cur["commentaires"][disc or "?"] += " " + ocr_fix(full)
            elif mode == "app":
                cur["appreciation"] = (cur["appreciation"] + " " + ocr_fix(full)).strip()
        # objectifs : lignes de texte rattachées à la croix la plus proche (une croix centrée sur 2-3 lignes)
        groups = {id(m): [] for m in marks}
        for y, ws in obj_lines:
            near = min(marks, key=lambda m: abs(m[1] - y), default=None)
            if near and abs(near[1] - y) <= 14:
                groups[id(near)].append((y, " ".join(w[4] for w in ws)))
        for m in sorted(marks, key=lambda m: m[1]):
            texte = ocr_fix(" ".join(t for _, t in sorted(groups[id(m)])))
            if not texte:
                continue
            # discipline : dernière en-tête au-dessus de la croix sur cette page, sinon celle de la page précédente
            above = [(y, label) for y, ws in lines for key, label in LSU_DISCIPLINES
                     if y < m[1] and fold(" ".join(w[4] for w in ws if w[0] < 0.27 * W)).strip().startswith(key)
                     and not [w for w in ws if w[0] >= 0.27 * W]]
            d = above[-1][1] if above else cur.get("_disc_prev")
            niveau = None if fold(m[4]).startswith("evalu") else LSU_NIVEAUX[lvl((m[0] + m[2]) / 2)]
            cur["objectifs"].append({"discipline": d or "?", "objectif": texte, "niveau": niveau})
        cur["_disc_prev"] = disc
    out = []
    for s in sorted(semestres.values(), key=lambda s: s["semestre"]):
        s.pop("_disc_prev", None)
        s["commentaires"] = {k: v.strip() for k, v in s["commentaires"].items() if v.strip()}
        out.append(s)
    return {**meta, "semestres": out} if out else None


def parse_livret_lsu(path):
    """Export « Livret scolaire » (PDF texte, tous les bilans de la scolarité) → périodes du primaire :
    [{niveau, annee, periode, n, du, au, etablissement, enseignant, classe, pages, objectifs, appreciation, parcours}].
    Les bilans collège du même export sont listés à part (niveau 6EME…3EME) pour signaler les doublons."""
    import subprocess
    first = subprocess.run(["pdftotext", "-f", "2", "-l", "3", "-layout", str(path), "-"], capture_output=True, text=True).stdout
    total = int(re.search(r"Pages:\s+(\d+)", subprocess.run(["pdfinfo", str(path)], capture_output=True, text=True).stdout).group(1))
    som = re.sub(r"\s+", " ", first.split("Académie")[0])
    entries = [{"niveau": m[0].upper(), "periode": m[1].capitalize(), "n": int(m[2]), "annee": f"{m[3]}/{m[4]}",
                "etablissement": re.sub(r"\s\d+\.\s", " ", f" {m[5]} ").strip().title(), "page": int(m[6])}  # « 16. » = n° de l'entrée suivante
               for m in re.findall(r"Bilan périodique (\S+) (Trimestre|Semestre) (\d) (\d{4})/(\d{4}) .+? \((.+?)\) (\d+)", som)]
    for i, e in enumerate(sorted(entries, key=lambda e: e["page"])):
        nxt = sorted(entries, key=lambda e: e["page"])[i + 1]["page"] if i + 1 < len(entries) else total + 1
        e["pages"] = [e["page"], nxt - 1]
    lvl = lambda xc: LSU_NIVEAUX[0 if xc < 515 else 1 if xc < 535 else 2 if xc < 555 else 3]
    tops = {"Français", "Mathématiques", "Enseignements artistiques", "Questionner le monde", "Langues vivantes", "Éducation physique et sportive",
            "Sciences et technologie", "Histoire et géographie", "Enseignement moral et civique"}
    for e in entries:
        if re.match(r"\dEME$", e["niveau"]):
            continue  # bilans collège : déjà archivés via les bulletins
        words = _bbox_words(path, *e["pages"])
        e.update(objectifs=[], appreciation="", parcours="", enseignant="", classe="")
        disc, mode, last = None, None, None
        for page in sorted({w[0] for w in words}):
            pw = [w[1:] for w in words if w[0] == page]
            lines = _lines(pw, ytol=3)
            text = "\n".join(" ".join(w[4] for w in ws) for _, ws in lines)
            if m := re.search(r"Enseignant\(e\)\(s\)\s*:\s*([^\n]+)", text): e["enseignant"] = m[1].strip()
            if m := re.search(r"Classe de ([^\n]+)", text): e["classe"] = m[1].strip()
            if m := re.search(r"(?:Semestre|Trimestre) \d du (\d\d/\d\d/\d{4}) au (\d\d/\d\d/\d{4})", text): e["du"], e["au"] = m[1], m[2]
            head = max([w[1] for w in pw if w[4] in ("Dépassés", "Partiellement", "Domaines")] or [0]) + 12
            stop = min([y for y, ws in lines if " ".join(w[4] for w in ws).strip().startswith(
                ("Bilan de l", "Appréciation générale", "Parcours éducatifs", "Communication avec"))
                or re.match(r"^[A-ZÀ-Ü' -]{3,60}\s\d{1,2}/\d{1,2}$", " ".join(w[4] for w in ws).strip())] or [1e9])  # + pied de page « NOM PRÉNOM 12/30 »
            bullets = [w[0] for w in pw if w[4] == "-" and w[0] > 120 and head < w[1] < stop]
            ox = (min(bullets) - 2) if bullets else 175
            marks = sorted([w for w in pw if w[4] in ("X", "x") and w[0] > 495 and head < w[1] < stop], key=lambda w: w[1])
            marks += [w for w in pw if w[4] == "Non" and w[0] > 480 and head < w[1] < stop
                      and any(v[4].startswith("évalu") and abs(v[1] - w[1]) < 3 for v in pw)]
            marks.sort(key=lambda w: w[1])
            # blocs : lignes espacées de ~10 pt dans un bloc, ≥ 20 pt entre deux blocs ; un bloc = libellé (gauche) + objectifs + croix
            rows = sorted(_lines([w for w in pw if w[0] < 495 and head < w[1] < stop], ytol=3), key=lambda r: r[0])
            blocks = []
            for y, ws in rows:
                if not blocks or y - blocks[-1][-1][0] > 14: blocks.append([])
                blocks[-1].append((y, ws))
            for bl in blocks:
                y0, y1 = bl[0][0], bl[-1][0]
                dom = " ".join(" ".join(w[4] for w in ws if w[2] <= ox - 1) for _, ws in bl).strip()
                objs = []
                for _, ws in bl:
                    t = " ".join(w[4] for w in ws if w[0] >= ox - 1)
                    if not t: continue
                    if t.startswith("- ") or not objs: objs.append(t[2:] if t.startswith("- ") else t)
                    else: objs[-1] += " " + t
                mk = next((m for m in marks if y0 - 6 <= m[1] <= y1 + 6), None)
                if not objs and not mk:
                    if dom: disc = dom  # en-tête de discipline (« Français », « Mathématiques »…)
                    continue
                if objs and not dom and not mk and last:  # suite d'un bloc coupé par le saut de page
                    d, dm, niveau = last
                else:
                    if dom in tops or not disc: disc, d, dm = (dom or disc), (dom or disc), ""
                    else: d, dm = disc, dom
                    niveau = None if not mk or mk[4] == "Non" else lvl((mk[0] + mk[2]) / 2)
                last = (d, dm, niveau)
                for t in objs or [dom]:
                    e["objectifs"].append({"discipline": d or "?", "domaine": dm, "objectif": t.strip(), "niveau": niveau})
            # parcours et appréciation (après le tableau)
            for y, ws in lines:
                t = " ".join(w[4] for w in ws).strip()
                if t.startswith("Parcours éducatifs"): mode = "par"; continue
                if t.startswith("Appréciation générale"): mode = "app"; continue
                if t.startswith("Communication avec") or re.match(r"^[A-Z' -]{3,60} \d{1,2}/\d{1,2}$", t): mode = None; continue
                if mode == "par" and not t.startswith("Aucun parcours"): e["parcours"] = (e["parcours"] + " " + t).strip()
                if mode == "app": e["appreciation"] = (e["appreciation"] + " " + t).strip()
    return entries


def cmd_livret(args):
    """Livret scolaire complet (export PDF) → archives/<prénom>/ : ajoute seulement les périodes du primaire manquantes
    (bilan.json + pages extraites) ; les bilans déjà présents (collège : bulletins ; primaire : bilan.json) sont des doublons."""
    import shutil
    import subprocess
    import tempfile
    src = Path(args.pdf).expanduser()
    entries = parse_livret_lsu(src)
    added, dup = [], []
    for e in sorted(entries, key=lambda e: (e["annee"], e["n"])):
        an = e["annee"].replace("/", "-")
        if re.match(r"\dEME$", e["niveau"]):
            dup.append(f"{e['niveau']} {e['periode']} {e['n']} {e['annee']} (bulletins collège déjà archivés)"); continue
        ydir = ARCH / args.prenom / f"{an}_{e['niveau']}"
        bf = ydir / "bilan.json"
        if not bf.exists() and ydir.is_dir() and any(ydir.glob("*.pdf")):  # année déjà archivée autrement (livret scanné lu par parse_lsu)
            dup.append(f"{e['niveau']} {e['periode']} {e['n']} {e['annee']} (année déjà archivée : {ydir.relative_to(ROOT)})"); continue
        bilan = load_json(bf) or {"source": f"Extrait de {src.name} (livret scolaire), à corriger ici si besoin.", "annee": e["annee"],
                                  "classe": e["niveau"], "enseignant": e["enseignant"].title(), "etablissement": e["etablissement"], "semestres": []}
        bilan.setdefault("periode", e["periode"])
        if any(p["semestre"] == e["n"] for p in bilan["semestres"]):
            dup.append(f"{e['niveau']} {e['periode']} {e['n']} {e['annee']} (déjà dans {bf.relative_to(ROOT)})"); continue
        bilan["semestres"].append({"semestre": e["n"], "du": e.get("du", ""), "au": e.get("au", ""), "enseignant": e["enseignant"].title(),
                                   "objectifs": e["objectifs"], "commentaires": {}, "appreciation": e["appreciation"], "parcours": e["parcours"]})
        bilan["semestres"].sort(key=lambda p: p["semestre"])
        added.append(f"{e['niveau']} {e['periode']} {e['n']} {e['annee']} : {len(e['objectifs'])} objectifs, pages {e['pages'][0]}-{e['pages'][1]}")
        if args.dry_run:
            continue
        ydir.mkdir(parents=True, exist_ok=True)
        out = ydir / f"livret-{e['periode'].lower()}-{e['n']}.pdf"
        # poppler râle sur la table xref de l'export (avertissements, code retour ≠ 0) mais produit des pages valides : on vérifie le fichier
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdfseparate", "-f", str(e["pages"][0]), "-l", str(e["pages"][1]), str(src), f"{tmp}/p-%03d.pdf"], capture_output=True)
            parts = sorted(Path(tmp).glob("p-*.pdf"))
            if len(parts) == 1: shutil.copy(parts[0], out)
            elif parts: subprocess.run(["pdfunite", *map(str, parts), str(out)], capture_output=True)
        if not out.is_file() or out.stat().st_size == 0:
            sys.exit(f"extraction des pages {e['pages']} impossible ({out})")
        write_json(bf, bilan)  # en dernier : une reprise après erreur ne prend pas la période pour un doublon
    print("Ajouté" + (" (simulation)" if args.dry_run else "") + " :", *added or ["rien"], sep="\n  ", file=sys.stderr)
    print("Doublons ignorés :", *dup or ["aucun"], sep="\n  ", file=sys.stderr)


ARCH = ROOT / "archives"
REVS = OUT / "revisions"  # fiches de révision par contrôle (PDF + index.json écrits à la main)


def past_years(n=6):
    y = int(annee_scolaire()[:4])
    return [f"{y - i}-{y - i + 1}" for i in range(1, n + 1)]


def fetch_past_notes(ed, eid, prenom):
    """Notes des années précédentes (API, anneeScolaire=AAAA-AAAA) → out/<prénom>_notes_<année>.json.
    Une année passée ne change plus : téléchargée une seule fois. On s'arrête à la première année vide."""
    for an in past_years():
        f = OUT / f"{prenom}_notes_{an}.json"
        if not f.exists():
            try:
                write_json(f, ed.notes_annee(eid, an) or {})
            except EDError as err:
                print(f"  notes {an}: {err}", file=sys.stderr)
                break
        if not (load_json(f) or {}).get("notes"):
            break


def load_archives():
    """archives/<Prénom>/<AAAA-AAAA>_<classe>/ : bulletins collège (PDF EcoleDirecte), livret primaire (LSU scanné/OCR)
    ou bilan.json (transcription), + pour un enfant EcoleDirecte les notes détaillées de l'API (cache out/)."""
    out = []
    kids = sorted({p.name for p in ARCH.iterdir() if p.is_dir()} if ARCH.is_dir() else set()
                  | {f.name.split("_notes_")[0] for f in OUT.glob("*_notes_20*.json")})
    for prenom in kids:
        years = {}
        for ydir in sorted((ARCH / prenom).glob("20*_*")) if (ARCH / prenom).is_dir() else []:
            an, classe = ydir.name.split("_", 1)
            e = years.setdefault(an, {"annee": an, "classe": classe})
            pdfs = sorted(ydir.glob("*.pdf"))
            e["fichiers"] = [{"path": f.relative_to(ARCH).as_posix(), "name": f.name, "size": f.stat().st_size} for f in pdfs]
            if (ydir / "bilan.json").exists():
                e.update({"type": "primaire", **{k: v for k, v in load_json(ydir / "bilan.json").items() if k not in ("source", "annee", "classe")}})
                continue
            trims = [b for f in pdfs if (b := parse_bulletin_college(f))]
            if trims:
                e.update(type="college", trimestres=sorted(trims, key=lambda b: b["trimestre"]))
                continue
            for f in pdfs:
                if lsu := parse_lsu(f):
                    e.update(type="primaire", **{k: v for k, v in lsu.items() if k not in ("annee", "classe")}, classeLivret=lsu.get("classe"))
                    break
        for f in OUT.glob(f"{prenom}_notes_20*.json"):
            an = f.stem.rsplit("_", 1)[1]
            raw = load_json(f) or {}
            if raw.get("notes"):
                e = years.setdefault(an, {"annee": an, "classe": "", "type": "college"})
                n = norm_notes(raw)
                e["notes"], e["periodes"] = n["notes"], [p for p in n["periodes"] if not p["releve"]]
        if years:
            out.append({"prenom": prenom, "annees": [years[k] for k in sorted(years, reverse=True)]})
    return out


def _pgm(path):
    """PGM binaire (P5, 8 bits) → (largeur, hauteur, octets)."""
    raw = Path(path).read_bytes()
    m = re.match(rb"P5\s+(\d+)\s+(\d+)\s+255\s", raw)
    w, h = int(m.group(1)), int(m.group(2))
    return w, h, raw[m.end():m.end() + w * h]


def parse_menu_grid_pdf(path, ref):
    """Menu mensuel « en grille » (ex. restauration scolaire municipale) : blocs d'une semaine, colonnes « Lundi 31/08 »,
    bandes entrée | plat + garniture | fromage | dessert séparées par des filets. Les filets sont dessinés dans des
    images : on les repère sur un rendu en niveaux de gris à 72 dpi (1 px = 1 pt, même repère que pdftotext)."""
    import subprocess
    import tempfile
    try:
        with tempfile.TemporaryDirectory() as tmp:
            subprocess.run(["pdftotext", "-bbox", str(path), f"{tmp}/w.html"], check=True, capture_output=True, timeout=60)
            subprocess.run(["pdftoppm", "-r", "72", "-gray", str(path), f"{tmp}/p"], check=True, capture_output=True, timeout=120)
            pages_html = Path(f"{tmp}/w.html").read_text().split("<page ")[1:]
            images = [_pgm(p) for p in sorted(Path(tmp).glob("p-*.pgm"))]
    except (OSError, subprocess.SubprocessError, AttributeError):
        return {}
    out = {}
    for page_html, (W, H, px) in zip(pages_html, images):
        words = [(float(a), float(b), float(c), float(d), html.unescape(t)) for a, b, c, d, t in
                 re.findall(r'<word xMin="([\d.]+)" yMin="([\d.]+)" xMax="([\d.]+)" yMax="([\d.]+)">([^<]*)</word>', page_html)]
        # En-têtes de colonnes : « Lundi » suivi de « 31/08 » sur la même ligne.
        heads = []
        for i, (x0, y0, x1, y1, t) in enumerate(words):
            if fold(t) in ("lundi", "mardi", "mercredi", "jeudi", "vendredi") and i + 1 < len(words):
                nx0, ny0, nx1, ny1, nt = words[i + 1]
                m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", nt)
                if m and abs(ny0 - y0) < 4:
                    heads.append({"x0": x0, "x1": nx1, "y0": y0, "y1": max(y1, ny1), "d": int(m.group(1)), "m": int(m.group(2))})
        for h in heads:
            h["veg"] = any("vegetarien" in fold(t) for x0, y0, x1, y1, t in words if abs(y0 - h["y0"]) < 4 and h["x1"] < x0 < h["x1"] + 70)
        # Blocs = en-têtes de la même rangée et de la même semaine (l'écart entre blocs ≈ l'écart entre colonnes).
        for h in heads:
            year = min((ref.year - 1, ref.year, ref.year + 1), key=lambda y: abs((date(y, h["m"], h["d"]) - ref).days))
            h["date"] = date(year, h["m"], h["d"])
        blocks = {}
        for h in sorted(heads, key=lambda h: h["x0"]):
            blocks.setdefault((round(h["y0"] / 10), h["date"] - timedelta(days=h["date"].weekday())), []).append(h)
        blocks = list(blocks.values())
        for b in blocks:
            xa, xb = int(b[0]["x0"]), int(b[-1]["x1"])
            lines, y, run = [], int(b[0]["y1"]) - 2, 0
            while y < H:
                row = px[y * W + xa:y * W + xb]
                dark = sum(1 for v in row if v < 225) / max(1, len(row)) > 0.9  # filet : ligne quasi pleine (le texte gras monte à ~70 %)
                if dark:
                    run += 1
                elif run:
                    start = y - run
                    if run <= 3:
                        lines.append(start + run / 2)
                    elif len(lines) >= 2:  # fin du tableau (fond de page)
                        lines.append(start)
                        break
                    run = 0
                y += 1
            else:
                if run > 3 and len(lines) >= 2:  # bloc collé au bas de la page
                    lines.append(H - run)
            if len(lines) < 4:
                continue
            bands = list(zip(lines, lines[1:]))
            keys = ["entrees", "plats", "laitages", "desserts"] if len(bands) == 4 else MENU_BANDS[:len(bands)]
            # colonnes : milieu entre en-têtes voisins
            cuts = [(b[i]["x1"] + b[i + 1]["x0"]) / 2 for i in range(len(b) - 1)]
            for ci, h in enumerate(b):
                left = cuts[ci - 1] if ci else xa - 20
                right = cuts[ci] if ci < len(cuts) else xb + 20
                menu = {}
                for (top, bottom), key in zip(bands, keys):
                    ws = sorted((w for w in words if left <= (w[0] + w[2]) / 2 < right and top < (w[1] + w[3]) / 2 < bottom),
                                key=lambda w: (round(w[1]), w[0]))
                    # lignes de texte, puis plats : un écart vertical > 1,6 interligne sépare deux plats
                    rows = []
                    for w in ws:
                        if rows and abs(rows[-1][0] - w[1]) < 3:
                            rows[-1][2].append(w[4])
                        else:
                            rows.append([w[1], w[3] - w[1], [w[4]]])
                    dishes = []
                    for i, (y0, lh, ts) in enumerate(rows):
                        if i and y0 - rows[i - 1][0] <= 1.6 * max(lh, rows[i - 1][1]):
                            dishes[-1] += " " + " ".join(ts)
                        else:
                            dishes.append(" ".join(ts))
                    dishes = [re.sub(r"\s+", " ", x).strip(" /") for x in dishes if x.strip(" /")]
                    if key == "plats" and len(keys) == 4 and len(dishes) > 1:
                        menu["plats"], menu["accompagnements"] = dishes[:1], dishes[1:]
                    elif dishes:
                        menu[key] = dishes
                if not menu:
                    continue
                out[h["date"].isoformat()] = {**menu, **({"vegetarien": True} if h["veg"] else {})}
    return out


def file_ref_date(path):
    """Date de référence d'un fichier importé : année (et mois) lus dans le nom, sinon date de modification."""
    name = fold(Path(path).stem)
    y = re.search(r"20\d\d", name)
    mo = next((i + 1 for i, m in enumerate(MOIS) if m in name), None)
    if y:
        return date(int(y.group()), mo or 1, 15)
    return date.fromtimestamp(Path(path).stat().st_mtime)


def parse_date_fr(s, ref):
    s = s.strip()
    for fmt in ("%Y-%m-%d", "%d/%m/%Y", "%d/%m/%y"):
        try:
            return datetime.strptime(s, fmt).date()
        except ValueError:
            pass
    m = re.fullmatch(r"(\d{1,2})/(\d{1,2})", s)
    if m:
        d, mo = int(m.group(1)), int(m.group(2))
        return min((date(y, mo, d) for y in (ref.year - 1, ref.year, ref.year + 1)), key=lambda x: abs((x - ref).days))
    raise ValueError(f"date illisible : {s!r}")


def _rows(path):
    """CSV (séparateur ; ou ,) ou JSON (liste d'objets) → liste de dicts aux clés normalisées (minuscules, sans accents)."""
    import csv
    if path.suffix.lower() == ".json":
        data = json.loads(path.read_text())
        return [{fold(k).strip(): v for k, v in r.items()} for r in (data if isinstance(data, list) else [])]
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.split("\n", 1)[0], delimiters=";,\t")
    return [{fold(k or "").strip(): (v or "").strip() for k, v in r.items()} for r in csv.DictReader(text.splitlines(), dialect=dialect)]


YES = {"oui", "o", "x", "1", "true", "vrai", "yes"}


def data_url(path):
    mime = {".png": "image/png", ".webp": "image/webp"}.get(path.suffix.lower(), "image/jpeg")
    return f"data:{mime};base64," + base64.b64encode(path.read_bytes()).decode()


def load_espaces():
    """espaces.json : [{prenom, source: "ecoledirecte"}, {prenom, source: "dossier", dossier, …}].
    Dossier : *menu*.pdf|csv|json → cantine ; *devoir*.csv|json → devoirs. Fichiers commençant par « _ » ignorés."""
    conf = load_json(ROOT / "espaces.json")
    if not conf:
        return None
    out = []
    for e in conf:
        e = dict(e)
        if e.get("source") != "dossier":
            out.append(e)
            continue
        folder = ROOT / e.get("dossier", "sources")
        cantine, devoirs, imports = {}, [], []
        for f in sorted(folder.glob("*")) if folder.is_dir() else []:
            name, ext = fold(f.name), f.suffix.lower()
            if f.name.startswith(("_", ".")) or ext not in (".pdf", ".csv", ".json"):
                continue
            ref = file_ref_date(f)
            try:
                if "menu" in name and ext == ".pdf":
                    menus = parse_menu_grid_pdf(f, ref) or parse_menu_pdf(f, ref.isoformat())
                    cantine.update(menus)
                    imports.append({"fichier": f.name, "type": "cantine", "n": len(menus),
                                    "du": min(menus, default=None), "au": max(menus, default=None)})
                elif "menu" in name:
                    n = 0
                    for r in _rows(f):
                        d = parse_date_fr(str(r.get("date", "")), ref).isoformat()
                        menu = {k: [x.strip() for x in re.split(r"\s*/\s*|\s*\|\s*", str(r.get(k, ""))) if x.strip()] for k in MENU_BANDS}
                        menu = {k: v for k, v in menu.items() if v}
                        if r.get("note"):
                            menu["note"] = str(r["note"])
                        if str(r.get("vegetarien", "")).lower() in YES:
                            menu["vegetarien"] = True
                        cantine[d] = menu
                        n += 1
                    imports.append({"fichier": f.name, "type": "cantine", "n": n})
                elif "devoir" in name:
                    n = 0
                    for r in _rows(f):
                        if not r.get("date"):
                            continue
                        devoirs.append({"date": parse_date_fr(str(r["date"]), ref).isoformat(),
                                        "matiere": r.get("matiere", ""), "contenu": r.get("travail") or r.get("contenu", ""),
                                        "interrogation": str(r.get("controle", "")).lower() in YES,
                                        "effectue": str(r.get("fait", "")).lower() in YES})
                        n += 1
                    imports.append({"fichier": f.name, "type": "devoirs", "n": n})
            except (ValueError, KeyError, json.JSONDecodeError) as err:
                imports.append({"fichier": f.name, "erreur": str(err)})
                print(f"  import {f.name} : {err}", file=sys.stderr)
        # doublons (même date, matière, travail) : un fichier mensuel peut en recouvrir un autre
        seen = set()
        devoirs = [d for d in sorted(devoirs, key=lambda d: d["date"])
                   if (k := (d["date"], d["matiere"], d["contenu"])) not in seen and not seen.add(k)]
        # copines du jeu Squishy : photo de visage (option « vrais visages ») intégrée en data URL, fichier local non versionné
        if e.get("copines"):
            e["copines"] = [{**c, "visage": data_url(folder / c["visage"])} if c.get("visage") and (folder / c["visage"]).is_file()
                            else {k: v for k, v in c.items() if k != "visage"} for c in e["copines"]]
        out.append({**e, "cantine": dict(sorted(cantine.items())), "devoirs": devoirs, "imports": imports})
    return out


def load_famille():
    raw = {k: load_json(FAM / f"{k}.json") for k in ("messages_received", "messages_sent", "documents", "factures")}
    raw["messages_detail"] = load_json(FAM / "messages_detail.json", {})
    return norm_famille(raw, load_json(FILES / "index.json", {})) if FAM.exists() else None


def build_child(ident, raw):
    return {
        **ident,
        "notes": norm_notes(raw.get("notes")),
        "edt": norm_edt(raw.get("edt")),
        "devoirs": norm_devoirs(raw.get("devoirs")),
        "vie": norm_vie(raw.get("vie_scolaire")),
        "erreurs": [k for k in ("notes", "edt", "devoirs", "vie_scolaire") if raw.get(k) is None],
    }


def write_dashboard(children, semaine, path=None, **extra):
    path = path or OUT / "dashboard.json"
    extra.setdefault("evenements", load_json(ROOT / "evenements.json", []))  # stage, examens… (saisis à la main)
    extra.setdefault("garde", load_json(ROOT / "garde.json"))  # garde partagée papa / maman (saisie à la main)
    extra.setdefault("cantine", cantine_menus())  # menus extraits des PDF « Menu … » (documents, PJ)
    extra.setdefault("espaces", load_espaces())
    extra.setdefault("archives", load_archives())
    extra.setdefault("revisions", load_json(REVS / "index.json", []))
    extra.setdefault("enseignants", {k: v for k, v in (load_json(ROOT / "enseignants.json") or {}).items() if not k.startswith("_")})  # bulletins/livrets des années passées (archives/) + notes API passées  # espaces enfants hors EcoleDirecte (espaces.json + dossier d'import)
    for c in children:  # dernier fetch de l'enfant = dump brut le plus récent (normalize ne le change pas)
        dumps = [OUT / f"{c.get('prenom')}_{k}.json" for k in ("notes", "edt", "devoirs", "vie_scolaire")]
        t = max((f.stat().st_mtime for f in dumps if f.exists()), default=None)
        if t:
            c["fetched_at"] = datetime.fromtimestamp(t).isoformat(timespec="seconds")
    write_json(path, {"generated_at": datetime.now().isoformat(timespec="seconds"),
                      "semaine": semaine, **extra, "children": children})
    print(f"OK → {path}", file=sys.stderr)


def cmd_normalize(_args):
    """Reconstruit dashboard.json depuis les dumps bruts, sans appel réseau."""
    known = {}
    try:
        known = {c["prenom"]: c for c in json.loads((OUT / "dashboard.json").read_text()).get("children", [])}
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    prenoms = sorted({f.name.rsplit("_", 1)[0] for f in OUT.glob("*_*.json")
                      if f.name.rsplit("_", 1)[1].removesuffix(".json") in ("notes", "edt", "devoirs", "eleve")}
                     | {f.name.removesuffix("_vie_scolaire.json") for f in OUT.glob("*_vie_scolaire.json")})
    if not prenoms:
        sys.exit("aucun dump dans out/ : lancer fetch d'abord")
    children = []
    for prenom in prenoms:
        raw = {}
        for kind in ("notes", "edt", "devoirs", "vie_scolaire"):
            f = OUT / f"{prenom}_{kind}.json"
            raw[kind] = json.loads(f.read_text()) if f.exists() else None
        f = OUT / f"{prenom}_eleve.json"
        prev = known.get(prenom, {})
        ident = json.loads(f.read_text()) if f.exists() else {
            "id": prev.get("id"), "prenom": prenom, "classe": prev.get("classe", ""),
            "etablissement": prev.get("etablissement", "")}
        children.append(build_child(ident, raw))
    d1, d2 = week_bounds()
    write_dashboard(children, [d1.isoformat(), d2.isoformat()], famille=load_famille(),
                    calendrier=load_json(OUT / "calendrier.json"))


def cmd_demo(_args):
    rnd = random.Random(42)
    today = date.today()
    d1, d2 = week_bounds()
    matieres = [("FRANCAIS", "FRANC", "#e0592a"), ("MATHEMATIQUES", "MATHS", "#2a78d6"),
                ("HISTOIRE-GEOGRAPHIE", "HI-GE", "#8b6d3b"), ("ANGLAIS LV1", "AGL1", "#1baf7a"),
                ("SCIENCES VIE & TERRE", "SVT", "#008300"), ("PHYSIQUE-CHIMIE", "PH-CH", "#4a3aa7"),
                ("ED.PHYSIQUE & SPORT.", "EPS", "#e87ba4"), ("ESPAGNOL LV2", "ESP2", "#eda100"),
                ("ARTS PLASTIQUES", "A-PLA", "#c2185b"), ("EDUCATION MUSICALE", "EDMUS", "#7b1fa2")]
    profs = ["M. MARTIN P.", "Mme DURAND C.", "M. PETIT J.", "Mme LEROY A.", "Mme MOREAU S."]
    rooms = ["B12", "C04", "Labo 2", "Gymnase", "A21", "CDI", "Salle Arts"]
    start_year = date(today.year if today.month >= 9 else today.year - 1, 9, 2)
    niveaux = [{"niveau": 1, "libelle": "Maîtrise insuffisante", "couleur": "#ff0000"},
               {"niveau": 2, "libelle": "Maîtrise fragile", "couleur": "#ffc000"},
               {"niveau": 3, "libelle": "Maîtrise satisfaisante", "couleur": "#0070c0"},
               {"niveau": 4, "libelle": "Très bonne maîtrise", "couleur": "#00b050"}]
    competences = ["Lire et comprendre un document", "Raisonner", "S'exprimer à l'oral",
                   "Pratiquer des démarches scientifiques", "Communiquer", "Modéliser"]

    def child(prenom, classe, level):
        bounds = [(start_year, start_year + timedelta(days=95)),
                  (start_year + timedelta(days=96), start_year + timedelta(days=190)),
                  (start_year + timedelta(days=191), start_year + timedelta(days=305))]
        notes, nid = [], 1
        span = max((today - start_year).days, 20)
        per_subject = {}
        for m, code, _ in matieres:
            base = level + rnd.uniform(-3, 3)
            for _ in range(rnd.randint(2, 4)):
                dt = start_year + timedelta(days=rnd.randint(3, span))
                sur = rnd.choice([20, 20, 20, 10])
                v = max(0, min(20, rnd.gauss(base, 2.5)))
                avg = rnd.uniform(9, 14)
                notes.append({
                    "id": nid, "date": dt.isoformat(), "saisie": (dt + timedelta(days=rnd.randint(0, 10))).isoformat(), "matiere": m, "codeMatiere": code,
                    "devoir": rnd.choice(["Contrôle chapitre 1", "Interro de vocabulaire", "DM n°2",
                                          "Évaluation commune", "Exposé", "TP noté"]),
                    "type": rnd.choice(["DS", "Ecrit", "ORAL", "Interrogation Ecrite", "Travaux Pratiques"]),
                    "periode": "A001", "valeur": round(v * sur / 20 * 2) / 2, "brut": None, "sur": sur,
                    "coef": rnd.choice([1, 1, 0.5, 2]), "nonSignificatif": False,
                    "moyenneClasse": round(avg * sur / 20, 2),
                    "minClasse": round(max(0, avg - rnd.uniform(5, 8)) * sur / 20, 1),
                    "maxClasse": round(min(20, avg + rnd.uniform(4, 7)) * sur / 20, 1),
                    "commentaire": "",
                    "competences": [{"libelle": c, "descriptif": "", "niveau": rnd.choice([2, 3, 3, 4, 4])}
                                    for c in rnd.sample(competences, rnd.choice([0, 0, 1, 2]))],
                })
                per_subject.setdefault(m, []).append(v)
                nid += 1
        disciplines = [{"matiere": m, "code": code, "groupe": False,
                        "moyenne": round(sum(per_subject[m]) / len(per_subject[m]), 2),
                        "moyenneClasse": round(rnd.uniform(10, 13.5), 2), "min": round(rnd.uniform(4, 8), 2),
                        "max": round(rnd.uniform(16, 19.5), 2), "coef": 1, "rang": None, "effectif": 27,
                        "profs": [rnd.choice(profs)]} for m, code, _ in matieres if m in per_subject]
        moy = round(sum(d["moyenne"] for d in disciplines) / len(disciplines), 2)
        periodes = [{"code": f"A00{i + 1}", "libelle": f"{i + 1}{'er' if i == 0 else 'ème'} Trimestre",
                     "debut": a.isoformat(), "fin": b.isoformat(), "annuel": False, "releve": False,
                     "cloture": False, "conseil": (b + timedelta(days=3)).isoformat(), "heureConseil": "17:30",
                     "pp": "Mme DURAND C.", "moyenne": moy if i == 0 else None,
                     "moyenneClasse": 11.8 if i == 0 else None, "min": 6.4 if i == 0 else None,
                     "max": 17.9 if i == 0 else None, "disciplines": disciplines if i == 0 else []}
                    for i, (a, b) in enumerate(bounds)]
        edt = []
        for day in range(14):
            d = d1 + timedelta(days=day)
            if d.weekday() >= 5:
                continue
            slots = [8, 9, 10, 11, 13, 14, 15, 16] if d.weekday() != 2 else [8, 9, 10, 11]
            prev = None
            for h in slots:
                if rnd.random() < 0.12:
                    prev = None
                    continue
                # cours doubles de temps en temps
                m, code, col = prev if prev and rnd.random() < 0.35 else rnd.choice(matieres)
                room = rnd.choice(rooms)
                edt.append({"start": f"{d} {h:02d}:00", "end": f"{d} {h:02d}:55", "matiere": m, "code": code,
                            "prof": profs[len(m) % len(profs)], "salle": room, "groupe": classe,
                            "couleur": col, "type": "COURS", "annule": rnd.random() < 0.04,
                            "modifie": rnd.random() < 0.05, "dispense": False, "devoir": rnd.random() < 0.2,
                            "seance": rnd.random() < 0.3})
                prev = (m, code, col)
        devoirs = []
        for _ in range(9):
            dd = today + timedelta(days=rnd.randint(1, 12))
            if dd.weekday() >= 5:
                dd += timedelta(days=7 - dd.weekday())
            m, code, _ = rnd.choice(matieres)
            devoirs.append({"date": dd.isoformat(), "matiere": m, "code": code,
                            "donneLe": (dd - timedelta(days=rnd.randint(2, 7))).isoformat(),
                            "effectue": rnd.random() < 0.25, "interrogation": rnd.random() < 0.25,
                            "rendreEnLigne": False, "prof": rnd.choice(profs),
                            "contenu": rnd.choice([
                                "Exercices 12 à 15 p. 84.", "Apprendre la leçon sur les fractions.",
                                "Lire le chapitre 3 et répondre aux questions 1 à 5.",
                                "Préparer l'exposé (5 min).\n• Plan\n• Sources\n• 3 images"]),
                            "seance": rnd.choice(["", "Correction de l'exercice 4. Début du chapitre 2."]),
                            "documents": rnd.choice([[], [], ["fiche-exercices.pdf"]])})
        vie = [{"type": "Absence", "date": (today - timedelta(days=12)).isoformat(),
                "detail": "de 08:00 à 12:00", "libelle": "1 demi-journée", "motif": "Médical", "justifie": True},
               {"type": "Retard", "date": (today - timedelta(days=4)).isoformat(),
                "detail": "à 08:10", "libelle": "00:10", "motif": "", "justifie": False}]
        return {"id": rnd.randint(1000, 9999), "prenom": prenom, "classe": classe, "etablissement": "Collège Démo",
                "notes": {"periodes": periodes, "notes": sorted(notes, key=lambda n: n["date"]), "niveaux": niveaux},
                "edt": edt, "devoirs": sorted(devoirs, key=lambda d: d["date"]), "vie": vie, "erreurs": []}

    def msg(i, days, frm, role, subject, content, read=True, pj=(), box="received", to=("Moi",)):
        return {"id": i, "box": box, "date": (datetime.now() - timedelta(days=days, hours=i)).strftime("%Y-%m-%d %H:%M:%S"),
                "subject": subject, "from": frm, "fromRole": role, "to": list(to), "read": read, "answered": False,
                "content": content, "attachments": [{"id": 100 + i, "name": n, "file": None} for n in pj]}
    famille = {
        "messages": [
            msg(1, 0, "Mme DURAND C.", "Professeur", "Sortie au musée des Confluences",
                "Bonjour,\n\nLa sortie aura lieu jeudi prochain. Merci de signer l'autorisation jointe.\n\nBien cordialement.",
                read=False, pj=["autorisation-sortie.pdf"]),
            msg(2, 1, "Vie scolaire", "Administration", "Photos de classe", "Les photos de classe sont disponibles en ligne : https://exemple.org/photos"),
            msg(3, 3, "M. MARTIN P.", "Professeur", "Réunion parents-professeurs",
                "Madame, Monsieur,\nLa réunion parents-professeurs aura lieu le 12 novembre à partir de 17 h.", pj=["planning.pdf"]),
            msg(4, 8, "Secrétariat", "Administration", "Certificat de scolarité", "Veuillez trouver ci-joint le certificat demandé.",
                pj=["certificat.pdf"]),
            msg(5, 2, "Moi", "", "Absence de Léa vendredi", "Bonjour, Léa sera absente vendredi matin pour un rendez-vous médical.",
                box="sent", to=("Vie scolaire",), pj=["justificatif-medical.pdf"]),
        ],
        "messagesStats": {"recus": 4, "nonLus": 1, "envoyes": 1},
        "documents": [
            {"cat": "administratifs", "catLabel": "Administratif", "id": 1, "libelle": "Certificat de scolarité 2026-2027",
             "date": (today - timedelta(days=20)).isoformat(), "type": "", "signatureDemandee": False, "signatures": [], "file": None},
            {"cat": "notes", "catLabel": "Bulletins & compétences", "id": 2, "libelle": "Bulletin du 3e trimestre 2025-2026",
             "date": (today - timedelta(days=90)).isoformat(), "type": "Note", "signatureDemandee": False, "signatures": [], "file": None},
            {"cat": "inscriptions", "catLabel": "Inscriptions & signatures", "id": 3, "libelle": "Autorisation de droit à l'image",
             "date": (today - timedelta(days=15)).isoformat(), "type": "INSCR_DOC_A_SIGNER", "signatureDemandee": True, "signatures": [], "file": None},
        ],
        "factures": [{"id": 9, "libelle": "Facture scolarité · 1er trimestre", "date": (today - timedelta(days=10)).isoformat(),
                      "type": "Facture", "montant": 412.5, "file": None}],
        "pieces": {"listes": [], "pieces": [], "televersements": [], "personnes": {}},
        "erreurs": [],
    }
    write_dashboard([child("Léa", "4EME B", 14.5), child("Tom", "6EME A", 12)],
                    [d1.isoformat(), d2.isoformat()], path=OUT / "demo.json", demo=True, famille=famille, cantine={}, archives=[], espaces=None,
                    calendrier=load_json(OUT / "calendrier.json"))


def lan_ip():
    """IP du Mac sur le Wi-Fi/Ethernet. Un VPN « full tunnel » (utun) capte la route par défaut :
    on interroge donc d'abord les interfaces physiques plutôt que la table de routage."""
    import socket
    import subprocess
    for iface in ("en0", "en1", "en2"):
        try:
            ip = subprocess.run(["ipconfig", "getifaddr", iface], capture_output=True,
                                text=True, timeout=2).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            break
        if ip and not ip.startswith("169.254."):
            return ip
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        try:
            sock.connect(("192.0.2.1", 9))  # aucune donnée envoyée : sert juste à choisir l'interface
            return sock.getsockname()[0]
        except OSError:
            return "127.0.0.1"


def dash_token():
    """Clé d'accès du dashboard en réseau local : ED_DASH_TOKEN (.env.local), générée au besoin."""
    tok = os.environ.get("ED_DASH_TOKEN")
    if tok:
        return tok
    import secrets
    tok = secrets.token_urlsafe(18)
    env = ROOT / ".env.local"
    with open(env, "a") as f:
        f.write(f"\n# Clé d'accès au dashboard depuis le réseau local (serve --lan)\nED_DASH_TOKEN={tok}\n")
    os.chmod(env, 0o600)
    os.environ["ED_DASH_TOKEN"] = tok
    return tok


CONNECT_PAGE = """<!doctype html><html lang="fr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>Installer Good Morning</title><link rel="icon" href="/icon.svg">
<link href="https://fonts.googleapis.com/css2?family=Bricolage+Grotesque:opsz,wght@12..96,700..800&family=Plus+Jakarta+Sans:wght@400..700&display=swap" rel="stylesheet">
<style>
:root{--ink:#1e3932;--ink2:#5b6b63;--card:rgba(255,255,255,.82);--bg:#f2f0eb}
@media (prefers-color-scheme:dark){:root{--ink:#f2f0eb;--ink2:#c9d3cd;--card:rgba(20,40,34,.72);--bg:#0f1f1a}}
*{box-sizing:border-box}
body{margin:0;min-height:100vh;display:grid;place-items:center;padding:24px 16px;color:var(--ink);font:15px/1.55 "Plus Jakarta Sans",system-ui,-apple-system,sans-serif;
 background:var(--bg);background-image:radial-gradient(40vmax 40vmax at 8% 6%,#d4e9e2,transparent 70%),radial-gradient(38vmax 38vmax at 96% 12%,#efe3cf,transparent 70%),radial-gradient(44vmax 44vmax at 70% 104%,#e3c29b,transparent 70%)}
@media (prefers-color-scheme:dark){body{background-image:radial-gradient(40vmax 40vmax at 8% 6%,#0b4a34,transparent 70%),radial-gradient(38vmax 38vmax at 96% 12%,#1e3932,transparent 70%),radial-gradient(44vmax 44vmax at 70% 104%,#4a3020,transparent 70%)}}
main{width:min(560px,100%);background:var(--card);backdrop-filter:blur(18px);-webkit-backdrop-filter:blur(18px);border:1px solid rgba(255,255,255,.6);border-radius:10px;
 box-shadow:0 24px 60px -24px rgba(30,57,50,.45);padding:28px 26px;text-align:center}
.app{display:flex;align-items:center;justify-content:center;gap:14px;margin-bottom:6px}
.app img{width:72px;height:72px;border-radius:16px;box-shadow:0 12px 26px -10px rgba(0,98,63,.7)}
h1{margin:0;font:800 40px/1.05 "Bricolage Grotesque",system-ui,sans-serif;letter-spacing:-.01em;background:linear-gradient(120deg,#00704a,#3d8a6b 40%,#b3835a 75%,#6f4e37);-webkit-background-clip:text;background-clip:text;color:transparent}
.tag{color:var(--ink2);margin:2px 0 18px}
#qr{background:#fff;display:inline-block;padding:14px;border-radius:10px;box-shadow:0 10px 30px -14px rgba(42,26,46,.5);position:relative}
#qr img{display:block;width:250px;height:250px;image-rendering:pixelated}
#qr .center{position:absolute;inset:0;margin:auto;width:56px;height:56px;border-radius:12px;border:4px solid #fff;background:#fff}
ol{text-align:left;margin:20px auto 6px;padding:0;list-style:none;max-width:400px;display:grid;gap:10px;counter-reset:s}
li{display:grid;grid-template-columns:30px 1fr;gap:10px;align-items:start;counter-increment:s}
li::before{content:counter(s);width:30px;height:30px;border-radius:50%;display:grid;place-items:center;font:800 14px "Bricolage Grotesque",sans-serif;color:#fff;background:linear-gradient(135deg,#00704a,#3d8a6b)}
b{font-weight:700}
.url{margin-top:16px;display:flex;gap:8px;align-items:center;justify-content:center;flex-wrap:wrap}
code{word-break:break-all;font-size:12.5px;background:rgba(127,127,127,.14);padding:6px 10px;border-radius:10px}
button{border:0;border-radius:999px;padding:7px 14px;font:700 13px "Plus Jakarta Sans",sans-serif;cursor:pointer;color:#fff;background:linear-gradient(120deg,#00704a,#6f4e37)}
.warn{font-size:12.5px;color:var(--ink2);margin:14px 0 0}
</style></head>
<body><main>
<div class="app"><img src="/icon.svg" alt=""><h1>Good Morning</h1></div>
<p class="tag">__KIDS__ · à installer sur l'iPad</p>
<div id="qr"></div>
<ol>
 <li><span>Sur l'iPad (même Wi-Fi que ce Mac), ouvre l'<b>appareil photo</b> et vise le QR code.</span></li>
 <li><span>Touche la bannière pour ouvrir Good Morning dans <b>Safari</b>.</span></li>
 <li><span><b>Partager</b> → <b>Sur l'écran d'accueil</b> → <b>Ajouter</b> : l'icône ☕ Good Morning apparaît, l'appli s'ouvre en plein écran.</span></li>
</ol>
<div class="url"><code id="u"></code><button type="button" id="copy">Copier</button></div>
<p class="warn">🔐 Ce lien contient la clé d'accès de la maison : à garder dans la famille. L'espace Papa reste protégé par son propre mot de passe.</p>
</main>
<script src="/vendor/qrcode.js"></script>
<script>const u = __URL__; document.getElementById("u").textContent = u;
document.getElementById("copy").onclick = e => navigator.clipboard?.writeText(u).then(() => e.target.textContent = "Copié ✓");
try { const q = qrcode(0, "H"); q.addData(u); q.make(); document.getElementById("qr").innerHTML = q.createImgTag(7, 0) + '<img class="center" src="/icon.svg" alt="">'; }
catch (e) { document.getElementById("qr").textContent = "QR indisponible : saisis l'adresse ci-dessous."; }</script></body></html>"""


def cmd_serve(args):
    import hmac
    from http.cookies import SimpleCookie
    from urllib.parse import parse_qs, urlsplit

    routes = {"/": (ROOT / "dashboard" / "index.html", "text/html; charset=utf-8"),
              "/data.json": (OUT / ("demo.json" if args.demo else "dashboard.json"),
                             "application/json; charset=utf-8")}
    # Fichiers PWA (manifest, icônes) : servis sans clé, iOS les récupère hors session.
    public = {"/manifest.json": (ROOT / "dashboard" / "manifest.json", "application/manifest+json"),
              **{f"/icon-{n}.png": (ROOT / "dashboard" / f"icon-{n}.png", "image/png") for n in (180, 192, 512)},
              "/vendor/dicebear.js": (ROOT / "dashboard" / "vendor" / "dicebear.js", "text/javascript; charset=utf-8"),
              "/vendor/qrcode.js": (ROOT / "dashboard" / "vendor" / "qrcode.js", "text/javascript; charset=utf-8"),
              "/icon.svg": (ROOT / "dashboard" / "icon.svg", "image/svg+xml")}
    token = dash_token() if args.lan else None
    # Espace « Papa » (messagerie, documents, factures, fichiers) : mot de passe ED_PAPA_PASS (.env.local).
    # Données retirées de data.json et servies par /papa.json seulement avec le cookie de session « edp »,
    # dérivé du mot de passe (survit aux redémarrages, invalidé si le mot de passe change). Exigé même en local.
    papa_pass = os.environ.get("ED_PAPA_PASS") or ""
    papa_token = hmac.new(papa_pass.encode(), b"espace-papa-v1", "sha256").hexdigest() if papa_pass else None
    PAPA_TTL = 8 * 3600
    fails = {"n": 0}
    import subprocess
    import threading
    refreshing = threading.Lock()  # un seul « Mettre à jour » à la fois
    host = "0.0.0.0" if args.lan else "127.0.0.1"
    started = datetime.now().isoformat(timespec="seconds")
    lan_url = f"http://{lan_ip()}:{args.port}/?k={token}" if args.lan else None

    class H(BaseHTTPRequestHandler):
        def _local(self):
            return self.client_address[0] in ("127.0.0.1", "::1")

        def _authorized(self, query):
            """Hors localhost : clé dans l'URL (?k=, première visite) ou cookie posé ensuite."""
            if not token or self._local():
                return True, False
            cookie = SimpleCookie(self.headers.get("Cookie", ""))
            got = cookie["edk"].value if "edk" in cookie else ""
            if got and hmac.compare_digest(got, token):
                return True, False
            k = (query.get("k") or [""])[0]
            return (bool(k) and hmac.compare_digest(k, token)), True

        def _papa(self):
            if args.demo:
                return True  # données factices
            got = SimpleCookie(self.headers.get("Cookie", "")).get("edp")
            return bool(papa_token and got and hmac.compare_digest(got.value, papa_token))

        def _json(self, code, obj, extra=()):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), "application/json; charset=utf-8", extra)

        def do_POST(self):
            url = urlsplit(self.path).path
            ok, _ = self._authorized({})
            if not ok:
                return self._json(401, {"erreur": "clé d'accès manquante"})
            if url == "/refresh":
                # bouton « Mettre à jour » : fetch (EcoleDirecte) ou normalize (espaces « dossier »), dans un sous-processus
                n = int(self.headers.get("Content-Length") or 0)
                try:
                    mode = str(json.loads(self.rfile.read(min(n, 1024)) or b"{}").get("mode", ""))
                except (ValueError, AttributeError):
                    mode = ""
                if args.demo or mode not in ("fetch", "normalize"):
                    return self._json(400, {"erreur": "mise à jour indisponible"})
                if not refreshing.acquire(blocking=False):
                    return self._json(409, {"erreur": "mise à jour déjà en cours"})
                try:
                    # stdin fermé : si EcoleDirecte demande le QCM de double authentification, fetch s'arrête
                    r = subprocess.run([sys.executable, str(Path(__file__).resolve()), mode], cwd=ROOT, stdin=subprocess.DEVNULL,
                                       capture_output=True, text=True, timeout=300)
                    out = (r.stderr + r.stdout).strip().splitlines()[-6:]
                    return self._json(200 if r.returncode == 0 else 502, {"ok": r.returncode == 0, "sortie": out})
                except subprocess.TimeoutExpired:
                    return self._json(504, {"erreur": "délai dépassé (5 min)"})
                finally:
                    refreshing.release()
            if url == "/papa/logout":
                return self._json(200, {"ok": True}, [("Set-Cookie", "edp=; Max-Age=0; Path=/; HttpOnly; SameSite=Strict")])
            if url != "/papa/login":
                return self.send_error(404)
            if not papa_pass:
                return self._json(503, {"erreur": "ED_PAPA_PASS non défini dans .env.local"})
            n = int(self.headers.get("Content-Length") or 0)
            try:
                pw = str(json.loads(self.rfile.read(min(n, 4096)) or b"{}").get("password", ""))
            except (ValueError, AttributeError):
                pw = ""
            if hmac.compare_digest(pw.encode(), papa_pass.encode()):
                fails["n"] = 0
                return self._json(200, {"ok": True}, [("Set-Cookie", f"edp={papa_token}; Max-Age={PAPA_TTL}; Path=/; HttpOnly; SameSite=Strict")])
            fails["n"] += 1
            time.sleep(min(10, 1.5 * fails["n"]))  # freine les essais en série
            return self._json(403, {"erreur": "mot de passe incorrect"})

        def _send(self, code, body, ctype, extra=()):
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            for k, v in extra:
                self.send_header(k, v)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = urlsplit(self.path)
            url, query = unquote(parts.path), parse_qs(parts.query)
            if url == "/connect":
                if not self._local() or not lan_url:
                    self.send_error(404)
                    return
                # prénoms lus dans espaces.json (non versionné) : rien de personnel dans le code
                kids = [e.get("prenom", "") for e in load_json(ROOT / "espaces.json", []) or [] if e.get("source") != "papa"]
                label = html.escape("Le carnet de " + " & ".join(kids) if kids else "Le carnet de la famille")
                page = CONNECT_PAGE.replace("__URL__", json.dumps(lan_url)).replace("__KIDS__", label)
                return self._send(200, page.encode(), "text/html; charset=utf-8")
            if url in public:
                path, ctype = public[url]
                return self._send(200, path.read_bytes(), ctype) if path.exists() else self.send_error(404)
            ok, via_key = self._authorized(query)
            if not ok:
                return self._send(401, "Accès protégé : ouvre le lien avec la clé (voir « serve --lan »).".encode(),
                                  "text/plain; charset=utf-8")
            # Clé reçue dans l'URL : on pose aussi un cookie pour data.json et les fichiers. Pas de redirection :
            # l'URL avec ?k= doit rester celle du raccourci « écran d'accueil » (cookies séparés sur iOS).
            extra = [("Set-Cookie", f"edk={token}; Max-Age=31536000; Path=/; HttpOnly; SameSite=Lax")] if via_key else []
            if url == "/data.json":
                # données publiques du dashboard : sans la partie famille (privée → /papa.json)
                data = load_json(OUT / ("demo.json" if args.demo else "dashboard.json"))
                if data is None:
                    return self.send_error(404)
                data.pop("famille", None)
                data["papa"] = {"configure": bool(papa_pass) or args.demo}
                data["serveur"] = {"lance": started}
                return self._json(200, data, extra)
            if url == "/papa.json":
                if not self._papa():
                    return self._json(401, {"erreur": "verrouillé"}, extra)
                data = load_json(OUT / ("demo.json" if args.demo else "dashboard.json")) or {}
                return self._json(200, {"famille": data.get("famille")}, extra)
            if url.startswith("/files/") and not self._papa():
                return self._send(401, "Espace Papa verrouillé.".encode(), "text/plain; charset=utf-8", extra)
            if url.startswith("/files/") and not args.demo:
                # uniquement les fichiers indexés par fetch (pas de traversée de chemin)
                index = load_json(FILES / "index.json", {})
                entry = next((e for e in index.values() if e["path"] == url[7:]), None)
                path, ctype = (FILES / entry["path"], entry["mime"] or "application/octet-stream") if entry else (None, None)
            elif url.startswith("/revisions/") and not args.demo:
                # uniquement les PDF listés dans revisions/index.json
                name = url[len("/revisions/"):]
                listed = {f["path"] for r in load_json(REVS / "index.json", []) or [] for f in r.get("fichiers", [])}
                path, ctype = (REVS / name, "application/pdf") if name in listed else (None, None)
            elif url.startswith("/archives/") and not args.demo:
                # PDF du dossier archives/ uniquement (chemin résolu puis vérifié : pas de traversée)
                cand = (ARCH / url[len("/archives/"):]).resolve()
                ok = cand.suffix.lower() == ".pdf" and cand.is_relative_to(ARCH.resolve())
                path, ctype = (cand, "application/pdf") if ok else (None, None)
            else:
                path, ctype = routes.get(url, (None, None))
            if not path or not path.exists():
                self.send_error(404)
                return
            self._send(200, path.read_bytes(), ctype, extra)

        def log_message(self, *a):
            pass

    srv = ThreadingHTTPServer((host, args.port), H)
    print(f"dashboard → http://127.0.0.1:{args.port}", file=sys.stderr)
    if args.lan:
        print(f"réseau local → {lan_url}\n"
              f"QR code pour l'iPad → http://127.0.0.1:{args.port}/connect (à ouvrir sur ce Mac)", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


def main():
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("fetch").set_defaults(fn=cmd_fetch)
    sub.add_parser("normalize").set_defaults(fn=cmd_normalize)
    sub.add_parser("vacances").set_defaults(fn=cmd_vacances)
    sub.add_parser("demo").set_defaults(fn=cmd_demo)
    lp = sub.add_parser("livret", help="importe un livret scolaire PDF complet dans archives/ (périodes manquantes seulement)")
    lp.add_argument("pdf"); lp.add_argument("prenom"); lp.add_argument("--dry-run", action="store_true")
    lp.set_defaults(fn=cmd_livret)
    sp = sub.add_parser("serve")
    sp.add_argument("--port", type=int, default=8765)
    sp.add_argument("--demo", action="store_true", help="sert out/demo.json au lieu de out/dashboard.json")
    sp.add_argument("--lan", action="store_true", help="accessible depuis le réseau local, protégé par ED_DASH_TOKEN")
    sp.set_defaults(fn=cmd_serve)
    args = p.parse_args()
    args.fn(args)


if __name__ == "__main__":
    main()
