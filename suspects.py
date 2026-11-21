#!/usr/bin/env python3
"""Bot « chutes de cotes suspectes » de Lucas — SIMULATION ET DOCUMENTATION UNIQUEMENT, jamais de pari.

Toutes les 10 minutes, relève les cotes 1N2 des compétitions secondaires (via The Odds API) pour les
matchs qui commencent dans les 4 prochaines heures, archive chaque relevé avec l'heure exacte, et
envoie une alerte Telegram quand la cote d'une issue chute brutalement (>= SEUIL) sur plusieurs
bookmakers en peu de temps. Chaque alerte est ensuite réglée (résultat du match) pour mesurer, en
simulation, si ces alertes repèrent vraiment quelque chose.

Variables d'environnement : ODDS_API_KEY, TG_TOKEN, TG_CHAT, DUREE_MIN (durée de la boucle).
"""
import json, os, time, statistics, urllib.request, urllib.parse, csv
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

KEY = os.environ["ODDS_API_KEY"]
TG_TOKEN = os.environ["TG_TOKEN"]
TG_CHAT = os.environ.get("TG_CHAT", "6893492357")
DUREE_MIN = int(os.environ.get("DUREE_MIN", "60"))
PARIS = ZoneInfo("Europe/Paris")

INTERVALLE_S = 600          # un relevé toutes les 10 minutes
HORIZON_H = 4               # on surveille les matchs qui commencent dans les 4 h
SEUIL = 0.20                # chute d'au moins 20 % de la cote
FENETRE_MIN = 120           # ... par rapport au plus haut des 2 dernières heures
MIN_BOOKS = 3               # sur au moins 3 bookmakers (médiane), pour éviter les erreurs d'un seul site
COTE_MAX_AVANT = 6.0        # on ignore les très grosses cotes (trop volatiles)
BUDGET_JOUR = 550           # crédits The Odds API max par jour (le bot de paris principal garde le reste)
BOOKS = "pinnacle,betfair_ex_eu,marathonbet,onexbet,williamhill,sport888,unibet_eu,winamax_fr,betclic_fr,pmu_fr"

EXCLUS = ("soccer_epl", "soccer_spain_la_liga", "soccer_italy_serie_a", "soccer_germany_bundesliga",
          "soccer_france_ligue_one", "soccer_uefa", "soccer_fifa", "soccer_conmebol", "soccer_usa_mls")
EXCLUS_BASKET = ("basketball_nba", "basketball_ncaab", "basketball_wnba", "basketball_euroleague")

ETAT = "etat_suspects.json"
ARCHIVE = "archive_cotes.csv"
ALERTES = "alertes_suspectes.csv"


def api(path, **p):
    p["apiKey"] = KEY
    url = f"https://api.the-odds-api.com/v4/{path}?{urllib.parse.urlencode(p)}"
    with urllib.request.urlopen(url, timeout=30) as r:
        return json.loads(r.read().decode()), dict(r.headers)


def telegram(txt):
    data = urllib.parse.urlencode({"chat_id": TG_CHAT, "text": txt, "disable_web_page_preview": "true"}).encode()
    try:
        urllib.request.urlopen(urllib.request.Request(f"https://api.telegram.org/bot{TG_TOKEN}/sendMessage", data=data), timeout=20)
    except Exception as e:
        print("telegram", e)


def charger():
    try:
        return json.load(open(ETAT))
    except Exception:
        return {"histo": {}, "alertes": [], "credits": {}, "events": {}, "events_maj": ""}


def sauver(e):
    json.dump(e, open(ETAT, "w"), ensure_ascii=False)


def iso(d):
    return d.strftime("%Y-%m-%dT%H:%M:%SZ")


def competitions():
    sp, _ = api("sports")  # gratuit
    return [s["key"] for s in sp if s["active"] and not s["key"].endswith("winner") and (
        (s["key"].startswith("soccer_") and not s["key"].startswith(EXCLUS)) or
        (s["key"].startswith("basketball_") and s["key"] not in EXCLUS_BASKET))]


def maj_events(e, now):
    """Liste des matchs à venir par compétition (endpoint gratuit), rafraîchie toutes les heures."""
    if e.get("events_maj") and now - datetime.fromisoformat(e["events_maj"]) < timedelta(hours=1):
        return
    ev = {}
    for k in competitions():
        try:
            lst, _ = api(f"sports/{k}/events", commenceTimeFrom=iso(now), commenceTimeTo=iso(now + timedelta(hours=HORIZON_H + 1)))
            if lst:
                ev[k] = [x["commence_time"] for x in lst]
        except Exception as ex:
            print("events", k, ex)
    e["events"], e["events_maj"] = ev, now.isoformat()


def releve(e, now):
    jour = now.strftime("%Y-%m-%d")
    dep = e["credits"].get(jour, 0)
    # compétitions avec un match dans l'horizon, les plus proches d'abord
    cibles = []
    for k, debuts in e["events"].items():
        prochains = [datetime.fromisoformat(d.replace("Z", "+00:00")) for d in debuts]
        prochains = [d for d in prochains if now < d <= now + timedelta(hours=HORIZON_H)]
        if prochains:
            cibles.append((min(prochains), k))
    cibles.sort()
    lignes = []
    dernier = e.setdefault("dernier_releve", {})
    for proche, k in cibles:
        # match dans les 2 h : relevé toutes les 10 min ; sinon toutes les 30 min
        ecart = timedelta(minutes=9) if proche - now <= timedelta(hours=2) else timedelta(minutes=29)
        if k in dernier and now - datetime.fromisoformat(dernier[k]) < ecart:
            continue
        dernier[k] = now.isoformat()
        if dep >= BUDGET_JOUR:
            print("budget du jour atteint"); break
        try:
            evs, h = api(f"sports/{k}/odds", bookmakers=BOOKS, markets="h2h", oddsFormat="decimal",
                         commenceTimeFrom=iso(now), commenceTimeTo=iso(now + timedelta(hours=HORIZON_H)))
        except Exception as ex:
            print("odds", k, ex); continue
        dep += int(h.get("x-requests-last", h.get("X-Requests-Last", 1)) or 1)
        for ev in evs:
            prix = {}
            for b in ev.get("bookmakers", []):
                for m in b["markets"]:
                    for o in m["outcomes"]:
                        prix.setdefault(o["name"], {})[b["key"]] = o["price"]
            for issue, books in prix.items():
                cle = f'{ev["id"]}|{issue}'
                med = statistics.median(books.values())
                e["histo"].setdefault(cle, []).append([now.isoformat(), med, len(books), books.get("pinnacle")])
                lignes.append([now.astimezone(PARIS).strftime("%Y-%m-%d %H:%M"), k, f'{ev["home_team"]} - {ev["away_team"]}',
                               ev["commence_time"], issue, round(med, 3), len(books), books.get("pinnacle", "")])
                detecter(e, now, k, ev, issue, cle)
    e["credits"][jour] = dep
    if lignes:
        neuf = not os.path.exists(ARCHIVE)
        with open(ARCHIVE, "a", newline="") as f:
            w = csv.writer(f)
            if neuf:
                w.writerow(["releve_paris", "competition", "match", "debut_utc", "issue", "cote_mediane", "nb_books", "pinnacle"])
            w.writerows(lignes)


def detecter(e, now, k, ev, issue, cle):
    h = e["histo"][cle]
    debut = datetime.fromisoformat(ev["commence_time"].replace("Z", "+00:00"))
    if debut <= now or any(a["cle"] == cle for a in e["alertes"]):
        return
    actu, nb = h[-1][1], h[-1][2]
    if nb < MIN_BOOKS:
        return
    recents = [x for x in h[:-1] if now - datetime.fromisoformat(x[0]) <= timedelta(minutes=FENETRE_MIN)]
    if not recents:
        return
    haut = max(recents, key=lambda x: x[1])
    if haut[1] > COTE_MAX_AVANT or actu > haut[1] * (1 - SEUIL):
        return
    chute = 1 - actu / haut[1]
    duree = int((now - datetime.fromisoformat(haut[0])).total_seconds() // 60)
    a = {"cle": cle, "id": ev["id"], "competition": k, "match": f'{ev["home_team"]} - {ev["away_team"]}',
         "issue": issue, "debut": ev["commence_time"], "cote_avant": round(haut[1], 2), "cote_apres": round(actu, 2),
         "chute_pct": round(chute * 100), "duree_min": duree, "nb_books": nb, "alerte_le": now.isoformat(), "resultat": None}
    e["alertes"].append(a)
    with open(ALERTES, "a", newline="") as f:
        csv.writer(f).writerow([a["alerte_le"], k, a["match"], a["debut"], issue, a["cote_avant"], a["cote_apres"], a["chute_pct"], duree, nb])
    heure = debut.astimezone(PARIS).strftime("%d/%m %Hh%M")
    issue_fr = "Match nul" if issue == "Draw" else f"Victoire {issue}"
    telegram(f"🚨 CHUTE DE COTE ANORMALE\n{k.replace('soccer_', '⚽ ').replace('basketball_', '🏀 ')} — {a['match']} (début {heure})\n"
             f"📉 {issue_fr} : {a['cote_avant']} → {a['cote_apres']} (−{a['chute_pct']} %) en {duree} min, sur {nb} bookmakers\n"
             f"🕒 Relevé à {now.astimezone(PARIS).strftime('%Hh%M')} (heure de Paris), archivé.\n"
             f"⚠️ Simulation et documentation uniquement : ne pas parier. Vérifie d'abord les infos (compo, blessé).")


def regler(e, now):
    """Résultat des alertes dont le match est fini (endpoint scores : 2 crédits par compétition)."""
    a_regler = [a for a in e["alertes"] if a["resultat"] is None and
                now - datetime.fromisoformat(a["debut"].replace("Z", "+00:00")) > timedelta(hours=3)]
    for k in {a["competition"] for a in a_regler}:
        try:
            sc, h = api(f"sports/{k}/scores", daysFrom=3)
        except Exception as ex:
            print("scores", k, ex); continue
        e["credits"][now.strftime("%Y-%m-%d")] = e["credits"].get(now.strftime("%Y-%m-%d"), 0) + 2
        res = {s["id"]: s for s in sc if s.get("completed") and s.get("scores")}
        for a in [x for x in a_regler if x["competition"] == k]:
            s = res.get(a["id"])
            if not s:
                continue
            pts = {x["name"]: float(x["score"]) for x in s["scores"]}
            dom, ext = s["home_team"], s["away_team"]
            gagnant = "Draw" if pts[dom] == pts[ext] else (dom if pts[dom] > pts[ext] else ext)
            a["resultat"] = "gagne" if gagnant == a["issue"] else "perdu"
            a["score"] = f"{int(pts[dom])}-{int(pts[ext])}"
            fin = [x for x in e["alertes"] if x["resultat"]]
            g = sum(1 for x in fin if x["resultat"] == "gagne")
            u = sum((x["cote_apres"] - 1) if x["resultat"] == "gagne" else -1 for x in fin)
            telegram(f"📋 Simulation — {a['match']} {a['score']}\nL'issue signalée ({'nul' if a['issue'] == 'Draw' else a['issue']}) a "
                     f"{'✅ GAGNÉ' if a['resultat'] == 'gagne' else '❌ PERDU'}.\n"
                     f"Bilan des alertes : {g}/{len(fin)} justes | simulation 1 € par alerte à la cote d'après chute : {u:+.2f} €")


def nettoyer(e, now):
    for cle in list(e["histo"]):
        e["histo"][cle] = [x for x in e["histo"][cle] if now - datetime.fromisoformat(x[0]) <= timedelta(hours=6)]
        if not e["histo"][cle]:
            del e["histo"][cle]
    e["credits"] = {j: v for j, v in e["credits"].items() if j >= (now - timedelta(days=40)).strftime("%Y-%m-%d")}


def main():
    fin = time.time() + DUREE_MIN * 60
    while True:
        now = datetime.now(timezone.utc)
        e = charger()
        try:
            maj_events(e, now)
            releve(e, now)
            regler(e, now)
        except Exception as ex:
            print("erreur", ex)
        nettoyer(e, now)
        sauver(e)
        print(now.astimezone(PARIS).strftime("%H:%M"), "crédits du jour :", e["credits"].get(now.strftime("%Y-%m-%d"), 0),
              "| suivis :", len(e["histo"]), "| alertes :", len(e["alertes"]))
        if time.time() + INTERVALLE_S > fin:
            break
        time.sleep(INTERVALLE_S)


if __name__ == "__main__":
    main()
