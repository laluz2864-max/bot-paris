#!/usr/bin/env python3
"""Suivi en direct des paris de Lucas.

Toutes les 5 minutes (GitHub Actions) :
1. lit dans Notion les paris « en attente » qui ont commencé (ou commencent dans 10 min) ;
2. récupère le score en direct (ESPN, gratuit, sans clé) ;
3. envoie sur Telegram : début du match, buts / sets, pari passé, perdu ou remboursé ;
4. écrit le résultat dans Notion → l'appli Bankroll se met à jour toute seule.

Secrets GitHub nécessaires : NOTION_TOKEN, TG_TOKEN, TG_CHAT.
"""
import json, os, re, sys, time, unicodedata
import urllib.request, urllib.parse
from datetime import datetime, timedelta, timezone

NOTION = os.environ["NOTION_TOKEN"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ["TG_CHAT"]
DS = "a42805ab-7d55-47f9-8e8c-1aba3dad0998"  # base Notion « Paris Bot — picks »
APP = "https://claude.ai/artifact/DijYfsGgXBqTVn8sAthfK2"
ETAT_F = "etat.json"
NOW = datetime.now(timezone.utc)

# Quel flux ESPN interroger selon le sport / la compétition
ESPN_FOOT = {
    "ligue 1": "soccer/fra.1", "ligue 2": "soccer/fra.2", "premier league": "soccer/eng.1",
    "championship": "soccer/eng.2", "liga": "soccer/esp.1", "serie a": "soccer/ita.1",
    "bundesliga": "soccer/ger.1", "eredivisie": "soccer/ned.1", "primeira": "soccer/por.1",
    "champions": "soccer/uefa.champions", "europa league": "soccer/uefa.europa",
    "conference": "soccer/uefa.europa.conf", "nations": "soccer/uefa.nations",
    "coupe de france": "soccer/fra.coupe_de_france", "mls": "soccer/usa.1",
    "belg": "soccer/bel.1", "turq": "soccer/tur.1", "super lig": "soccer/tur.1",
}


def http(url, data=None, headers=None, method=None):
    h = {"User-Agent": "curl/8.5.0", "Accept": "*/*"}
    h.update(headers or {})
    body = json.dumps(data).encode() if data is not None else None
    if body:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=body, headers=h, method=method)
    with urllib.request.urlopen(req, timeout=30) as r:
        return json.loads(r.read().decode() or "{}")


def notion(path, data=None, method=None):
    return http(f"https://api.notion.com/v1/{path}", data,
                {"Authorization": f"Bearer {NOTION}", "Notion-Version": "2025-09-03"}, method)


def telegram(text):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": text, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(
            f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=30)
    except Exception as e:
        print("telegram:", e)


def norm(s):
    s = unicodedata.normalize("NFKD", str(s or "")).encode("ascii", "ignore").decode().lower()
    return re.sub(r"[^a-z0-9 ]", " ", s)


def mots(s):
    return [w for w in norm(s).split() if len(w) > 2]


def meme_nom(a, b):
    """Vrai si les deux noms partagent un mot significatif (nom de famille, ville…)."""
    return bool(set(mots(a)) & set(mots(b)))


# ---------- Notion ----------
def paris_a_suivre():
    out, cursor = [], None
    while True:
        body = {"page_size": 100, "filter": {"property": "Resultat", "select": {"equals": "attente"}}}
        if cursor:
            body["start_cursor"] = cursor
        r = notion(f"data_sources/{DS}/query", body, "POST")
        print(f"Notion : {len(r.get('results', []))} pari(s) en attente")
        for p in r.get("results", []):
            pr = p["properties"]
            txt = "".join(t.get("plain_text", "") for t in pr.get("Data", {}).get("rich_text", []))
            try:
                d = json.loads(txt)
            except Exception:
                continue
            deb = (pr.get("Debut", {}).get("date") or {}).get("start")
            if not deb or not d.get("suivi") or "test" in str(d.get("id", "")):
                continue
            debut = datetime.fromisoformat(deb.replace("Z", "+00:00"))
            if debut.tzinfo is None:
                debut = debut.replace(tzinfo=timezone.utc)
            if debut - timedelta(minutes=10) <= NOW <= debut + timedelta(hours=8):
                out.append({"page": p["id"], "debut": debut, **d})
        if not r.get("has_more"):
            return out
        cursor = r.get("next_cursor")


def ecrire_resultat(page, res):
    notion(f"pages/{page}", {"properties": {"Resultat": {"select": {"name": res}}}}, "PATCH")


# ---------- ESPN ----------
_cache = {}


def scoreboard(path, jour):
    key = (path, jour)
    if key not in _cache:
        try:
            _cache[key] = http(f"https://site.api.espn.com/apis/site/v2/sports/{path}/scoreboard?dates={jour}&limit=300")
        except Exception as e:
            print("espn", path, e)
            _cache[key] = {}
    return _cache[key]


def chemins(p):
    s = p.get("suivi", {})
    if s.get("espn"):
        return [s["espn"]]
    sport, comp = p.get("sport", ""), norm(p.get("competition", ""))
    if sport == "tennis":
        return ["tennis/atp", "tennis/wta"]
    if sport in ("nba",) or "nba" in comp:
        return ["basketball/nba"]
    if sport == "basket":
        return ["basketball/nba", "basketball/wnba"]
    if sport == "hockey":
        return ["hockey/nhl"]
    if sport == "foot":
        return [v for k, v in ESPN_FOOT.items() if k in comp] or ["soccer/all"]
    return []


def trouver_match(p):
    """Renvoie un dict normalisé {etat, termine, statut, joueurs:[{nom, score, jeux:[..], gagnant}]}."""
    a, _, b = p["match"].partition(" - ")
    jours = sorted({(p["debut"] + timedelta(hours=h)).strftime("%Y%m%d") for h in (-6, 0, 6)})
    for path in chemins(p):
        for j in jours:
            sb = scoreboard(path, j)
            comps = []
            for ev in sb.get("events", []):
                if ev.get("competitions"):
                    comps += ev["competitions"]
                for g in ev.get("groupings", []):
                    comps += g.get("competitions", [])
            for c in comps:
                cs = c.get("competitors", [])
                if len(cs) != 2:
                    continue
                noms = [(x.get("athlete") or x.get("team") or {}).get("displayName", "") for x in cs]
                if not ((meme_nom(a, noms[0]) and meme_nom(b, noms[1])) or (meme_nom(a, noms[1]) and meme_nom(b, noms[0]))):
                    continue
                t = c.get("status", {}).get("type", {})
                joueurs = []
                for x, n in zip(cs, noms):
                    jeux = [int(l.get("value") or 0) for l in x.get("linescores", [])]
                    sc = x.get("score")
                    try:
                        sc = int(float(sc)) if sc not in (None, "") else None
                    except Exception:
                        sc = None
                    joueurs.append({"nom": n, "score": sc, "jeux": jeux, "gagnant": bool(x.get("winner"))})
                return {"etat": t.get("state"), "termine": bool(t.get("completed")), "statut": t.get("name", ""),
                        "detail": t.get("shortDetail", ""), "joueurs": joueurs, "tennis": path.startswith("tennis")}
    return None


# ---------- Évaluation du pari ----------
def valeur(m, j, unite):
    if m["tennis"]:
        if unite == "sets":
            return sum(1 for i, g in enumerate(j["jeux"]) if g > autre(m, j)["jeux"][i] and (g >= 6 or m["termine"]))
        return sum(j["jeux"])
    return j["score"] or 0


def autre(m, j):
    return m["joueurs"][1] if m["joueurs"][0] is j else m["joueurs"][0]


def cible(m, nom):
    for j in m["joueurs"]:
        if meme_nom(nom, j["nom"]):
            return j
    return None


def evaluer(p, m):
    """Renvoie ('gagne'|'perdu'|'annule'|None, texte du score)."""
    s = p["suivi"]
    typ, unite = s.get("type"), s.get("unite", "jeux" if m["tennis"] else "points")
    j0, j1 = m["joueurs"]
    if m["tennis"]:
        score = " ".join(f"{a}-{b}" for a, b in zip(j0["jeux"], j1["jeux"])) or "0-0"
    else:
        score = f"{j0['score'] or 0}-{j1['score'] or 0}"
    score = f"{j0['nom']} {score} {j1['nom']}"
    fini = m["termine"]
    if any(k in m["statut"] for k in ("WALKOVER", "RETIRED", "ABANDON", "CANCEL", "POSTPON")):
        return "annule", score
    c = cible(m, s.get("cible", "")) if s.get("cible") else None
    ligne = float(s.get("ligne", 0) or 0)
    sens = s.get("sens", "over")

    if typ in ("total", "joueur_total"):
        if typ == "joueur_total":
            if not c:
                return None, score
            v = valeur(m, c, unite)
        else:
            v = valeur(m, j0, unite) + valeur(m, j1, unite)
        if sens == "over":
            if v > ligne:
                return "gagne", score
            return ("perdu" if fini else None), score
        if v > ligne:
            return "perdu", score
        return ("gagne" if fini else None), score

    if typ == "btts":
        if (j0["score"] or 0) > 0 and (j1["score"] or 0) > 0:
            return "gagne", score
        return ("perdu" if fini else None), score

    if not fini or not c:
        return None, score
    o = autre(m, c)
    if typ == "vainqueur":
        return ("gagne" if c["gagnant"] else "perdu"), score
    if typ == "double_chance":
        return ("perdu" if o["gagnant"] else "gagne"), score
    if typ == "handicap":
        diff = valeur(m, c, unite) + ligne - valeur(m, o, unite)
        return ("gagne" if diff > 0 else "perdu" if diff < 0 else "annule"), score
    return None, score


# ---------- Boucle principale ----------
def main():
    etat = json.load(open(ETAT_F)) if os.path.exists(ETAT_F) else {}
    paris = paris_a_suivre()
    print(f"{len(paris)} pari(s) à suivre")
    for p in paris:
        pid = p["id"]
        e = etat.setdefault(pid, {})
        m = trouver_match(p)
        if not m:
            print(pid, "match introuvable")
            continue
        res, score = evaluer(p, m)
        tete = f"{p['match']} — {p['pari']}"
        if m["etat"] == "in" and not e.get("debut"):
            e["debut"] = True
            telegram(f"▶️ C'est parti : {tete}")
        # Nouveau but / nouveau set terminé / nouvelle période
        if m["etat"] in ("in", "post") and score != e.get("score"):
            if e.get("score") and res is None:
                telegram(f"📢 {score}\n({p['pari']})")
            e["score"] = score
        if res:
            txt = {"gagne": "✅ PARI PASSÉ !", "perdu": "❌ Pari perdu", "annule": "↩️ Pari remboursé / annulé"}[res]
            try:
                ecrire_resultat(p["page"], res)
            except Exception as ex:
                print("notion", ex)
                continue  # on retentera au prochain passage
            telegram(f"{txt}\n{tete}\nScore : {score}\n👉 {APP}")
            e["fini"] = res
    # garde l'état léger : on oublie les paris réglés depuis longtemps
    if len(etat) > 300:
        etat = dict(list(etat.items())[-200:])
    json.dump(etat, open(ETAT_F, "w"), ensure_ascii=False, indent=1)


if __name__ == "__main__":
    main()
