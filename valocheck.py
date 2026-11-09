# -*- coding: utf-8 -*-
"""
VALOCHECK  -  stima della probabilita' che un giocatore stia barando
====================================================================

Un solo file. Nessuna libreria da installare: serve solo Python 3.9+.

Cosa fa
-------
* Si collega al client di VALORANT come faceva Valorant-Tracker (lockfile,
  token locali, stato della partita, MMR ufficiale Riot).  Quella parte del
  tracker e' stata riscritta qui dentro senza dipendenze esterne.
* Per ogni giocatore della lobby scarica le sue ultime partite competitive e,
  DENTRO OGNI PARTITA, lo confronta con gli altri 9 giocatori su 5 segnali:
      1. headshot %                 2. ADR (danno medio per round)
      3. primi duelli vinti         4. precisione angolare della mira alla kill
      5. kill a raffica (< 300 ms tra due kill con arma)
* Aggiunge il profilo dell'account (livello, stagioni giocate, winrate,
  velocita' di salita RR, pause lunghe).
* Combina tutto in una stima bayesiana (rapporti di verosimiglianza).
* Mostra i risultati in una pagina del browser che si apre da sola.
* Contiene gli strumenti di ricerca: Fase 0 (refunded_rr), Fase 1
  (triangolazione dei cheater confermati), Fase 2 (negativi dal tempo),
  esportazione del dataset per la Fase 4.

Cosa NON fa (scelte volute)
---------------------------
* NON fa instalock e NON seleziona agenti (automazione che viola i termini
  di Riot e non serve allo scopo).
* NON rivela il nome dei giocatori in modalita' "nascosta" (incognito):
  vengono analizzati comunque, ma mostrati come "Nascosto" + agente.
* NON tocca file del gioco e non inietta nulla: legge solo le API che il
  client espone gia' sul tuo PC.

ATTENZIONE: il risultato e' una STIMA statistica, non una prova.  I parametri
del modello sono ipotesi iniziali finche' la Fase 4 non li calibra su dati
etichettati (vedi parametri.json).

Uso:  doppio clic su AVVIA_VALOCHECK.bat  (oppure: py valocheck.py)
      py valocheck.py demo | test | fase0 | fase1 | fase2 | esporta
"""

from __future__ import annotations

import base64
import csv
import gzip
import http.server
import json
import math
import os
import random
import socketserver
import sqlite3
import ssl
import statistics
import sys
import threading
import time
import traceback
import urllib.error
import urllib.request
import webbrowser
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from itertools import combinations
from typing import Dict, List, Optional, Tuple

VERSIONE = "0.2.0"

if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
        sys.stderr.reconfigure(encoding="utf-8")
    except Exception:
        pass

# ════════════════════════════════════════════════════════════════════════════
# CARTELLE E PARAMETRI
# ════════════════════════════════════════════════════════════════════════════

CARTELLA = os.path.dirname(os.path.abspath(__file__))
DATI = os.path.join(CARTELLA, "valocheck_dati")
CACHE_PARTITE = os.path.join(DATI, "partite")
FILE_CONTENUTI = os.path.join(DATI, "contenuti.json")
FILE_CALIBRAZIONE = os.path.join(DATI, "calibrazione_angolo.json")
FILE_DB = os.path.join(DATI, "valocheck.db")
FILE_PARAMETRI = os.path.join(CARTELLA, "parametri.json")
FILE_CHIAVE_HENRIK = os.path.join(CARTELLA, "henrik_key.txt")

PORTA_WEB = 5077

# Tutti i numeri del modello stanno qui. Al primo avvio vengono scritti in
# parametri.json, cosi' si possono cambiare senza toccare il codice.
# Sono IPOTESI DI PARTENZA, non valori stimati: la Fase 4 serve a sostituirli.
PARAMETRI_DEFAULT = {
    "_nota": ("Valori iniziali ipotetici, NON calibrati. mu = di quante "
              "'deviazioni' un cheater sta sopra la lobby in media; sigma = "
              "dispersione dei giocatori onesti; cap = massimo peso di un "
              "singolo segnale (in log-odds)."),
    "prevalenza_a_priori": 0.03,
    "partite_da_analizzare": 6,
    "code_ammesse": ["competitive", "premier", "unrated"],
    "fattore_correlazione_segnali": 0.6,
    "peso_profilo_account": 0.7,
    "segnali": {
        "hs":    {"nome": "Headshot %",              "mu": 1.5, "sigma": 1.2, "cap": 3.0, "minimo_scala": 0.03},
        "adr":   {"nome": "ADR",                     "mu": 1.0, "sigma": 1.2, "cap": 2.0, "minimo_scala": 10.0},
        "fk":    {"nome": "Primi duelli vinti",      "mu": 1.0, "sigma": 1.2, "cap": 2.0, "minimo_scala": 0.08},
        "ang":   {"nome": "Precisione angolare",     "mu": 1.8, "sigma": 1.2, "cap": 3.0, "minimo_scala": 1.5},
        "burst": {"nome": "Kill a raffica <300 ms",  "mu": 1.3, "sigma": 1.2, "cap": 2.5, "minimo_scala": 0.03},
    },
    "soglia_raffica_ms": 300,
    "distanza_minima_angolo": 500,
    "kill_minime_calibrazione": 60,
    "account": {
        "livello":   [[0, 20, 0.7], [20, 50, 0.3], [50, 150, 0.0], [150, 100000, -0.4]],
        "stagioni":  [[0, 2, 0.5], [2, 4, 0.2], [4, 9, 0.0], [9, 1000, -0.4]],
        "winrate":   [[0.0, 0.45, -0.2], [0.45, 0.60, 0.0], [0.60, 0.70, 0.3], [0.70, 1.01, 0.8]],
        "rr_medio":  [[-1000, -5, -0.2], [-5, 8, 0.0], [8, 15, 0.2], [15, 1000, 0.6]],
        "salto_tier": [[-100, 4, 0.0], [4, 1000, 0.4]],
        "pausa_giorni": [[0, 30, 0.0], [30, 90, 0.2], [90, 100000, 0.4]],
        "partite_minime_winrate": 10,
    },
    "soglie_colore": {"giallo": 0.10, "rosso": 0.50},
    "fase1": {"finestra_partite": 12, "max_partite_per_rimborso": 4, "tolleranza_rr": 1},
    "fase2": {"giorni_minimi": 60, "giorni_attivita": 21},
    "henrik_pausa_secondi": 2.2,
}


def _unisci(base: Dict, extra: Dict) -> Dict:
    out = dict(base)
    for k, v in (extra or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _unisci(out[k], v)
        else:
            out[k] = v
    return out


def carica_parametri() -> Dict:
    if not os.path.exists(FILE_PARAMETRI):
        try:
            with open(FILE_PARAMETRI, "w", encoding="utf-8") as fh:
                json.dump(PARAMETRI_DEFAULT, fh, ensure_ascii=False, indent=2)
        except OSError:
            pass
        return json.loads(json.dumps(PARAMETRI_DEFAULT))
    try:
        with open(FILE_PARAMETRI, "r", encoding="utf-8") as fh:
            return _unisci(PARAMETRI_DEFAULT, json.load(fh))
    except Exception:
        print("[!] parametri.json non leggibile: uso i valori predefiniti.")
        return json.loads(json.dumps(PARAMETRI_DEFAULT))


P = carica_parametri()


def assicura_cartelle():
    os.makedirs(CACHE_PARTITE, exist_ok=True)


# ════════════════════════════════════════════════════════════════════════════
# HTTP (solo libreria standard)
# ════════════════════════════════════════════════════════════════════════════

_SSL_LOCALE = ssl.create_default_context()
_SSL_LOCALE.check_hostname = False
_SSL_LOCALE.verify_mode = ssl.CERT_NONE
_SSL_REMOTO = {"ctx": ssl.create_default_context()}


def http_json(metodo: str, url: str, headers: Optional[Dict] = None, corpo=None,
              timeout: float = 10, locale: bool = False) -> Tuple[int, object, Dict]:
    """Esegue una richiesta e restituisce (status, json_o_None, headers)."""
    dati = None
    h = dict(headers or {})
    if corpo is not None:
        dati = json.dumps(corpo).encode("utf-8")
        h.setdefault("Content-Type", "application/json")
    req = urllib.request.Request(url, data=dati, headers=h, method=metodo)
    ctx = _SSL_LOCALE if locale else _SSL_REMOTO["ctx"]
    try:
        with urllib.request.urlopen(req, timeout=timeout, context=ctx) as r:
            raw = r.read()
            try:
                return r.status, json.loads(raw.decode("utf-8")), dict(r.headers)
            except ValueError:
                return r.status, None, dict(r.headers)
    except urllib.error.HTTPError as e:
        try:
            body = json.loads(e.read().decode("utf-8"))
        except Exception:
            body = None
        hh = dict(e.headers or {})
        hh["_errore"] = json.dumps(body)[:300] if body is not None else str(e.reason)
        return e.code, body, hh
    except urllib.error.URLError as e:
        # Alcuni server Riot usano certificati propri: ripiego (come il tracker).
        if not locale and isinstance(getattr(e, "reason", None), ssl.SSLError) \
                and _SSL_REMOTO["ctx"] is not _SSL_LOCALE:
            _SSL_REMOTO["ctx"] = _SSL_LOCALE
            return http_json(metodo, url, headers, corpo, timeout, locale)
        return 0, None, {"_errore": f"rete: {getattr(e, 'reason', e)}"}
    except Exception as e:  # noqa: BLE001
        return 0, None, {"_errore": f"{type(e).__name__}: {e}"}


# ════════════════════════════════════════════════════════════════════════════
# CONTENUTI DI GIOCO (da valorant-api.com, con cache) - preso dal tracker
# ════════════════════════════════════════════════════════════════════════════

VERSIONE_CLIENT_RISERVA = "release-13.08-shipping-20-5715960"
MEDIA = "https://media.valorant-api.com"


class Contenuti:
    def __init__(self):
        self.versione_client = VERSIONE_CLIENT_RISERVA
        self.agenti: Dict[str, str] = {}      # uuid -> nome
        self.mappe: Dict[str, str] = {}       # mapUrl -> nome
        self.tier: Dict[str, str] = {}        # "24" -> "Immortal 1"
        self.atto_corrente = ""
        self.atti: Dict[str, str] = {}        # uuid -> nome
        self._carica()

    def _carica(self):
        try:
            with open(FILE_CONTENUTI, "r", encoding="utf-8") as fh:
                d = json.load(fh)
            self.__dict__.update({k: v for k, v in d.items() if k in self.__dict__})
            self._scaricato = d.get("_scaricato", 0)
        except Exception:
            self._scaricato = 0

    def aggiorna(self, forza: bool = False):
        if not forza and time.time() - getattr(self, "_scaricato", 0) < 12 * 3600 and self.agenti:
            return

        def get(ep):
            s, d, _ = http_json("GET", "https://valorant-api.com/v1" + ep, timeout=8)
            return d.get("data") if s == 200 and isinstance(d, dict) else None

        v = get("/version")
        if isinstance(v, dict) and v.get("riotClientVersion"):
            self.versione_client = v["riotClientVersion"]
        a = get("/agents?isPlayableCharacter=true")
        if isinstance(a, list):
            self.agenti = {x["uuid"].lower(): x["displayName"] for x in a if x.get("uuid")}
        m = get("/maps")
        if isinstance(m, list):
            self.mappe = {x["mapUrl"]: x["displayName"] for x in m if x.get("mapUrl")}
        t = get("/competitivetiers")
        if isinstance(t, list) and t:
            self.tier = {str(x["tier"]): (x.get("tierName") or "").title()
                         for x in t[-1].get("tiers", [])}
        s = get("/seasons")
        if isinstance(s, list):
            ora = datetime.now(timezone.utc)
            for x in s:
                self.atti[x.get("uuid", "")] = x.get("displayName", "")
                if "act" not in (x.get("type") or "").lower():
                    continue
                try:
                    ini = datetime.fromisoformat(x["startTime"].replace("Z", "+00:00"))
                    fin = datetime.fromisoformat(x["endTime"].replace("Z", "+00:00"))
                    if ini <= ora < fin:
                        self.atto_corrente = x["uuid"]
                except Exception:
                    pass
        self._scaricato = time.time()
        try:
            assicura_cartelle()
            d = {k: getattr(self, k) for k in ("versione_client", "agenti", "mappe", "tier",
                                               "atto_corrente", "atti")}
            d["_scaricato"] = self._scaricato
            with open(FILE_CONTENUTI, "w", encoding="utf-8") as fh:
                json.dump(d, fh, ensure_ascii=False)
        except OSError:
            pass

    def nome_agente(self, uuid: str) -> str:
        return self.agenti.get((uuid or "").lower(), "")

    def icona_agente(self, uuid: str) -> str:
        return f"{MEDIA}/agents/{uuid.lower()}/displayicon.png" if uuid else ""

    def nome_mappa(self, map_id: str) -> str:
        if not map_id:
            return ""
        if map_id in self.mappe:
            return self.mappe[map_id]
        cod = map_id.rstrip("/").split("/")[-1].lower()
        for url, nome in self.mappe.items():
            if url.rstrip("/").split("/")[-1].lower() == cod:
                return nome
        return cod

    def nome_tier(self, tier: int) -> str:
        if not tier:
            return "Senza grado"
        return self.tier.get(str(tier), f"Tier {tier}")


contenuti = Contenuti()


# ════════════════════════════════════════════════════════════════════════════
# CONNESSIONE A VALORANT (logica del tracker, riscritta senza dipendenze)
# ════════════════════════════════════════════════════════════════════════════

REGIONE_CHAT_A_AFFINITA = {
    "na": "na", "us": "na", "use": "na", "usw": "na", "latam": "latam", "la": "latam",
    "br": "br", "eu": "eu", "euw": "eu", "eun": "eu", "ru": "eu", "tr": "eu", "me": "eu",
    "kr": "kr", "ap": "ap", "jp": "ap", "oce": "ap", "oc": "ap", "sea": "ap", "as": "ap",
    "apac": "ap", "sa": "ap", "ph": "ap", "th": "ap", "vn": "ap",
}
AFFINITA_A_SHARD = {"eu": "eu", "na": "na", "latam": "na", "br": "na", "ap": "ap", "kr": "kr"}


def risolvi_regione(grezza: str) -> Tuple[str, str]:
    r = (grezza or "").lower().strip()
    aff = REGIONE_CHAT_A_AFFINITA.get(r.rstrip("0123456789")) or REGIONE_CHAT_A_AFFINITA.get(r) or "eu"
    return aff, AFFINITA_A_SHARD.get(aff, aff)


class RiotAPI:
    """Client delle API locali e remote di VALORANT (solo lettura)."""

    def __init__(self):
        self.lockfile = None
        self.token = None
        self.entitlement = None
        self.puuid = None
        self.regione = None
        self.shard = None
        self._lock_ritmo = threading.Lock()
        self._ultimo = 0.0
        self.intervallo_minimo = 0.25   # secondi tra due chiamate remote
        self._piattaforma = base64.b64encode(json.dumps({
            "platformType": "PC", "platformOS": "Windows",
            "platformOSVersion": "10.0.19042.1.256.64bit", "platformChipset": "Unknown",
        }).encode()).decode()
        self._mem_partite: Dict[str, Dict] = {}
        self.registro: List[Tuple[str, str, int, str]] = []   # (metodo, percorso, status, dettaglio)
        self.fonte_versione = ""

    # ---- locale -----------------------------------------------------------

    def _leggi_lockfile(self):
        base = os.getenv("LOCALAPPDATA")
        if not base:
            return None
        p = os.path.join(base, "Riot Games", "Riot Client", "Config", "lockfile")
        try:
            with open(p, "r") as fh:
                parti = fh.read().strip().split(":")
            if len(parti) >= 5:
                return {"porta": parti[2], "password": parti[3], "protocollo": parti[4]}
        except OSError:
            pass
        return None

    def _locale(self, endpoint: str):
        if not self.lockfile:
            return None
        cred = base64.b64encode(f"riot:{self.lockfile['password']}".encode()).decode()
        url = f"{self.lockfile['protocollo']}://127.0.0.1:{self.lockfile['porta']}{endpoint}"
        s, d, _ = http_json("GET", url, {"Authorization": f"Basic {cred}"}, timeout=5, locale=True)
        return d if s == 200 else None

    def _aggiorna_token(self) -> bool:
        d = self._locale("/entitlements/v1/token")
        if d and d.get("accessToken") and d.get("token"):
            self.token, self.entitlement = d["accessToken"], d["token"]
            return True
        return False

    def connetti(self) -> bool:
        self.lockfile = self._leggi_lockfile()
        if not self.lockfile or not self._aggiorna_token():
            return False
        sess = self._locale("/chat/v1/session")
        if not sess:
            return False
        self.puuid = sess.get("puuid")
        self.regione, self.shard = risolvi_regione(sess.get("region", ""))
        return bool(self.puuid)

    def client_raggiungibile(self) -> bool:
        return self._locale("/chat/v1/session") is not None

    def _mia_presenza(self) -> Optional[Dict]:
        d = self._locale("/chat/v4/presences")
        for pr in (d or {}).get("presences", []) or []:
            if pr.get("puuid") == self.puuid and pr.get("product") == "valorant":
                priv = pr.get("private", "")
                try:
                    return json.loads(base64.b64decode(priv)) if isinstance(priv, str) else priv
                except Exception:
                    return {}
        return None

    # ---- remoto -----------------------------------------------------------

    def _remoto(self, metodo: str, url: str, corpo=None):
        if not (self.token and self.entitlement):
            return None
        for tentativo in range(4):
            with self._lock_ritmo:
                attesa = self._ultimo + self.intervallo_minimo - time.time()
                if attesa > 0:
                    time.sleep(attesa)
                self._ultimo = time.time()
            h = {
                "Authorization": f"Bearer {self.token}",
                "X-Riot-Entitlements-JWT": self.entitlement,
                "X-Riot-ClientPlatform": self._piattaforma,
                "X-Riot-ClientVersion": self.versione_client(),
                "User-Agent": "ShooterGame/13 Windows/10.0.19043.1.256.64bit",
            }
            s, d, hh = http_json(metodo, url, h, corpo, timeout=15)
            percorso = url.split(".net", 1)[-1].split("?")[0]
            self.registro.append((metodo, percorso, s, "" if s == 200 else hh.get("_errore", "")))
            if len(self.registro) > 300:
                self.registro = self.registro[-300:]
            if s == 200:
                return d
            if s == 404:
                return None
            if s in (400, 401, 403) and tentativo == 0 and self._aggiorna_token():
                continue
            if s == 429:
                try:
                    pausa = float(hh.get("Retry-After") or hh.get("retry-after") or 5)
                except ValueError:
                    pausa = 5
                time.sleep(min(max(pausa, 2), 60))
                continue
            time.sleep(0.6)
        return None

    def versione_client(self) -> str:
        """Versione del client: prima dal log di VALORANT sul PC, poi da valorant-api.com."""
        if not self.fonte_versione:
            self._versione = contenuti.versione_client
            self.fonte_versione = "valorant-api.com"
            log = os.path.join(os.getenv("LOCALAPPDATA") or "", "VALORANT", "Saved", "Logs", "ShooterGame.log")
            try:
                with open(log, "r", encoding="utf-8", errors="ignore") as fh:
                    for riga in fh:
                        if "CI server version:" in riga:
                            v = riga.split("CI server version:", 1)[1].strip()
                            parti = v.split("-")
                            if len(parti) >= 4:          # release-13.08-shipping-20-5715960
                                v = "-".join(parti[:2] + ["shipping"] + parti[-2:])
                            self._versione = v
                            self.fonte_versione = "log di VALORANT"
                            break
            except OSError:
                pass
        return self._versione

    @property
    def _glz(self):
        return f"https://glz-{self.regione}-1.{self.shard}.a.pvp.net"

    @property
    def _pd(self):
        return f"https://pd.{self.shard}.a.pvp.net"

    def rileva_stato(self) -> Tuple[str, Optional[str]]:
        priv = self._mia_presenza()
        if priv is None:
            if not self.client_raggiungibile():
                return "DISCONNESSO", None
            return "MENU", None
        mpd = priv.get("matchPresenceData", {}) or {}
        stato = (mpd.get("sessionLoopState") or priv.get("sessionLoopState") or "").upper()
        if stato == "PREGAME":
            d = self._remoto("GET", f"{self._glz}/pregame/v1/players/{self.puuid}")
            return "PREGAME", (d or {}).get("MatchID")
        if stato == "INGAME":
            d = self._remoto("GET", f"{self._glz}/core-game/v1/players/{self.puuid}")
            return "INGAME", (d or {}).get("MatchID")
        return "MENU", None

    def partita_pregame(self, mid):
        return self._remoto("GET", f"{self._glz}/pregame/v1/matches/{mid}")

    def partita_corrente(self, mid):
        return self._remoto("GET", f"{self._glz}/core-game/v1/matches/{mid}")

    def nomi(self, puuids: List[str]) -> Dict[str, str]:
        d = self._remoto("PUT", f"{self._pd}/name-service/v2/players", [p for p in puuids if p])
        out = {}
        for x in d or []:
            if x.get("Subject") and x.get("GameName"):
                out[x["Subject"]] = f"{x['GameName']}#{x.get('TagLine', '')}"
        return out

    def mmr(self, puuid):
        return self._remoto("GET", f"{self._pd}/mmr/v1/players/{puuid}")

    def aggiornamenti_competitivi(self, puuid, n=20):
        return self._remoto("GET", f"{self._pd}/mmr/v1/players/{puuid}/competitiveupdates"
                                   f"?startIndex=0&endIndex={n}&queue=competitive")

    def storico_partite(self, puuid, n=20, coda=None):
        filtro = f"&queue={coda}" if coda else ""
        return self._remoto("GET", f"{self._pd}/match-history/v1/history/{puuid}"
                                   f"?startIndex=0&endIndex={n}{filtro}")

    def dettagli_partita(self, match_id) -> Optional[Dict]:
        """Dettagli completi (round per round). Salvati su disco: non cambiano."""
        if match_id in self._mem_partite:
            return self._mem_partite[match_id]
        f = os.path.join(CACHE_PARTITE, f"{match_id}.json.gz")
        if os.path.exists(f):
            try:
                with gzip.open(f, "rt", encoding="utf-8") as fh:
                    d = json.load(fh)
                self._ricorda(match_id, d)
                return d
            except Exception:
                pass
        d = self._remoto("GET", f"{self._pd}/match-details/v1/matches/{match_id}")
        if d:
            try:
                assicura_cartelle()
                with gzip.open(f, "wt", encoding="utf-8") as fh:
                    json.dump(d, fh)
            except OSError:
                pass
            self._ricorda(match_id, d)
        return d

    def _ricorda(self, mid, d):
        if len(self._mem_partite) > 150:
            self._mem_partite.pop(next(iter(self._mem_partite)))
        self._mem_partite[mid] = d


# ════════════════════════════════════════════════════════════════════════════
# ESTRAZIONE DEI SEGNALI DA UNA PARTITA
# ════════════════════════════════════════════════════════════════════════════

def _id(d: Dict) -> str:
    return d.get("subject") or d.get("puuid") or d.get("Subject") or ""


def _t_round(k: Dict) -> int:
    for chiave in ("timeSinceRoundStartMillis", "roundTime", "round_time"):
        if k.get(chiave) is not None:
            return int(k[chiave])
    return 0


def estrai_partita(md: Dict) -> Optional[Dict]:
    """Riduce i dettagli di una partita ai numeri che servono, per ogni giocatore."""
    if not md or not md.get("players"):
        return None
    info = md.get("matchInfo", {}) or {}
    squadra, round_giocati, livello = {}, {}, {}
    for p in md["players"]:
        u = _id(p)
        if not u or p.get("isObserver"):
            continue
        squadra[u] = p.get("teamId", "")
        round_giocati[u] = (p.get("stats") or {}).get("roundsPlayed", 0) or 0
        livello[u] = p.get("accountLevel")
    vuoto = lambda: {"testa": 0, "corpo": 0, "gambe": 0, "danno": 0, "fk": 0, "fd": 0,
                     "raffiche": 0, "kill": 0, "kill_arma": 0, "geometria": []}
    agg = {u: vuoto() for u in squadra}
    soglia = P["soglia_raffica_ms"]
    dist_min = P["distanza_minima_angolo"]

    rounds = md.get("roundResults") or []
    kill_per_round: Dict[int, List[Dict]] = {}
    for i, r in enumerate(rounds):
        nr = r.get("roundNum", i)
        for ps in r.get("playerStats") or []:
            u = _id(ps)
            if u not in agg:
                continue
            for d in ps.get("damage") or []:
                ric = d.get("receiver")
                if ric == u or squadra.get(ric) == squadra.get(u):
                    continue          # niente autodanno o fuoco amico
                agg[u]["testa"] += d.get("headshots", 0) or 0
                agg[u]["corpo"] += d.get("bodyshots", 0) or 0
                agg[u]["gambe"] += d.get("legshots", 0) or 0
                agg[u]["danno"] += d.get("damage", 0) or 0
            for k in ps.get("kills") or []:
                kill_per_round.setdefault(nr, []).append(k)
    if not kill_per_round:                       # formato alternativo
        for k in md.get("kills") or []:
            kill_per_round.setdefault(k.get("round", 0), []).append(k)

    for nr, kills in kill_per_round.items():
        kills = [k for k in kills if k.get("killer") in agg and k.get("victim") in agg
                 and squadra[k["killer"]] != squadra[k["victim"]]]
        if not kills:
            continue
        kills.sort(key=_t_round)
        agg[kills[0]["killer"]]["fk"] += 1
        agg[kills[0]["victim"]]["fd"] += 1
        tempi: Dict[str, List[int]] = {}
        for k in kills:
            kil = k["killer"]
            agg[kil]["kill"] += 1
            fd = k.get("finishingDamage") or {}
            if (fd.get("damageType") or "").lower() != "weapon":
                continue
            agg[kil]["kill_arma"] += 1
            tempi.setdefault(kil, []).append(_t_round(k))
            vl = k.get("victimLocation") or {}
            for pl in k.get("playerLocations") or []:
                if _id(pl) == kil and pl.get("location") and pl.get("viewRadians") is not None:
                    dx = (vl.get("x", 0) or 0) - (pl["location"].get("x", 0) or 0)
                    dy = (vl.get("y", 0) or 0) - (pl["location"].get("y", 0) or 0)
                    if math.hypot(dx, dy) >= dist_min:
                        agg[kil]["geometria"].append((dx, dy, float(pl["viewRadians"])))
                    break
        for kil, ts in tempi.items():
            ts.sort()
            agg[kil]["raffiche"] += sum(1 for a, b in zip(ts, ts[1:]) if b - a < soglia)

    n_round = len(rounds) or max(round_giocati.values() or [0])
    for u in round_giocati:
        if not round_giocati[u]:
            round_giocati[u] = n_round
    vincitori = {t.get("teamId"): t.get("won") for t in md.get("teams") or []}
    return {
        "match_id": info.get("matchId", ""),
        "inizio_ms": info.get("gameStartMillis", 0),
        "coda": (info.get("queueID") or info.get("queueId") or "").lower(),
        "mappa": info.get("mapId", ""),
        "stagione": info.get("seasonId", ""),
        "squadra": squadra, "round": round_giocati, "livello": livello,
        "vinto": {u: bool(vincitori.get(t)) for u, t in squadra.items()},
        "agg": agg,
    }


# ════════════════════════════════════════════════════════════════════════════
# CALIBRAZIONE AUTOMATICA DELL'ANGOLO DI MIRA
# ════════════════════════════════════════════════════════════════════════════
# Riot non documenta come e' orientato "viewRadians" rispetto alle coordinate
# x,y della mappa.  Invece di indovinare, proviamo le 8 convenzioni possibili
# (assi scambiati / invertiti) e teniamo quella per cui, su migliaia di kill,
# la mira del killer punta piu' coerentemente verso la vittima.  Questo si
# misura con la "lunghezza del vettore medio" R (1 = tutte allineate).

CONVENZIONI = [
    lambda dx, dy: math.atan2(dy, dx), lambda dx, dy: math.atan2(dx, dy),
    lambda dx, dy: math.atan2(-dy, dx), lambda dx, dy: math.atan2(dy, -dx),
    lambda dx, dy: math.atan2(-dx, dy), lambda dx, dy: math.atan2(dx, -dy),
    lambda dx, dy: math.atan2(-dy, -dx), lambda dx, dy: math.atan2(-dx, -dy),
]


def _avvolgi(a: float) -> float:
    return (a + math.pi) % (2 * math.pi) - math.pi


def calibra_angolo(geometrie: List[Tuple[float, float, float]]) -> Optional[Dict]:
    if len(geometrie) < P["kill_minime_calibrazione"]:
        return None
    migliore = None
    for i, f in enumerate(CONVENZIONI):
        res = [_avvolgi(v - f(dx, dy)) for dx, dy, v in geometrie]
        c = sum(math.cos(r) for r in res) / len(res)
        s = sum(math.sin(r) for r in res) / len(res)
        R = math.hypot(c, s)
        if migliore is None or R > migliore["R"]:
            migliore = {"convenzione": i, "scarto": math.atan2(s, c), "R": R, "n": len(res)}
    # R basso = nessuna convenzione spiega i dati: il segnale va disattivato.
    migliore["affidabile"] = migliore["R"] >= 0.5
    return migliore


def errori_angolo_gradi(geom, cal) -> List[float]:
    if not cal or not cal.get("affidabile"):
        return []
    f = CONVENZIONI[cal["convenzione"]]
    return [abs(math.degrees(_avvolgi(v - f(dx, dy) - cal["scarto"]))) for dx, dy, v in geom]


# ════════════════════════════════════════════════════════════════════════════
# CONFRONTO CON LA LOBBY
# ════════════════════════════════════════════════════════════════════════════

def valori_segnali(partita: Dict, cal) -> Dict[str, Dict[str, Optional[float]]]:
    """Valore grezzo di ogni segnale, per ogni giocatore di una partita."""
    out = {}
    for u, a in partita["agg"].items():
        rnd = partita["round"].get(u, 0) or 0
        colpi = a["testa"] + a["corpo"] + a["gambe"]
        err = errori_angolo_gradi(a["geometria"], cal)
        out[u] = {
            "hs": a["testa"] / colpi if colpi >= 10 else None,
            "adr": a["danno"] / rnd if rnd >= 8 else None,
            "fk": (a["fk"] + 1) / (a["fk"] + a["fd"] + 2) if rnd >= 8 else None,
            "ang": statistics.median(err) if len(err) >= 3 else None,
            "burst": a["raffiche"] / rnd if rnd >= 8 else None,
            "_raffiche": a["raffiche"], "_kill_arma": a["kill_arma"], "_round": rnd,
        }
    return out


def z_in_lobby(valori: Dict[str, Dict], bersaglio: str) -> Dict[str, Tuple[float, float, float]]:
    """Per ogni segnale: (z, valore del giocatore, mediana degli altri).

    z = quanto il giocatore sta sopra gli altri 9 della STESSA partita,
    misurato con mediana e MAD (robusti: un altro cheater nella lobby non
    sposta il riferimento).  Per l'angolo, errore piu' piccolo = z positivo.
    """
    out = {}
    mio = valori.get(bersaglio) or {}
    for s, conf in P["segnali"].items():
        x = mio.get(s)
        altri = [v[s] for u, v in valori.items() if u != bersaglio and v.get(s) is not None]
        if x is None or len(altri) < 5:
            continue
        if s == "burst":
            # Conteggi rari: confronto "di Poisson" invece di mediana/MAD.
            # Atteso = kill con arma del giocatore x tasso di raffiche della lobby;
            # la trasformazione radice (Anscombe) rende z ~ Normale(0,1) per gli onesti.
            k, n = mio["_raffiche"], mio["_kill_arma"]
            K = sum(v["_raffiche"] for u, v in valori.items() if u != bersaglio and v.get(s) is not None)
            N = sum(v["_kill_arma"] for u, v in valori.items() if u != bersaglio and v.get(s) is not None)
            R = sum(v["_round"] for u, v in valori.items() if u != bersaglio and v.get(s) is not None)
            atteso = n * (K + 0.5) / (N + 1)
            z = 2 * (math.sqrt(k + 0.375) - math.sqrt(atteso + 0.375))
            out[s] = (max(-6.0, min(6.0, z)), x, K / R if R else 0.0)
            continue
        cen = statistics.median(altri)
        mad = statistics.median([abs(a - cen) for a in altri]) * 1.4826
        dev = statistics.pstdev(altri)
        scala = max(mad, 0.5 * dev, conf["minimo_scala"])
        z = (x - cen) / scala
        if s == "ang":
            z = -z
        out[s] = (max(-6.0, min(6.0, z)), x, cen)
    return out


# ════════════════════════════════════════════════════════════════════════════
# PROFILO DELL'ACCOUNT
# ════════════════════════════════════════════════════════════════════════════

def profilo_account(mmr: Optional[Dict], agg_comp: Optional[Dict], storico: Optional[Dict],
                    livello: Optional[int]) -> Dict:
    prof = {"livello": livello}
    stag = (((mmr or {}).get("QueueSkills") or {}).get("competitive") or {}) \
        .get("SeasonalInfoBySeasonID") or {}
    atti = [s for s in stag.values() if (s.get("NumberOfGames") or 0) > 0]
    prof["stagioni"] = len(atti) if mmr else None
    cur = stag.get(contenuti.atto_corrente) if contenuti.atto_corrente else None
    if cur is None and atti:
        cur = atti[-1]
    if cur and (cur.get("NumberOfGames") or 0) > 0:
        g = cur.get("NumberOfGames") or 0
        w = cur.get("NumberOfWins") or 0
        prof["partite_atto"] = g
        prof["winrate"] = (w + 5) / (g + 10)          # media "prudente" (Beta 5,5)
        prof["tier"] = cur.get("CompetitiveTier", 0)
        prof["rr"] = cur.get("RankedRating", 0)
    tier_max = 0
    for s in stag.values():
        tier_max = max(tier_max, s.get("CompetitiveTier") or 0)
        for t in (s.get("WinsByTier") or {}):
            try:
                tier_max = max(tier_max, int(t))
            except ValueError:
                pass
    prof["tier_max"] = tier_max
    if not prof.get("tier"):
        lu = (mmr or {}).get("LatestCompetitiveUpdate") or {}
        prof["tier"] = lu.get("TierAfterUpdate", 0)
    m = [x for x in ((agg_comp or {}).get("Matches") or []) if x.get("TierAfterUpdate")]
    if len(m) >= 8:
        prof["rr_medio"] = statistics.mean(x.get("RankedRatingEarned", 0) or 0 for x in m)
        prof["salto_tier"] = (m[0].get("TierAfterUpdate", 0) or 0) - (m[-1].get("TierBeforeUpdate", 0) or 0)
    tempi = sorted([h.get("GameStartTime", 0) for h in ((storico or {}).get("History") or [])
                    if h.get("GameStartTime")], reverse=True)
    if len(tempi) >= 2:
        pause = [(a - b) / 86400000 for a, b in zip(tempi, tempi[1:])]
        prof["pausa_giorni"] = max(pause)
        prof["ultima_partita_giorni"] = (time.time() * 1000 - tempi[0]) / 86400000
    return prof


def _tabella(valore, righe) -> float:
    for lo, hi, llr in righe:
        if lo <= valore < hi:
            return float(llr)
    return 0.0


SPIEGAZIONI_ACCOUNT = {
    "livello": "Livello account",
    "stagioni": "Atti competitivi giocati",
    "winrate": "Winrate dell'atto (stima prudente)",
    "rr_medio": "RR medio guadagnato a partita",
    "salto_tier": "Gradi saliti nelle ultime ~20 partite",
    "pausa_giorni": "Pausa piu' lunga tra due partite (giorni)",
}


def llr_account(prof: Dict) -> List[Dict]:
    tab = P["account"]
    voci = []
    for chiave in ("livello", "stagioni", "winrate", "rr_medio", "salto_tier", "pausa_giorni"):
        v = prof.get(chiave)
        if v is None:
            continue
        if chiave == "winrate" and (prof.get("partite_atto") or 0) < tab["partite_minime_winrate"]:
            continue
        llr = _tabella(v, tab[chiave]) * P["peso_profilo_account"]
        testo = f"{v * 100:.0f}%" if chiave == "winrate" else (f"{v:.1f}" if isinstance(v, float) else str(v))
        voci.append({"nome": SPIEGAZIONI_ACCOUNT[chiave], "valore": testo, "llr": round(llr, 2)})
    return voci


# ════════════════════════════════════════════════════════════════════════════
# STIMA BAYESIANA
# ════════════════════════════════════════════════════════════════════════════
# Idea: si parte dalla probabilita' a priori (quanti cheater ci aspettiamo in
# una lobby qualunque).  Ogni indizio la sposta di un "rapporto di
# verosimiglianza": quanto e' piu' probabile osservare quel valore se il
# giocatore bara rispetto a se non bara.  In scala logaritmica gli indizi si
# sommano.  Per ogni segnale prestazionale assumiamo:
#     onesto:  z ~ Normale(0, sigma)      cheater:  z ~ Normale(mu, sigma)
# quindi per ogni partita  log LR = (mu*z - mu^2/2) / sigma^2.
# I 5 segnali sono correlati tra loro (chi ha piu' HS fa anche piu' ADR), per
# questo la loro somma viene ridotta dal "fattore_correlazione_segnali".

def _logit(p):
    return math.log(p / (1 - p))


def _sigmoide(x):
    return 1 / (1 + math.exp(-x))


def stima_bayesiana(z_per_partita: List[Dict[str, Tuple]], voci_account: List[Dict],
                    confermato: bool = False) -> Dict:
    contributi = []
    tot_prest = 0.0
    for s, conf in P["segnali"].items():
        zs = [zp[s] for zp in z_per_partita if s in zp]
        if not zs:
            contributi.append({"chiave": s, "nome": conf["nome"], "n": 0})
            continue
        mu, sg = conf["mu"], conf["sigma"]
        llr = sum((mu * z - mu * mu / 2) / (sg * sg) for z, _, _ in zs)
        llr = max(-conf["cap"], min(conf["cap"], llr))
        tot_prest += llr
        contributi.append({
            "chiave": s, "nome": conf["nome"], "n": len(zs),
            "z_medio": round(statistics.mean(z for z, _, _ in zs), 2),
            "valore": statistics.mean(x for _, x, _ in zs),
            "lobby": statistics.mean(c for _, _, c in zs),
            "llr": round(llr, 2),
        })
    tot_prest *= P["fattore_correlazione_segnali"]
    tot_acc = sum(v["llr"] for v in voci_account)
    logodds = _logit(P["prevalenza_a_priori"]) + tot_prest + tot_acc
    prob = _sigmoide(logodds)
    if confermato:
        prob = max(prob, 0.97)
    n_partite = len(z_per_partita)
    soglie = P["soglie_colore"]
    if prob >= soglie["rosso"]:
        colore, etichetta = "rosso", "Sospetto alto"
    elif prob >= soglie["giallo"]:
        colore, etichetta = "giallo", "Da tenere d'occhio"
    else:
        colore, etichetta = "verde", "Nessun segnale forte"
    if n_partite < 3 and not confermato:
        etichetta += " (pochi dati)"
    return {"prob": prob, "colore": colore, "etichetta": etichetta, "n_partite": n_partite,
            "llr_prestazioni": round(tot_prest, 2), "llr_account": round(tot_acc, 2),
            "contributi": contributi, "account": voci_account, "confermato": confermato}


def formatta_segnale(chiave: str, v: float) -> str:
    if chiave == "hs":
        return f"{v * 100:.0f}%"
    if chiave == "fk":
        return f"{v * 100:.0f}%"
    if chiave == "adr":
        return f"{v:.0f}"
    if chiave == "ang":
        return f"{v:.1f}°"
    if chiave == "burst":
        return f"{v:.2f}/round"
    return f"{v:.2f}"


# ════════════════════════════════════════════════════════════════════════════
# DATABASE LOCALE (serve alle Fasi 1, 2 e 4)
# ════════════════════════════════════════════════════════════════════════════

class Database:
    def __init__(self, percorso: str = FILE_DB):
        assicura_cartelle()
        self.con = sqlite3.connect(percorso, check_same_thread=False)
        self.lock = threading.Lock()
        with self.lock:
            self.con.executescript("""
            CREATE TABLE IF NOT EXISTS giocatori(puuid TEXT PRIMARY KEY, nome TEXT,
                prima_vista REAL, ultima_vista REAL);
            CREATE TABLE IF NOT EXISTS osservazioni(id INTEGER PRIMARY KEY AUTOINCREMENT,
                puuid TEXT, ts REAL, prob REAL, llr_prestazioni REAL, llr_account REAL,
                n_partite INTEGER, dettagli TEXT);
            CREATE TABLE IF NOT EXISTS rimborsi(puuid TEXT, match_id TEXT, data TEXT, rr INTEGER,
                fonte TEXT, partite_candidate TEXT, PRIMARY KEY(puuid, match_id, fonte));
            CREATE TABLE IF NOT EXISTS henrik_controllati(puuid TEXT PRIMARY KEY, ts REAL, esito TEXT);
            CREATE TABLE IF NOT EXISTS confermati(puuid TEXT PRIMARY KEY, punteggio REAL,
                n_partite INTEGER, partite TEXT, ts REAL);
            CREATE TABLE IF NOT EXISTS controlli_attivita(puuid TEXT, ts REAL,
                ultima_partita_ms REAL, esito TEXT, PRIMARY KEY(puuid, ts));
            CREATE TABLE IF NOT EXISTS squadre(match_id TEXT, puuid TEXT, team TEXT,
                PRIMARY KEY(match_id, puuid));
            """)
            self.con.commit()

    def esegui(self, sql, args=()):
        with self.lock:
            cur = self.con.execute(sql, args)
            self.con.commit()
            return cur.fetchall()

    def vedi_giocatore(self, puuid, nome):
        ora = time.time()
        self.esegui("INSERT INTO giocatori VALUES(?,?,?,?) ON CONFLICT(puuid) DO UPDATE SET "
                    "ultima_vista=excluded.ultima_vista, nome=COALESCE(excluded.nome, giocatori.nome)",
                    (puuid, nome, ora, ora))

    def salva_osservazione(self, puuid, an: Dict, extra: Dict):
        self.esegui("INSERT INTO osservazioni(puuid,ts,prob,llr_prestazioni,llr_account,n_partite,dettagli)"
                    " VALUES(?,?,?,?,?,?,?)",
                    (puuid, time.time(), an["prob"], an["llr_prestazioni"], an["llr_account"],
                     an["n_partite"], json.dumps({"analisi": an, "profilo": extra}, default=str)))

    def salva_squadre(self, partita: Dict):
        with self.lock:
            self.con.executemany("INSERT OR IGNORE INTO squadre VALUES(?,?,?)",
                                 [(partita["match_id"], u, t) for u, t in partita["squadra"].items()])
            self.con.commit()

    def squadre_di(self, match_id) -> Dict[str, str]:
        return {u: t for u, t in self.esegui("SELECT puuid, team FROM squadre WHERE match_id=?", (match_id,))}

    def e_confermato(self, puuid) -> bool:
        return bool(self.esegui("SELECT 1 FROM confermati WHERE puuid=?", (puuid,)))


# ════════════════════════════════════════════════════════════════════════════
# ANALIZZATORE: scarica i dati di un giocatore e calcola la stima
# ════════════════════════════════════════════════════════════════════════════

class Analizzatore:
    def __init__(self, api, db: Optional[Database], lavoratori: int = 3, dati_reali: bool = True):
        self.api = api
        self.dati_reali = dati_reali
        self.db = db
        self.risultati: Dict[str, Dict] = {}
        self.lock = threading.Lock()
        self.pool = ThreadPoolExecutor(max_workers=lavoratori)
        self._geom: List[Tuple[float, float, float]] = []
        self._visti_geom = set()
        self.cal = None
        self._n_cal = 0
        if dati_reali:
            try:
                with open(FILE_CALIBRAZIONE, "r", encoding="utf-8") as fh:
                    c = json.load(fh)
                # Le calibrazioni senza "fonte: reale" vengono da test/demo
                # (versioni <= 0.1.2 le salvavano per errore): si ignorano.
                if c.get("fonte") == "reale":
                    self.cal = c
            except Exception:
                pass

    def richiedi(self, puuid: str, livello: Optional[int] = None):
        with self.lock:
            r = self.risultati.get(puuid)
            if r and r["stato"] in ("in coda", "in analisi", "pronto"):
                return
            self.risultati[puuid] = {"stato": "in coda", "analisi": None}
        self.pool.submit(self._sicuro, puuid, livello)

    def _imposta(self, puuid, **kw):
        with self.lock:
            self.risultati.setdefault(puuid, {}).update(kw)

    def _sicuro(self, puuid, livello):
        try:
            self._analizza(puuid, livello)
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            self._imposta(puuid, stato="errore", errore=str(e))

    def _aggiungi_geometria(self, partita):
        with self.lock:
            if partita["match_id"] in self._visti_geom:
                return
            self._visti_geom.add(partita["match_id"])
            for a in partita["agg"].values():
                self._geom.extend(a["geometria"])
            if len(self._geom) > 40000:
                self._geom = self._geom[-40000:]

    def calibrazione(self):
        with self.lock:
            n = len(self._geom)
            dati = list(self._geom) if (self.cal is None or n >= self._n_cal + 300) else None
        if dati:
            nuova = calibra_angolo(dati)
            if nuova and (self.cal is None or nuova["n"] >= min(self.cal.get("n", 0), 2000)):
                nuova["fonte"] = "reale" if self.dati_reali else "simulata"
                with self.lock:
                    self.cal, self._n_cal = nuova, n
                if self.dati_reali:
                    try:
                        assicura_cartelle()
                        with open(FILE_CALIBRAZIONE, "w", encoding="utf-8") as fh:
                            json.dump(nuova, fh)
                    except OSError:
                        pass
        return self.cal

    def _analizza(self, puuid, livello):
        self._imposta(puuid, stato="in analisi")
        storico = self.api.storico_partite(puuid, 20)
        mmr = self.api.mmr(puuid)
        comp = self.api.aggiornamenti_competitivi(puuid, 20)
        code = set(P["code_ammesse"])
        ids = [h["MatchID"] for h in ((storico or {}).get("History") or [])
               if (h.get("QueueID") or "").lower() in code]
        if len(ids) < P["partite_da_analizzare"] and "competitive" in code:
            # chi gioca molti deathmatch: chiedo esplicitamente le competitive
            comp_st = self.api.storico_partite(puuid, 20, "competitive")
            ids += [h["MatchID"] for h in ((comp_st or {}).get("History") or []) if h["MatchID"] not in ids]
        ids = ids[:P["partite_da_analizzare"]]
        partite = []
        for mid in ids:
            p = estrai_partita(self.api.dettagli_partita(mid))
            if p and puuid in p["squadra"]:
                partite.append(p)
                self._aggiungi_geometria(p)
                if self.db and self.dati_reali:
                    self.db.salva_squadre(p)
        cal = self.calibrazione()
        zs = [z for z in (z_in_lobby(valori_segnali(p, cal), puuid) for p in partite) if z]
        if livello is None:
            for p in partite:
                if p["livello"].get(puuid):
                    livello = p["livello"][puuid]
                    break
        if self.db and self.dati_reali:
            registra_rimborsi_riot(self.db, puuid, comp)     # raccolta passiva per la Fase 1
        prof = profilo_account(mmr, comp, storico, livello)
        voci = llr_account(prof)
        confermato = self.db.e_confermato(puuid) if self.db else False
        an = stima_bayesiana(zs, voci, confermato)
        for c in an["contributi"]:
            if c.get("n"):
                c["valore_testo"] = formatta_segnale(c["chiave"], c["valore"])
                c["lobby_testo"] = formatta_segnale(c["chiave"], c["lobby"])
        if not partite:
            an["etichetta"] = "Nessuna partita recente leggibile"
            an["colore"] = "grigio"
        an["grado"] = contenuti.nome_tier(prof.get("tier") or 0)
        an["grado_max"] = contenuti.nome_tier(prof.get("tier_max") or 0)
        an["angolo_attivo"] = bool(cal and cal.get("affidabile"))
        if self.db:
            self.db.salva_osservazione(puuid, an, prof)
        self._imposta(puuid, stato="pronto", analisi=an)
        return an


# ════════════════════════════════════════════════════════════════════════════
# MONITOR: segue lo stato del gioco e costruisce la lobby
# ════════════════════════════════════════════════════════════════════════════

class Monitor:
    def __init__(self, api, analizzatore: Analizzatore, db: Optional[Database]):
        self.api = api
        self.an = analizzatore
        self.db = db
        self.connesso = False
        self.stato = "CONNESSIONE"
        self.match_id = None
        self.lobby: List[Dict] = []
        self.mappa = ""
        self.messaggio = "Avvio..."
        self.lock = threading.Lock()

    def ciclo(self):
        while True:
            try:
                self._passo()
            except Exception as e:  # noqa: BLE001
                self.messaggio = f"Errore temporaneo: {e}"
            time.sleep(2)

    def _passo(self):
        if not self.connesso:
            if self.api.connetti():
                self.connesso = True
                self.messaggio = ("Modalita' demo: dati simulati." if isinstance(self.api, FintaAPI)
                                  else "Collegato a VALORANT.")
            else:
                self.stato = "DISCONNESSO"
                self.messaggio = "VALORANT non trovato. Avvia il gioco e arriva al menu principale."
                time.sleep(2)
                return
        stato, mid = self.api.rileva_stato()
        if stato == "DISCONNESSO":
            self.connesso = False
            self.stato = stato
            return
        if stato in ("PREGAME", "INGAME") and mid and (mid != self.match_id or stato != self.stato):
            if self._costruisci_lobby(stato, mid):
                self.match_id = mid
        self.stato = stato
        if stato == "MENU":
            self.messaggio = ("Nel menu. Quando entri in selezione agente l'analisi parte da sola."
                              + (" Sotto: l'ultima lobby vista." if self.lobby else ""))
        elif stato == "PREGAME":
            self.messaggio = ("Selezione agente: Riot mostra solo la TUA squadra. "
                              "Gli avversari compaiono appena la partita carica.")
        elif stato == "INGAME":
            self.messaggio = "Partita in corso: analisi di tutti e 10 i giocatori."

    def _costruisci_lobby(self, stato, mid) -> bool:
        if stato == "PREGAME":
            info = self.api.partita_pregame(mid)
            if not info:
                return False
            grezzi = (info.get("AllyTeam") or {}).get("Players") or []
            mia = (info.get("AllyTeam") or {}).get("TeamID", "")
            for g in grezzi:
                g["TeamID"] = mia
        else:
            info = self.api.partita_corrente(mid)
            if not info:
                return False
            grezzi = info.get("Players") or []
            mia = next((g.get("TeamID") for g in grezzi if g.get("Subject") == self.api.puuid), "")
        puuids = [g.get("Subject") for g in grezzi if g.get("Subject")]
        nomi = self.api.nomi(puuids)
        lobby = []
        for g in grezzi:
            u = g.get("Subject")
            if not u:
                continue
            ident = g.get("PlayerIdentity") or {}
            nascosto = bool(ident.get("Incognito"))
            livello = None if ident.get("HideAccountLevel") else (ident.get("AccountLevel") or None)
            agente = g.get("CharacterID", "")
            nome_ag = contenuti.nome_agente(agente) or (agente if 0 < len(agente) < 20 else "")
            nome = nomi.get(u, "")
            if nascosto and u != self.api.puuid:
                nome = ""             # rispettiamo la scelta di restare anonimo
            lobby.append({
                "puuid": u, "nome": nome or (f"Nascosto ({nome_ag or '?'})"
                                             if nascosto else "Nome non disponibile"),
                "nascosto": nascosto, "io": u == self.api.puuid,
                "squadra": "alleati" if g.get("TeamID") == mia else "avversari",
                "agente": nome_ag,
                "icona": contenuti.icona_agente(agente) if contenuti.nome_agente(agente) else "",
                "livello": livello,
            })
            if self.db:
                self.db.vedi_giocatore(u, None if nascosto else nome or None)
        with self.lock:
            self.lobby = lobby
            self.mappa = contenuti.nome_mappa(info.get("MapID", ""))
        for x in lobby:
            self.an.richiedi(x["puuid"], x["livello"])
        return True

    def istantanea(self) -> Dict:
        with self.lock:
            lobby = [dict(x) for x in self.lobby]
            mappa = self.mappa
        with self.an.lock:
            for x in lobby:
                r = self.an.risultati.get(x["puuid"]) or {}
                x["stato_analisi"] = r.get("stato", "")
                x["analisi"] = r.get("analisi")
                x["errore"] = r.get("errore", "")
            cal = self.an.cal
        return {"stato": self.stato, "messaggio": self.messaggio, "mappa": mappa,
                "giocatori": lobby, "versione": VERSIONE,
                "calibrazione": cal, "prevalenza": P["prevalenza_a_priori"],
                "condivisione": STATO_CONDIVISIONE["testo"]}


# ════════════════════════════════════════════════════════════════════════════
# PAGINA WEB LOCALE
# ════════════════════════════════════════════════════════════════════════════

PAGINA = r"""<!doctype html><html lang="it"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>Valocheck</title>
<style>
:root{--bg:#0f1218;--card:#181d26;--line:#262d3a;--txt:#e8ecf2;--dim:#8a94a6;
--verde:#3fb27f;--giallo:#e0b341;--rosso:#ef5350;--grigio:#6b7385;--acc:#ff4655}
*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--txt);
font:14px/1.45 "Segoe UI",system-ui,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:14px 22px;border-bottom:1px solid var(--line)}
header h1{font-size:18px;margin:0;letter-spacing:.08em}header h1 b{color:var(--acc)}
#stato{padding:3px 10px;border-radius:99px;background:var(--line);font-size:12px}
#msg{color:var(--dim);flex:1}main{padding:18px 22px;max-width:1250px;margin:auto}
h2{font-size:13px;text-transform:uppercase;letter-spacing:.1em;color:var(--dim);margin:22px 0 10px}
.griglia{display:grid;grid-template-columns:repeat(auto-fill,minmax(225px,1fr));gap:12px}
.g{background:var(--card);border:1px solid var(--line);border-radius:10px;padding:12px;cursor:pointer}
.g.io{outline:1px solid #4a5a7a}.top{display:flex;gap:10px;align-items:center}
.top img{width:40px;height:40px;border-radius:8px;background:#222}
.nome{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.sub{color:var(--dim);font-size:12px}.pc{font-size:26px;font-weight:700;margin-top:8px}
.barra{height:6px;background:var(--line);border-radius:3px;overflow:hidden;margin:4px 0}
.barra i{display:block;height:100%}.et{font-size:12px;font-weight:600}
.verde{color:var(--verde)}.giallo{color:var(--giallo)}.rosso{color:var(--rosso)}.grigio{color:var(--grigio)}
.bg-verde{background:var(--verde)}.bg-giallo{background:var(--giallo)}.bg-rosso{background:var(--rosso)}.bg-grigio{background:var(--grigio)}
.det{display:none;margin-top:10px;border-top:1px solid var(--line);padding-top:8px;font-size:12px}
.g.aperto .det{display:block}.g.aperto{grid-column:1/-1}.griglia{align-items:start}
.det table{max-width:620px}table{width:100%;border-collapse:collapse}
td{padding:2px 0}td.n{text-align:right;font-variant-numeric:tabular-nums}
.piu{color:var(--rosso)}.meno{color:var(--verde)}
footer{color:var(--dim);font-size:12px;padding:18px 22px;max-width:1250px;margin:auto}
.attesa{color:var(--dim);margin-top:10px}
</style></head><body>
<header><h1><b>VALO</b>CHECK</h1><span id="stato">...</span><span id="msg"></span><span id="mappa" class="sub"></span></header>
<main><div id="vuoto" class="attesa"></div>
<h2 id="h-all">La tua squadra</h2><div class="griglia" id="alleati"></div>
<h2 id="h-avv">Avversari</h2><div class="griglia" id="avversari"></div></main>
<footer id="piede"></footer>
<script>
const aperti=new Set();
const nomiStato={DISCONNESSO:"Gioco non trovato",CONNESSIONE:"Connessione",MENU:"Menu",PREGAME:"Selezione agente",INGAME:"In partita"};
function f(x,d=2){return (x>0?"+":"")+x.toFixed(d)}
function scheda(g){
 const a=g.analisi;let corpo="";
 if(!a){corpo=`<div class="attesa">${g.stato_analisi==="errore"?"Errore: "+g.errore:(g.stato_analisi||"in attesa")+"..."}</div>`}
 else{const p=Math.round(a.prob*1000)/10;
  let righe=a.contributi.map(c=>c.n?`<tr><td>${c.nome}</td><td class="n">${c.valore_testo}</td><td class="n sub">lobby ${c.lobby_testo}</td><td class="n ${c.llr>0?'piu':'meno'}">${f(c.llr)}</td></tr>`:`<tr><td>${c.nome}</td><td colspan=3 class="n sub">${c.chiave==='ang'&&!a.angolo_attivo?'in calibrazione':'nessun dato'}</td></tr>`).join("");
  let acc=a.account.map(v=>`<tr><td>${v.nome}</td><td class="n" colspan=2>${v.valore}</td><td class="n ${v.llr>0?'piu':(v.llr<0?'meno':'sub')}">${f(v.llr)}</td></tr>`).join("");
  corpo=`<div class="pc ${a.colore}">${p<1?"&lt;1":p.toFixed(p<10?1:0)}%</div>
  <div class="barra"><i class="bg-${a.colore}" style="width:${Math.max(2,p)}%"></i></div>
  <div class="et ${a.colore}">${a.confermato?"CONFERMATO dalla triangolazione":a.etichetta}</div>
  <div class="det"><div class="sub">Grado ${a.grado} · max ${a.grado_max} · ${a.n_partite} partite confrontate</div>
  <table><tr><td colspan=4 class="sub"><b>Prestazioni vs lobby</b> (peso totale ${f(a.llr_prestazioni)})</td></tr>${righe}
  <tr><td colspan=4 class="sub"><br><b>Profilo account</b> (peso totale ${f(a.llr_account)})</td></tr>${acc}</table>
  <div class="sub"><br>I numeri a destra sono "pesi" in log-odds: positivo spinge verso cheater, negativo verso onesto.</div></div>`}
 return `<div class="g ${g.io?'io':''} ${aperti.has(g.puuid)?'aperto':''}" data-p="${g.puuid}">
 <div class="top">${g.icona?`<img src="${g.icona}" alt="">`:''}<div style="min-width:0">
 <div class="nome">${g.io?"★ ":""}${g.nome}</div><div class="sub">${g.agente||"—"}${g.livello?" · liv. "+g.livello:""}</div></div></div>${corpo}</div>`}
async function aggiorna(){
 try{const r=await fetch("/api/stato");const d=await r.json();
  document.getElementById("stato").textContent=nomiStato[d.stato]||d.stato;
  document.getElementById("msg").textContent=d.messaggio||"";
  document.getElementById("mappa").textContent=d.mappa||"";
  const al=d.giocatori.filter(g=>g.squadra==="alleati"),av=d.giocatori.filter(g=>g.squadra==="avversari");
  document.getElementById("alleati").innerHTML=al.map(scheda).join("");
  document.getElementById("avversari").innerHTML=av.map(scheda).join("");
  document.getElementById("h-all").style.display=al.length?"":"none";
  document.getElementById("h-avv").style.display=av.length?"":"none";
  document.getElementById("vuoto").textContent=d.giocatori.length?"":"Nessuna lobby ancora. Entra in coda: i giocatori appaiono in selezione agente.";
  const c=d.calibrazione;
  document.getElementById("piede").innerHTML=`Valocheck ${d.versione} · probabilità a priori ${(d.prevalenza*100).toFixed(1)}% · calibrazione angolo: ${c?(c.affidabile?"attiva (R="+c.R.toFixed(2)+", "+c.n+" kill)":"NON affidabile, segnale spento"):"in corso"} · dati condivisi: ${d.condivisione||"—"}<br>
  È una stima statistica con parametri ancora da calibrare (Fase 4), non una prova. Clicca un giocatore per vedere il perché del numero.`;
 }catch(e){document.getElementById("msg").textContent="Valocheck non risponde (finestra chiusa?)"}}
document.addEventListener("click",e=>{const g=e.target.closest(".g");if(!g)return;const p=g.dataset.p;
 aperti.has(p)?aperti.delete(p):aperti.add(p);g.classList.toggle("aperto")});
aggiorna();setInterval(aggiorna,2000);
</script></body></html>"""


def avvia_web(monitor: Monitor) -> str:
    class Gestore(http.server.BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def do_GET(self):
            if self.path.startswith("/api/stato"):
                corpo = json.dumps(monitor.istantanea(), default=str).encode("utf-8")
                tipo = "application/json"
            else:
                corpo = PAGINA.encode("utf-8")
                tipo = "text/html; charset=utf-8"
            self.send_response(200)
            self.send_header("Content-Type", tipo)
            self.send_header("Cache-Control", "no-store")
            self.send_header("Content-Length", str(len(corpo)))
            self.end_headers()
            self.wfile.write(corpo)

    class Server(socketserver.ThreadingMixIn, http.server.HTTPServer):
        daemon_threads = True
        allow_reuse_address = True

    for porta in range(PORTA_WEB, PORTA_WEB + 10):
        try:
            srv = Server(("127.0.0.1", porta), Gestore)
        except OSError:
            continue
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        return f"http://127.0.0.1:{porta}"
    raise RuntimeError("Nessuna porta libera tra 5077 e 5086")


# ════════════════════════════════════════════════════════════════════════════
# DATI CONDIVISI (cartella Google Drive comune tra piu' persone)
# ════════════════════════════════════════════════════════════════════════════
# Un database SQLite su una cartella sincronizzata, scritto da due PC insieme,
# si rovina.  Quindi ognuno scrive SOLO il proprio file
# (dati_<codice>.json.gz) e legge quelli degli altri, unendoli al proprio
# database locale.  Nei file condivisi NON finiscono nomi dei giocatori,
# chiavi o token: solo codici anonimi (PUUID), rimborsi, squadre e statistiche.

NOME_CARTELLA_CONDIVISA = "Valocheck_condiviso"
FILE_CARTELLA_CONDIVISA = os.path.join(CARTELLA, "cartella_condivisa.txt")
STATO_CONDIVISIONE = {"testo": "non configurata", "ultima": 0.0}


def trova_cartella_condivisa() -> Optional[str]:
    """Percorso scritto in cartella_condivisa.txt, altrimenti ricerca automatica in Google Drive."""
    try:
        with open(FILE_CARTELLA_CONDIVISA, "r", encoding="utf-8-sig") as fh:
            for riga in fh:
                riga = riga.strip().strip('"')
                if riga and not riga.startswith("#") and os.path.isdir(riga):
                    return riga
    except OSError:
        pass
    radici = []
    if sys.platform == "win32":
        radici += [f"{lettera}:\\" for lettera in "GHIJKLMNOPQRSTUVWXYZDEF"]
    casa = os.path.expanduser("~")
    radici += [casa, os.path.join(casa, "Google Drive")]
    nomi_drive = ["My Drive", "Il mio Drive", "Mon Drive", "Mi unidad", "Meine Ablage", ""]
    for r in radici:
        for nd in nomi_drive:
            c = os.path.join(r, nd, NOME_CARTELLA_CONDIVISA)
            try:
                if os.path.isdir(c):
                    return c
            except OSError:
                continue
    return None


def _mio_codice() -> str:
    """Codice casuale e anonimo che distingue il tuo file da quello degli altri."""
    f = os.path.join(DATI, "codice_contributore.txt")
    try:
        with open(f, "r", encoding="utf-8") as fh:
            c = fh.read().strip()
            if c:
                return c
    except OSError:
        pass
    c = "%08x" % random.SystemRandom().getrandbits(32)
    assicura_cartelle()
    with open(f, "w", encoding="utf-8") as fh:
        fh.write(c)
    return c


def esporta_condivisi(db: Database, cartella: str) -> int:
    dati = {
        "formato": 1, "versione": VERSIONE, "contributore": _mio_codice(), "aggiornato": time.time(),
        "giocatori": db.esegui("SELECT puuid, prima_vista, ultima_vista FROM giocatori"),
        "rimborsi": db.esegui("SELECT puuid, match_id, data, rr, fonte, partite_candidate FROM rimborsi"),
        "squadre": db.esegui("SELECT match_id, puuid, team FROM squadre"),
        "controlli_attivita": db.esegui("SELECT puuid, ts, ultima_partita_ms, esito FROM controlli_attivita"),
        "osservazioni": db.esegui(
            "SELECT o.puuid, o.ts, o.prob, o.llr_prestazioni, o.llr_account, o.n_partite, o.dettagli "
            "FROM osservazioni o JOIN (SELECT puuid, MAX(ts) m FROM osservazioni GROUP BY puuid) u "
            "ON o.puuid=u.puuid AND o.ts=u.m"),
    }
    finale = os.path.join(cartella, f"dati_{dati['contributore']}.json.gz")
    tmp = finale + ".tmp"
    with gzip.open(tmp, "wt", encoding="utf-8") as fh:
        json.dump(dati, fh)
    os.replace(tmp, finale)
    return len(dati["rimborsi"])


def importa_condivisi(db: Database, cartella: str) -> Tuple[int, int]:
    mio = f"dati_{_mio_codice()}.json.gz"
    n_file = n_rimb = 0
    for nome in sorted(os.listdir(cartella)):
        if not (nome.startswith("dati_") and nome.endswith(".json.gz")) or nome == mio:
            continue
        try:
            with gzip.open(os.path.join(cartella, nome), "rt", encoding="utf-8") as fh:
                d = json.load(fh)
        except Exception:
            continue          # file a meta' sincronizzazione: lo riprendo la prossima volta
        n_file += 1
        with db.lock:
            c = db.con
            c.executemany("INSERT INTO giocatori VALUES(?,NULL,?,?) ON CONFLICT(puuid) DO UPDATE SET "
                          "prima_vista=MIN(giocatori.prima_vista, excluded.prima_vista), "
                          "ultima_vista=MAX(giocatori.ultima_vista, excluded.ultima_vista)",
                          [tuple(x) for x in d.get("giocatori", [])])
            prima = c.execute("SELECT COUNT(*) FROM rimborsi").fetchone()[0]
            c.executemany("INSERT OR IGNORE INTO rimborsi VALUES(?,?,?,?,?,?)",
                          [tuple(x) for x in d.get("rimborsi", [])])
            n_rimb += c.execute("SELECT COUNT(*) FROM rimborsi").fetchone()[0] - prima
            c.executemany("INSERT OR IGNORE INTO squadre VALUES(?,?,?)", [tuple(x) for x in d.get("squadre", [])])
            c.executemany("INSERT OR IGNORE INTO controlli_attivita VALUES(?,?,?,?)",
                          [tuple(x) for x in d.get("controlli_attivita", [])])
            for o in d.get("osservazioni", []):
                if not c.execute("SELECT 1 FROM osservazioni WHERE puuid=? AND ts=?", (o[0], o[1])).fetchone():
                    c.execute("INSERT INTO osservazioni(puuid,ts,prob,llr_prestazioni,llr_account,n_partite,dettagli)"
                              " VALUES(?,?,?,?,?,?,?)", tuple(o))
            c.commit()
    return n_file, n_rimb


def sincronizza(db: Database, stampa=print) -> bool:
    cartella = trova_cartella_condivisa()
    if not cartella:
        STATO_CONDIVISIONE["testo"] = "non configurata"
        return False
    try:
        n_file, n_rimb = importa_condivisi(db, cartella)
        miei = esporta_condivisi(db, cartella)
        totale = db.esegui("SELECT COUNT(*) FROM rimborsi")[0][0]
        STATO_CONDIVISIONE.update(ultima=time.time(), testo=(
            f"attiva ({n_file} altri partecipanti, rimborsi in totale: {totale})"))
        stampa(f"[condivisione] letti {n_file} file di altri (+{n_rimb} rimborsi nuovi), "
               f"scritti i tuoi dati ({miei} rimborsi).")
        return True
    except Exception as e:  # noqa: BLE001
        STATO_CONDIVISIONE["testo"] = f"errore: {e}"
        stampa(f"[condivisione] errore: {e}")
        return False


def sincronizza_menu():
    print("\n=== SINCRONIZZAZIONE CON LA CARTELLA CONDIVISA ===\n")
    cartella = trova_cartella_condivisa()
    if not cartella:
        print("[X] Non trovo la cartella condivisa.")
        print(f"    Deve chiamarsi esattamente  {NOME_CARTELLA_CONDIVISA}  ed essere dentro 'Il mio Drive'")
        print(f"    di Google Drive per desktop (di solito  G:\\Il mio Drive\\{NOME_CARTELLA_CONDIVISA} ).")
        print("    Se e' altrove, scrivi il percorso completo nel file cartella_condivisa.txt")
        print("    che ho creato ora nella cartella di Valocheck.")
        if not os.path.exists(FILE_CARTELLA_CONDIVISA):
            with open(FILE_CARTELLA_CONDIVISA, "w", encoding="utf-8") as fh:
                fh.write("# Se Valocheck non trova da solo la cartella condivisa, scrivi qui sotto il percorso\n"
                         f"# completo, per esempio:  G:\\Il mio Drive\\{NOME_CARTELLA_CONDIVISA}\n")
    else:
        print(f"Cartella trovata: {cartella}")
        if sincronizza(Database()):
            print("\nTutto a posto: la condivisione funziona.")
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# HENRIK API (serve solo per refunded_rr: Fase 0 e Fase 1)
# ════════════════════════════════════════════════════════════════════════════

def chiave_henrik() -> str:
    k = os.getenv("HENRIK_API_KEY", "").strip()
    if k:
        return k
    for f in (FILE_CHIAVE_HENRIK, os.path.join(CARTELLA, ".env")):
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                for riga in fh:
                    riga = riga.strip()
                    if not riga or riga.startswith("#"):
                        continue
                    if "=" in riga:
                        nome, _, val = riga.partition("=")
                        if nome.strip() == "HENRIK_API_KEY" and val.strip():
                            return val.strip()
                    elif f == FILE_CHIAVE_HENRIK and riga.startswith("HDEV-"):
                        return riga
        except OSError:
            pass
    if not os.path.exists(FILE_CHIAVE_HENRIK):
        try:
            with open(FILE_CHIAVE_HENRIK, "w", encoding="utf-8") as fh:
                fh.write("# Incolla qui sotto la tua chiave Henrik (inizia con HDEV-) e salva.\n")
        except OSError:
            pass
    return ""


def henrik_storico_mmr(puuid: str, regione: str, chiave: str):
    """Restituisce (lista_voci | None, messaggio)."""
    url = f"https://api.henrikdev.xyz/valorant/v2/by-puuid/mmr-history/{regione}/pc/{puuid}"
    for _ in range(3):
        s, d, hh = http_json("GET", url, {"Authorization": chiave, "Accept": "application/json",
                                          "User-Agent": "valocheck-ricerca"}, timeout=20)
        if s == 200 and isinstance(d, dict):
            return ((d.get("data") or {}).get("history") or []), "ok"
        if s == 429:
            time.sleep(30)
            continue
        if s in (401, 403):
            return None, "chiave Henrik non valida"
        if s == 404:
            return None, "account non trovato"
        return None, f"errore {s}"
    return None, "troppe richieste"


def interpreta_rimborsi(storia: List[Dict]) -> List[Dict]:
    """Per ogni voce con refunded_rr > 0 prova due ipotesi su QUALE partita e' stata rimborsata.

    A) il rimborso e' segnato sulla partita stessa (persa) che era truccata;
    B) il rimborso arriva dopo e restituisce l'RR perso in 1-4 sconfitte
       precedenti: cerchiamo le combinazioni la cui somma coincide con l'importo.
    Come controllo calcoliamo anche quante combinazioni "funzionerebbero" con
    importi spostati di +-7 RR: se sono tante quanto quelle vere, l'ipotesi B
    non dimostra niente (sono coincidenze).
    """
    cfg = P["fase1"]
    voci = sorted(storia, key=lambda v: v.get("date") or "")
    eventi = []

    def combinazioni(prec, importo):
        trovate = []
        for k in range(1, cfg["max_partite_per_rimborso"] + 1):
            for c in combinations(prec, k):
                if abs(sum(-(x.get("last_change") or 0) for x in c) - importo) <= cfg["tolleranza_rr"]:
                    trovate.append([x.get("match_id") for x in c])
        return trovate

    for i, v in enumerate(voci):
        rr = v.get("refunded_rr") or 0
        if rr <= 0:
            continue
        lc = v.get("last_change") or 0
        prec = [x for x in voci[max(0, i - cfg["finestra_partite"]):i] if (x.get("last_change") or 0) < 0]
        comb = combinazioni(prec, rr)
        eventi.append({
            "match_id": v.get("match_id"), "data": v.get("date"), "rr": rr, "last_change": lc,
            "ipotesi_A": lc < 0 and abs(-lc - rr) <= cfg["tolleranza_rr"],
            "combinazioni_B": comb[:50],
            "controllo_casuale_B": (len(combinazioni(prec, rr + 7)) + len(combinazioni(prec, max(1, rr - 7)))) / 2,
            "derank_protetto": bool(v.get("was_derank_protected")),
        })
    return eventi


def partite_rimborsate(ev: Dict) -> List[Tuple[str, float]]:
    """Partite candidate (con peso) a essere quella col cheater."""
    if ev.get("ipotesi_A"):
        return [(ev["match_id"], 1.0)]
    comb = ev.get("combinazioni_B") or []
    if not comb:
        return []
    pesi: Dict[str, float] = {}
    for c in comb:
        for m in c:
            pesi[m] = pesi.get(m, 0) + 1 / len(comb)
    return sorted(pesi.items(), key=lambda x: -x[1])


def _richiedi_riot() -> RiotAPI:
    api = RiotAPI()
    print("Aggiorno i dati di gioco...")
    contenuti.aggiorna()
    print("Mi collego a VALORANT...")
    if not api.connetti():
        print("\n[X] VALORANT non trovato. Avvia il gioco, arriva al menu principale e riprova.")
        attendi_invio()
        sys.exit(1)
    print(f"    Collegato. Regione {api.regione.upper()}.")
    return api


def attendi_invio():
    try:
        input("\nPremi INVIO per chiudere...")
    except EOFError:
        pass


# ════════════════════════════════════════════════════════════════════════════
# FASE 0 - refunded_rr rivela davvero i rimborsi anti-cheat?
# ════════════════════════════════════════════════════════════════════════════

def fase0():
    print("\n=== FASE 0: verifica del campo refunded_rr ===\n")
    chiave = chiave_henrik()
    api = _richiedi_riot()
    db = Database()
    righe: List[str] = []

    def scrivi(t=""):
        print(t)
        righe.append(t)

    # 1) Lo storico ufficiale Riot contiene campi sui rimborsi?
    scrivi("1) Campi presenti nello storico MMR ufficiale Riot (competitiveupdates):")
    comp = api.aggiornamenti_competitivi(api.puuid, 20) or {}
    campi = set()
    for m in comp.get("Matches") or []:
        campi.update(m.keys())
    sospetti = [c for c in campi if any(w in c.lower() for w in ("refund", "restor", "compens", "penalt"))]
    scrivi("   " + (", ".join(sorted(campi)) or "(nessun dato)"))
    scrivi("   Campi che parlano di rimborsi: " + (", ".join(sospetti) if sospetti else "NESSUNO"))
    if not comp and api.registro:
        scrivi(f"   [!] Riot ha risposto con errore: status {api.registro[-1][2]} {api.registro[-1][3][:150]}")
        scrivi("       Lancia la DIAGNOSI (opzione 7 del menu) e mandami il rapporto.")

    if not chiave:
        scrivi("\n[!] Manca la chiave Henrik. Apri il file henrik_key.txt nella cartella di Valocheck,")
        scrivi("    incolla la chiave (la ottieni gratis su https://docs.henrikdev.xyz) e rilancia.")
        _salva_rapporto("fase0", righe)
        attendi_invio()
        return

    # 2) Raccolgo giocatori: io + chi ho incontrato nelle mie ultime partite
    scrivi("\n2) Raccolgo i giocatori delle tue ultime partite...")
    storico = api.storico_partite(api.puuid, 10) or {}
    puuids = [api.puuid]
    for h in (storico.get("History") or [])[:6]:
        p = estrai_partita(api.dettagli_partita(h["MatchID"]))
        if p:
            puuids += [u for u in p["squadra"] if u not in puuids]
    puuids += [r[0] for r in db.esegui("SELECT puuid FROM giocatori ORDER BY ultima_vista DESC LIMIT 60")
               if r[0] not in puuids]
    puuids = puuids[:60]
    scrivi(f"   {len(puuids)} giocatori da controllare (circa {len(puuids) * P['henrik_pausa_secondi'] / 60:.0f} minuti).")

    # 3) Henrik: refunded_rr
    tot_voci = tot_rimb = tot_A = tot_B_unico = 0
    casuale_B = vero_B = 0.0
    esempi = []
    for i, u in enumerate(puuids, 1):
        storia, msg = henrik_storico_mmr(u, api.regione, chiave)
        time.sleep(P["henrik_pausa_secondi"])
        if storia is None:
            if msg == "chiave Henrik non valida":
                scrivi("[X] La chiave Henrik non e' valida."); break
            continue
        db.esegui("INSERT OR REPLACE INTO henrik_controllati VALUES(?,?,?)", (u, time.time(), "ok"))
        tot_voci += len(storia)
        ev = interpreta_rimborsi(storia)
        for e in ev:
            tot_rimb += 1
            tot_A += e["ipotesi_A"]
            vero_B += len(e["combinazioni_B"])
            casuale_B += e["controllo_casuale_B"]
            tot_B_unico += len(e["combinazioni_B"]) == 1
            db.esegui("INSERT OR REPLACE INTO rimborsi VALUES(?,?,?,?,?,?)",
                      (u, e["match_id"], e["data"], e["rr"], "henrik",
                       json.dumps(partite_rimborsate(e))))
            if len(esempi) < 15:
                esempi.append(e)
        print(f"   [{i}/{len(puuids)}] voci {len(storia)}, rimborsi {len(ev)}")

    scrivi("\n3) RISULTATI")
    scrivi(f"   Voci di storico lette: {tot_voci}")
    scrivi(f"   Voci con refunded_rr > 0: {tot_rimb}")
    if tot_rimb:
        scrivi(f"   Ipotesi A (rimborso = RR perso nella stessa partita): {tot_A}/{tot_rimb}")
        scrivi(f"   Ipotesi B, combinazioni medie che tornano: {vero_B / tot_rimb:.2f}"
               f"  (con importi spostati di 7 RR: {casuale_B / tot_rimb:.2f})")
        scrivi(f"   Rimborsi spiegati da UNA sola combinazione: {tot_B_unico}/{tot_rimb}")
        scrivi("\n   Come leggere:")
        scrivi("   - Se A e' quasi sempre vero  -> refunded_rr e' segnato sulla partita truccata: ottimo per la Fase 1.")
        scrivi("   - Se B torna molto piu' spesso che col controllo spostato -> il rimborso arriva dopo,")
        scrivi("     ma possiamo risalire alle partite. Se i due numeri sono simili, B e' solo caso.")
        scrivi("   - Se e' sempre 0 per tutti, il campo non e' utilizzabile (o Henrik non lo valorizza).")
        scrivi("\n   Esempi:")
        for e in esempi:
            scrivi(f"   {e['data']}  rimborso {e['rr']} RR  partita {e['match_id'][:8]}  "
                   f"cambio {e['last_change']}  A={'si' if e['ipotesi_A'] else 'no'}  "
                   f"B={len(e['combinazioni_B'])} comb.")
    else:
        scrivi("   Nessun rimborso trovato: o i campioni sono pochi, o il campo non viene valorizzato.")
        scrivi("   Prova a rilanciare dopo aver giocato altre partite (piu' giocatori = piu' possibilita').")
    _salva_rapporto("fase0", righe)
    attendi_invio()


def _salva_rapporto(nome, righe):
    assicura_cartelle()
    f = os.path.join(DATI, f"{nome}_rapporto_{datetime.now():%Y%m%d_%H%M}.txt")
    with open(f, "w", encoding="utf-8") as fh:
        fh.write("\n".join(righe))
    print(f"\nRapporto salvato in: {f}")


# ════════════════════════════════════════════════════════════════════════════
# FASE 0b - rimborsi visti direttamente nei dati Riot (senza Henrik)
# ════════════════════════════════════════════════════════════════════════════
# Lo storico ufficiale Riot dice, per ogni partita, RR prima e RR dopo.
# Se tra la fine di una partita e l'inizio della successiva l'RR cambia senza
# aver giocato, e' successo qualcosa "fuori partita": un rimborso (se sale)
# o una penalita'/decadimento (se scende).  Confronto solo partite consecutive
# nello stesso grado e nella stessa stagione, per evitare falsi salti dovuti
# a promozioni, reset di stagione o al grado Immortale (dove l'RR supera 100).

def salti_rr(comp: Optional[Dict]) -> List[Dict]:
    m = [x for x in ((comp or {}).get("Matches") or []) if x.get("TierBeforeUpdate") or x.get("TierAfterUpdate")]
    m = sorted(m, key=lambda x: x.get("MatchStartTime") or 0)          # dal piu' vecchio
    eventi = []
    for prec, cur in zip(m, m[1:]):
        stessa = prec.get("SeasonID") == cur.get("SeasonID")
        stesso_grado = prec.get("TierAfterUpdate") == cur.get("TierBeforeUpdate")
        salto = (cur.get("RankedRatingBeforeUpdate") or 0) - (prec.get("RankedRatingAfterUpdate") or 0)
        if not (stessa and stesso_grado):
            continue
        dentro = ((cur.get("RankedRatingAfterUpdate") or 0) - (cur.get("RankedRatingBeforeUpdate") or 0)
                  - (cur.get("RankedRatingEarned") or 0)) if cur.get("TierAfterUpdate") == cur.get("TierBeforeUpdate") else 0
        if salto or dentro:
            eventi.append({"prima": prec.get("MatchID"), "dopo": cur.get("MatchID"), "salto": salto,
                           "dentro": dentro, "ora": cur.get("MatchStartTime"),
                           "rr_prima": prec.get("RankedRatingEarned")})
    return eventi


def scomponi_aggiornamenti(comp: Optional[Dict]) -> List[Dict]:
    """Per ogni partita: residuo = (RR dopo - RR prima) - RR guadagnato, e la sua causa probabile.

    Scoperta della Fase 0b: Riot somma il rimborso dentro l'aggiornamento della
    partita SUCCESSIVA al rimborso.  Altre cause note del residuo: bonus
    prestazione, penalita' AFK, protezione dal derank (a 0 RR non si scende).
    """
    m = [x for x in ((comp or {}).get("Matches") or []) if x.get("TierBeforeUpdate") or x.get("TierAfterUpdate")]
    m = sorted(m, key=lambda x: x.get("MatchStartTime") or 0)
    out = []
    for i, c in enumerate(m):
        prima = c.get("RankedRatingBeforeUpdate") or 0
        dopo = c.get("RankedRatingAfterUpdate") or 0
        guad = c.get("RankedRatingEarned") or 0
        stesso_grado = c.get("TierAfterUpdate") == c.get("TierBeforeUpdate")
        residuo = ((dopo - prima) - guad) if stesso_grado else 0
        # Campo ufficiale scoperto nella Fase 0b: coincide 6/6 con Henrik.
        ufficiale = c.get("RankedRatingRefundApplied") or 0
        if not ufficiale and residuo == 0:
            continue
        bonus = c.get("RankedRatingPerformanceBonus") or 0
        afk = c.get("AFKPenalty") or 0
        rimborso = 0
        if ufficiale:
            tipo, rimborso = "rimborso (campo Riot)", int(ufficiale)
        elif c.get("WasDerankProtected"):
            tipo = "protezione derank (campo Riot)"
        elif bonus and residuo == bonus:
            tipo = "bonus prestazione"
        elif afk and residuo in (-afk, afk):
            tipo = "penalita' AFK"
        elif c.get("RRPenalty"):
            tipo = "RRPenalty (da capire)"
        else:
            tipo = "altro residuo (da capire)"
        # sconfitte precedenti la cui somma coincide con l'importo (partite col cheater?)
        perse = [x for x in m[max(0, i - 15):i] if (x.get("RankedRatingEarned") or 0) < 0]
        comb = []
        if rimborso > 0:
            for k in range(1, P["fase1"]["max_partite_per_rimborso"] + 1):
                for cc in combinations(perse, k):
                    if abs(sum(-(x.get("RankedRatingEarned") or 0) for x in cc) - rimborso) <= P["fase1"]["tolleranza_rr"]:
                        comb.append([x.get("MatchID") for x in cc])
        out.append({"match_id": c.get("MatchID"), "ora": c.get("MatchStartTime"), "residuo": residuo,
                    "tipo": tipo, "rimborso": max(0, rimborso), "guadagnato": guad,
                    "bonus": bonus, "afk": afk, "combinazioni_B": comb[:50], "ipotesi_A": False})
    return out


def registra_rimborsi_riot(db: "Database", puuid: str, comp: Optional[Dict]) -> List[Dict]:
    eventi = scomponi_aggiornamenti(comp)
    for e in eventi:
        if e["rimborso"] > 0:
            db.esegui("INSERT OR REPLACE INTO rimborsi VALUES(?,?,?,?,?,?)",
                      (puuid, e["match_id"], str(e["ora"]), e["rimborso"], "riot",
                       json.dumps(partite_rimborsate(e))))
    return eventi


def fase0b():
    print("\n=== FASE 0b: rimborsi nei dati ufficiali Riot ===\n")
    api = _richiedi_riot()
    db = Database()
    righe = []

    def scrivi(t=""):
        print(t)
        righe.append(t)

    henrik = {}
    for puuid, mid, rr in db.esegui("SELECT puuid, match_id, rr FROM rimborsi WHERE fonte='henrik'"):
        henrik.setdefault(puuid, {})[mid] = rr
    puuids = list(henrik)
    puuids += [r[0] for r in db.esegui("SELECT puuid FROM henrik_controllati") if r[0] not in henrik]
    puuids += [r[0] for r in db.esegui("SELECT puuid FROM giocatori") if r[0] not in puuids]
    if api.puuid not in puuids:
        puuids.insert(0, api.puuid)
    scrivi(f"Giocatori da controllare: {len(puuids)} (di cui {len(henrik)} con rimborsi secondo Henrik)")

    n_partite = 0
    per_tipo: Dict[str, List[Dict]] = {}
    confronto = []
    campi: Dict[str, int] = {}
    grezzi_henrik: Dict[str, Dict] = {}
    for i, u in enumerate(puuids, 1):
        comp = api.aggiornamenti_competitivi(u, 20)
        ms = (comp or {}).get("Matches") or []
        n_partite += len(ms)
        for x in ms:
            for k, val in x.items():
                if val not in (None, 0, "", False, [], {}):
                    campi[k] = campi.get(k, 0) + 1
                else:
                    campi.setdefault(k, 0)
            if x.get("MatchID") in (henrik.get(u) or {}):
                grezzi_henrik[x["MatchID"]] = x
        ev = registra_rimborsi_riot(db, u, comp)
        for e in ev:
            per_tipo.setdefault(e["tipo"], []).append(e)
        for mid, rr in (henrik.get(u) or {}).items():
            e = next((x for x in ev if x["match_id"] == mid), None)
            in_storico = any(x.get("MatchID") == mid for x in ms)
            confronto.append((u[:8], mid[:8], rr, in_storico, e))
        print(f"   [{i}/{len(puuids)}] partite {len(ms)}, eventi {len(ev)}")

    scrivi("\nRISULTATI")
    scrivi(f"   Partite competitive lette: {n_partite}")
    scrivi("   Partite con residuo (RR dopo - RR prima diverso da RR guadagnato), per causa:")
    for tipo, lista in sorted(per_tipo.items(), key=lambda x: -len(x[1])):
        valori = sorted(e["residuo"] for e in lista)
        scrivi(f"     {tipo:22s} {len(lista):4d}   residui: {', '.join(str(v) for v in valori[:20])}"
               + (" ..." if len(valori) > 20 else ""))
    rimb = [e for l in per_tipo.values() for e in l if e["rimborso"] > 0]
    unici = sum(1 for e in rimb if len(e["combinazioni_B"]) == 1)
    scrivi(f"   RIMBORSI rilevati dai dati Riot: {len(rimb)}  "
           f"(ricondotti a UNA sola combinazione di sconfitte precedenti: {unici})")
    scrivi("\nCONFRONTO con i rimborsi trovati da Henrik:")
    if not confronto:
        scrivi("   Nessun rimborso Henrik nel database.")
    for u, mid, rr, in_st, e in confronto:
        if not in_st:
            esito = "partita non nelle ultime 20 di Riot"
        elif e:
            esito = (f"Riot: residuo {e['residuo']:+d}, causa '{e['tipo']}', rimborso stimato {e['rimborso']}"
                     f"  {'COINCIDE' if abs(e['rimborso'] - rr) <= 1 else 'DIVERSO'}")
        else:
            esito = "Riot: nessun residuo"
        scrivi(f"   giocatore {u}  partita {mid}  Henrik {rr} RR  ->  {esito}")
    scrivi("\nCAMPI dello storico Riot (tra parentesi: in quante partite hanno un valore diverso da zero):")
    scrivi("   " + ", ".join(f"{k} ({v})" for k, v in sorted(campi.items())))
    scrivi("\nDATI GREZZI delle partite con rimborso Henrik (solo numeri, nessun nome):")
    for mid, x in grezzi_henrik.items():
        pulito = {k: v for k, v in x.items() if k not in ("MatchID", "MapID", "SeasonID")}
        scrivi(f"   {mid[:8]}: " + json.dumps(pulito, ensure_ascii=False))
    scrivi("\nI rimborsi Riot sono stati salvati nel database: la Fase 1 li usa anche senza Henrik.")
    _salva_rapporto("fase0b", righe)
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# FASE 1 - triangolazione dei cheater confermati
# ════════════════════════════════════════════════════════════════════════════
# Logica: Riot restituisce l'RR a chi ha perso contro un cheater bannato.  Se
# tre persone diverse ricevono un rimborso per tre partite DIVERSE, chi era
# avversario in tutte e tre e' quasi certamente il cheater.  In una partita
# sola i candidati sono 5 (tutta la squadra avversaria): servono almeno 2
# partite rimborsate distinte per isolarne uno.

def triangola(api, db: Database, stampa=print) -> List[Tuple[str, float, int]]:
    punteggio: Dict[str, float] = {}
    partite: Dict[str, set] = {}
    for puuid, cand_json in db.esegui("SELECT puuid, partite_candidate FROM rimborsi"):
        for mid, peso in json.loads(cand_json or "[]"):
            squadra = db.squadre_di(mid)
            if puuid not in squadra and api is not None:
                p = estrai_partita(api.dettagli_partita(mid))
                if p:
                    db.salva_squadre(p)
                    squadra = p["squadra"]
            if puuid not in squadra:
                continue
            mia = squadra[puuid]
            for c, t in squadra.items():
                if t != mia:
                    punteggio[c] = punteggio.get(c, 0) + peso
                    partite.setdefault(c, set()).add(mid)
    classifica = sorted(((c, s, len(partite[c])) for c, s in punteggio.items()), key=lambda x: -x[1])
    for c, s, n in classifica:
        if n >= 2 and s >= 1.5:
            db.esegui("INSERT OR REPLACE INTO confermati VALUES(?,?,?,?,?)",
                      (c, s, n, json.dumps(sorted(partite[c])), time.time()))
    return classifica


def fase1():
    print("\n=== FASE 1: triangolazione dei cheater confermati ===\n")
    chiave = chiave_henrik()
    api = _richiedi_riot()
    db = Database()
    sincronizza(db)
    righe = []
    if chiave:
        da_fare = [r[0] for r in db.esegui(
            "SELECT g.puuid FROM giocatori g LEFT JOIN henrik_controllati h ON g.puuid=h.puuid "
            "WHERE h.ts IS NULL OR h.ts < ?", (time.time() - 7 * 86400,))]
        print(f"Controllo i rimborsi di {len(da_fare)} giocatori incontrati (Ctrl+C per fermarti, riprende da qui).")
        try:
            for i, u in enumerate(da_fare, 1):
                storia, msg = henrik_storico_mmr(u, api.regione, chiave)
                time.sleep(P["henrik_pausa_secondi"])
                db.esegui("INSERT OR REPLACE INTO henrik_controllati VALUES(?,?,?)", (u, time.time(), msg))
                for e in interpreta_rimborsi(storia or []):
                    db.esegui("INSERT OR REPLACE INTO rimborsi VALUES(?,?,?,?,?,?)",
                              (u, e["match_id"], e["data"], e["rr"], "henrik",
                               json.dumps(partite_rimborsate(e))))
                print(f"   [{i}/{len(da_fare)}] {msg}")
        except KeyboardInterrupt:
            print("\nInterrotto: proseguo con la triangolazione dei dati raccolti finora.")
    else:
        print("[i] Nessuna chiave Henrik: uso solo i dati ufficiali Riot (va bene cosi').")
    tutti = [r[0] for r in db.esegui("SELECT puuid FROM giocatori UNION SELECT puuid FROM henrik_controllati")]
    print(f"Cerco rimborsi nei dati ufficiali Riot di {len(tutti)} giocatori (veloce, niente Henrik)...")
    try:
        for i, u in enumerate(tutti, 1):
            registra_rimborsi_riot(db, u, api.aggiornamenti_competitivi(u, 20))
            if i % 10 == 0 or i == len(tutti):
                print(f"   [{i}/{len(tutti)}]")
    except KeyboardInterrupt:
        print("\nInterrotto.")
    n_r = db.esegui("SELECT COUNT(*) FROM rimborsi")[0][0]
    righe.append(f"Rimborsi nel database: {n_r}")
    classifica = triangola(api, db)
    righe.append("\nCandidati (punteggio = partite rimborsate in cui era avversario, pesate):")
    for c, s, n in classifica[:25]:
        stato = "CONFERMATO" if (n >= 2 and s >= 1.5) else ""
        nome = (db.esegui("SELECT nome FROM giocatori WHERE puuid=?", (c,)) or [[None]])[0][0]
        righe.append(f"  {c[:8]}  {nome or '(nome non salvato)':24s} punteggio {s:.2f}  partite {n}  {stato}")
    if not classifica:
        righe.append("  Nessuno: servono piu' rimborsi. Continua a giocare con Valocheck aperto e rilancia.")
    for r in righe:
        print(r)
    sincronizza(db)
    _salva_rapporto("fase1", righe)
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# FASE 2 - negativi dal tempo
# ════════════════════════════════════════════════════════════════════════════

def fase2():
    print("\n=== FASE 2: chi e' ancora attivo dopo 60-90 giorni? ===\n")
    api = _richiedi_riot()
    db = Database()
    sincronizza(db)
    cfg = P["fase2"]
    limite = time.time() - cfg["giorni_minimi"] * 86400
    da_fare = db.esegui("SELECT puuid, prima_vista FROM giocatori WHERE prima_vista <= ?", (limite,))
    print(f"{len(da_fare)} giocatori visti almeno {cfg['giorni_minimi']} giorni fa.")
    conta = {"attivo": 0, "inattivo": 0, "non disponibile": 0}
    for i, (u, prima) in enumerate(da_fare, 1):
        st = api.storico_partite(u, 5)
        tempi = [h.get("GameStartTime", 0) for h in ((st or {}).get("History") or [])]
        if not st:
            esito, ultima = "non disponibile", None
        else:
            ultima = max(tempi) if tempi else 0
            esito = "attivo" if ultima >= (time.time() - cfg["giorni_attivita"] * 86400) * 1000 else "inattivo"
        conta[esito] += 1
        db.esegui("INSERT OR REPLACE INTO controlli_attivita VALUES(?,?,?,?)", (u, time.time(), ultima, esito))
        print(f"   [{i}/{len(da_fare)}] {esito}")
    righe = [f"Attivi (candidati negativi): {conta['attivo']}",
             f"Inattivi (possibile ban, oppure hanno solo smesso): {conta['inattivo']}",
             f"Non disponibili: {conta['non disponibile']}",
             "",
             "Attenzione: 'attivo' non garantisce 'onesto' (cheater non ancora presi) e",
             "'inattivo' non garantisce 'bannato'. Sono etichette rumorose, da usare con cautela."]
    for r in righe:
        print(r)
    sincronizza(db)
    _salva_rapporto("fase2", righe)
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# ESPORTAZIONE DEL DATASET (base per la Fase 4)
# ════════════════════════════════════════════════════════════════════════════

def esporta():
    db = Database()
    sincronizza(db)
    f = os.path.join(DATI, f"dataset_{datetime.now():%Y%m%d_%H%M}.csv")
    righe = db.esegui("SELECT o.puuid, o.ts, o.prob, o.llr_prestazioni, o.llr_account, o.n_partite, o.dettagli "
                      "FROM osservazioni o JOIN (SELECT puuid, MAX(ts) m FROM osservazioni GROUP BY puuid) u "
                      "ON o.puuid=u.puuid AND o.ts=u.m")
    conf = {r[0] for r in db.esegui("SELECT puuid FROM confermati")}
    att = {r[0]: r[1] for r in db.esegui(
        "SELECT puuid, esito FROM controlli_attivita c WHERE ts=(SELECT MAX(ts) FROM controlli_attivita d WHERE d.puuid=c.puuid)")}
    chiavi_s = list(P["segnali"].keys())
    chiavi_p = ["livello", "stagioni", "partite_atto", "winrate", "rr_medio", "salto_tier", "pausa_giorni", "tier", "tier_max"]
    with open(f, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh, delimiter=";")
        w.writerow(["puuid", "data", "prob", "llr_prestazioni", "llr_account", "n_partite"]
                   + [f"z_{s}" for s in chiavi_s] + chiavi_p
                   + ["etichetta_fase1_confermato", "esito_fase2"])
        for puuid, ts, prob, lp, la, n, det in righe:
            d = json.loads(det or "{}")
            contr = {c["chiave"]: c.get("z_medio", "") for c in d.get("analisi", {}).get("contributi", [])}
            prof = d.get("profilo", {})
            w.writerow([puuid, datetime.fromtimestamp(ts).isoformat(timespec="minutes"),
                        f"{prob:.4f}", lp, la, n] + [contr.get(s, "") for s in chiavi_s]
                       + [prof.get(k, "") for k in chiavi_p]
                       + [1 if puuid in conf else "", att.get(puuid, "")])
    print(f"Esportati {len(righe)} giocatori in:\n{f}")
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# DIAGNOSI: cosa rispondono davvero i server Riot
# ════════════════════════════════════════════════════════════════════════════

def diagnosi():
    print("\n=== DIAGNOSI DEL COLLEGAMENTO A RIOT ===\n")
    righe = []

    def scrivi(t=""):
        print(t)
        righe.append(t)

    contenuti.aggiorna(forza=True)
    api = RiotAPI()
    scrivi(f"Python {sys.version.split()[0]}  |  Valocheck {VERSIONE}")
    api.lockfile = api._leggi_lockfile()
    scrivi(f"Lockfile trovato: {'si' if api.lockfile else 'NO (VALORANT/Riot Client non avviato?)'}")
    if not api.lockfile:
        _salva_rapporto("diagnosi", righe); attendi_invio(); return
    scrivi(f"Token locali: {'ok' if api._aggiorna_token() else 'NO'}")
    sess = api._locale("/chat/v1/session") or {}
    api.puuid = sess.get("puuid")
    api.regione, api.shard = risolvi_regione(sess.get("region", ""))
    scrivi(f"Sessione: puuid {'ok' if api.puuid else 'NO'}, regione chat '{sess.get('region')}' -> {api.regione}/{api.shard}")
    scrivi(f"Versione client: {api.versione_client()}  (fonte: {api.fonte_versione})")
    scrivi(f"Versione da valorant-api.com: {contenuti.versione_client}")
    scrivi(f"Presenza VALORANT: {'ok' if api._mia_presenza() is not None else 'non trovata (gioco chiuso?)'}")
    scrivi("")
    prove = [
        ("MMR", lambda: api.mmr(api.puuid)),
        ("Storico MMR (competitiveupdates)", lambda: api.aggiornamenti_competitivi(api.puuid, 5)),
        ("Storico partite", lambda: api.storico_partite(api.puuid, 5)),
        ("Nomi (name-service)", lambda: api.nomi([api.puuid])),
    ]
    storico = None
    for nome, f in prove:
        api.registro.clear()
        d = f()
        if nome == "Storico partite":
            storico = d
        ultimo = api.registro[-1] if api.registro else ("", "", 0, "nessuna chiamata")
        esito = "OK" if d else "FALLITO"
        scrivi(f"{nome:34s} {esito:8s} status {ultimo[2]}  {ultimo[3][:160]}")
    mid = (((storico or {}).get("History") or [{}])[0]).get("MatchID")
    if mid:
        api.registro.clear()
        f = os.path.join(CACHE_PARTITE, f"{mid}.json.gz")
        if os.path.exists(f):
            os.remove(f)
        d = api.dettagli_partita(mid)
        ultimo = api.registro[-1] if api.registro else ("", "", 0, "")
        scrivi(f"{'Dettagli partita':34s} {'OK' if d else 'FALLITO':8s} status {ultimo[2]}  {ultimo[3][:160]}")
        if d:
            p = estrai_partita(d)
            n_geo = sum(len(a["geometria"]) for a in p["agg"].values()) if p else 0
            scrivi(f"   giocatori {len(p['squadra']) if p else 0}, kill con posizione/mira {n_geo}")
    else:
        scrivi("Dettagli partita: non provati (manca lo storico partite)")
    scrivi("\nNei rapporti non vengono mai salvati token o password.")
    _salva_rapporto("diagnosi", righe)
    attendi_invio()


# ════════════════════════════════════════════════════════════════════════════
# DEMO: lobby simulata (serve a provare l'interfaccia e i calcoli senza gioco)
# ════════════════════════════════════════════════════════════════════════════

AGENTI_DEMO = ["Jett", "Reyna", "Sova", "Omen", "Killjoy", "Raze", "Skye", "Viper", "Cypher", "Phoenix"]


class FintaAPI:
    """Stessa interfaccia di RiotAPI, ma con partite generate al computer.

    Nella lobby ci sono: un cheater evidente, un cheater "discreto", uno
    smurf forte ma onesto con account nuovo, e 7 giocatori normali.
    La mira e' generata con una convenzione d'angolo "strana" (assi scambiati
    + 90 gradi) per verificare che la calibrazione automatica la ritrovi.
    """

    def __init__(self, seme: int = 7):
        self.rng = random.Random(seme)
        self.puuid = "demo-io"
        self.regione, self.shard = "eu", "eu"
        self.partite: Dict[str, Dict] = {}
        self.storici: Dict[str, List[str]] = {}
        self.profili: Dict[str, Dict] = {}
        rng = self.rng
        normale = lambda: {"abilita": max(0.5, rng.gauss(1, 0.2)), "mira": rng.uniform(6, 10),
                           "hs": min(0.32, max(0.1, rng.gauss(0.2, 0.04))), "raffica": 0.03}
        self.pool = {f"pool-{i}": normale() for i in range(150)}
        self.lobby = {
            "demo-io": dict(normale(), nome="Tu#DEMO", team="Blue"),
            "demo-smurf": {"abilita": 1.7, "mira": 5.5, "hs": 0.29, "raffica": 0.05, "nome": "SmurfOnesto#EU", "team": "Blue"},
            "demo-a1": dict(normale(), nome="Compagno1#EU", team="Blue"),
            "demo-a2": dict(normale(), nome="Compagno2#EU", team="Blue"),
            "demo-a3": dict(normale(), nome="", team="Blue", nascosto=True),
            "demo-cheat": {"abilita": 2.1, "mira": 1.6, "hs": 0.45, "raffica": 0.28, "nome": "Evidente#666", "team": "Red"},
            "demo-closet": {"abilita": 1.3, "mira": 3.0, "hs": 0.27, "raffica": 0.10, "nome": "Discreto#777", "team": "Red"},
            "demo-e1": dict(normale(), nome="Avversario1#EU", team="Red"),
            "demo-e2": dict(normale(), nome="Avversario2#EU", team="Red"),
            "demo-e3": dict(normale(), nome="Avversario3#EU", team="Red"),
        }
        for u in self.lobby:
            self.profili[u] = {"livello": rng.randint(60, 400), "atti": rng.randint(4, 15),
                               "partite": rng.randint(30, 90), "wr": rng.uniform(0.44, 0.56), "rr": rng.uniform(-3, 6)}
        self.profili["demo-cheat"] = {"livello": 18, "atti": 1, "partite": 34, "wr": 0.79, "rr": 19}
        self.profili["demo-closet"] = {"livello": 75, "atti": 3, "partite": 40, "wr": 0.62, "rr": 9}
        self.profili["demo-smurf"] = {"livello": 24, "atti": 1, "partite": 25, "wr": 0.72, "rr": 17}
        self._agenti = {}
        uuid_agenti = list(contenuti.agenti.keys()) or AGENTI_DEMO
        for i, u in enumerate(self.lobby):
            self._agenti[u] = uuid_agenti[i % len(uuid_agenti)]
        for u in self.lobby:
            self.storici[u] = [self._nuova_partita(u) for _ in range(6)]

    # -- generatore ---------------------------------------------------------
    def _nuova_partita(self, protagonista) -> str:
        rng = self.rng
        mid = f"demo-{len(self.partite):04d}"
        altri = rng.sample(list(self.pool), 9)
        giocatori = {protagonista: self.lobby[protagonista]}
        for u in altri:
            giocatori[u] = self.pool[u]
        squadre = {u: ("Blue" if i < 5 else "Red") for i, u in enumerate(giocatori)}
        n_round = rng.randint(19, 25)
        vinti = {"Blue": 0, "Red": 0}
        rr = []
        for r in range(n_round):
            vivi = {"Blue": [u for u in giocatori if squadre[u] == "Blue"],
                    "Red": [u for u in giocatori if squadre[u] == "Red"]}
            per = {u: {"subject": u, "kills": [], "damage": []} for u in giocatori}
            t = rng.uniform(8000, 30000)
            ultimo = None
            while vivi["Blue"] and vivi["Red"]:
                cand = vivi["Blue"] + vivi["Red"]
                kil = rng.choices(cand, weights=[giocatori[u]["abilita"] for u in cand])[0]
                if ultimo and ultimo in cand and rng.random() < giocatori[ultimo]["raffica"]:
                    kil = ultimo
                    t += rng.uniform(90, 290)
                else:
                    t += 300 + rng.expovariate(1 / 2500)
                nem = "Red" if squadre[kil] == "Blue" else "Blue"
                vit = rng.choice(vivi[nem])
                x, y = rng.uniform(-6000, 6000), rng.uniform(-6000, 6000)
                b, dist = rng.uniform(-math.pi, math.pi), rng.uniform(600, 3500)
                vx, vy = x + dist * math.cos(b), y + dist * math.sin(b)
                vista = _avvolgi(math.atan2(vx - x, vy - y) + math.pi / 2
                                 + math.radians(rng.gauss(0, giocatori[kil]["mira"])))
                locs = [{"subject": u, "viewRadians": rng.uniform(-3.1, 3.1),
                         "location": {"x": rng.uniform(-6000, 6000), "y": rng.uniform(-6000, 6000)}}
                        for u in cand if u not in (kil, vit)]
                locs.append({"subject": kil, "viewRadians": vista, "location": {"x": x, "y": y}})
                per[kil]["kills"].append({
                    "roundTime": int(t), "gameTime": int(t), "killer": kil, "victim": vit,
                    "victimLocation": {"x": vx, "y": vy}, "playerLocations": locs,
                    "finishingDamage": {"damageType": "Weapon" if rng.random() < 0.9 else "Ability"}})
                colpi = rng.randint(2, 5)
                teste = sum(rng.random() < giocatori[kil]["hs"] for _ in range(colpi))
                per[kil]["damage"].append({"receiver": vit, "damage": 150, "headshots": teste,
                                           "bodyshots": colpi - teste, "legshots": 0})
                for u in cand:
                    if rng.random() < 0.15 and u != vit:
                        bersaglio = rng.choice(vivi["Red" if squadre[u] == "Blue" else "Blue"])
                        per[u]["damage"].append({"receiver": bersaglio, "damage": rng.randint(15, 60),
                                                 "headshots": 0, "bodyshots": 1, "legshots": rng.randint(0, 1)})
                vivi[nem].remove(vit)
                ultimo = kil
            vincitore = "Blue" if vivi["Blue"] else "Red"
            vinti[vincitore] += 1
            rr.append({"roundNum": r, "winningTeam": vincitore, "playerStats": list(per.values())})
        self.partite[mid] = {
            "matchInfo": {"matchId": mid, "mapId": "/Game/Maps/Ascent/Ascent", "queueID": "competitive",
                          "gameStartMillis": int((time.time() - rng.uniform(0, 20) * 86400) * 1000)},
            "players": [{"subject": u, "teamId": squadre[u], "accountLevel": 100,
                         "stats": {"roundsPlayed": n_round}} for u in giocatori],
            "teams": [{"teamId": t, "won": vinti[t] > vinti[o]} for t, o in (("Blue", "Red"), ("Red", "Blue"))],
            "roundResults": rr,
        }
        return mid

    # -- stessa interfaccia di RiotAPI -------------------------------------
    def connetti(self):
        return True

    def client_raggiungibile(self):
        return True

    def rileva_stato(self):
        return "INGAME", "demo-live"

    def partita_pregame(self, mid):
        return None

    def partita_corrente(self, mid):
        return {"MapID": "/Game/Maps/Ascent/Ascent", "Players": [
            {"Subject": u, "TeamID": d["team"], "CharacterID": self._agenti[u],
             "PlayerIdentity": {"AccountLevel": self.profili[u]["livello"], "Incognito": d.get("nascosto", False)}}
            for u, d in self.lobby.items()]}

    def nomi(self, puuids):
        return {u: self.lobby[u]["nome"] for u in puuids if self.lobby.get(u, {}).get("nome")}

    def mmr(self, u):
        pr = self.profili[u]
        atto = contenuti.atto_corrente or "atto-demo"
        stag = {f"vecchio-{i}": {"NumberOfGames": 20, "NumberOfWins": 10, "CompetitiveTier": 15}
                for i in range(pr["atti"] - 1)}
        stag[atto] = {"NumberOfGames": pr["partite"], "NumberOfWins": round(pr["partite"] * pr["wr"]),
                      "CompetitiveTier": 18, "RankedRating": 40}
        return {"QueueSkills": {"competitive": {"SeasonalInfoBySeasonID": stag}}}

    def aggiornamenti_competitivi(self, u, n=20):
        pr = self.profili[u]
        return {"Matches": [{"TierAfterUpdate": 18, "TierBeforeUpdate": 18 - (3 if pr["rr"] > 15 else 0),
                             "RankedRatingEarned": round(self.rng.gauss(pr["rr"], 8))} for _ in range(20)]}

    def storico_partite(self, u, n=20, coda=None):
        ids = self.storici.get(u, [])
        return {"History": [{"MatchID": m, "QueueID": "competitive",
                             "GameStartTime": self.partite[m]["matchInfo"]["gameStartMillis"]} for m in ids]}

    def dettagli_partita(self, mid):
        return self.partite.get(mid)


def esegui_test() -> bool:
    """Prova automatica dei calcoli sulla lobby simulata."""
    print("Genero la lobby simulata...")
    api = FintaAPI()
    an = Analizzatore(api, None, dati_reali=False)
    ris = {}
    for u in api.lobby:
        ris[u] = an._analizza(u, api.profili[u]["livello"])
    cal = an.cal
    print(f"\nCalibrazione angolo: convenzione {cal['convenzione']}, scarto "
          f"{math.degrees(cal['scarto']):.1f}°, R={cal['R']:.2f}, kill {cal['n']}")
    print(f"\n{'giocatore':16s} {'prob':>7s}  {'prest':>6s} {'acc':>6s}   z: " + " ".join(f"{s:>6s}" for s in P["segnali"]))
    for u, a in sorted(ris.items(), key=lambda x: -x[1]["prob"]):
        zs = {c["chiave"]: c.get("z_medio", float("nan")) for c in a["contributi"]}
        print(f"{u:16s} {a['prob'] * 100:6.1f}%  {a['llr_prestazioni']:6.2f} {a['llr_account']:6.2f}   "
              + " ".join(f"{zs[s]:6.2f}" for s in P["segnali"]))
    normali = [ris[u]["prob"] for u in ris if u not in ("demo-cheat", "demo-closet", "demo-smurf")]
    ok = True

    def verifica(cond, testo):
        nonlocal ok
        print(("  OK   " if cond else "  FALLITO ") + testo)
        ok &= bool(cond)

    print("\nVerifiche:")
    verifica(cal["convenzione"] == 1 and abs(math.degrees(_avvolgi(cal["scarto"] - math.pi / 2))) < 3,
             "la calibrazione ritrova la convenzione nascosta (assi scambiati + 90°)")
    verifica(ris["demo-cheat"]["prob"] == max(r["prob"] for r in ris.values()), "il cheater evidente ha la probabilita' piu' alta")
    verifica(ris["demo-cheat"]["prob"] > 0.5, "il cheater evidente supera il 50%")
    verifica(ris["demo-closet"]["prob"] > max(normali), "il cheater discreto sta sopra tutti i normali")
    verifica(max(normali) < 0.15, "nessun giocatore normale supera il 15%")
    verifica(ris["demo-smurf"]["prob"] < ris["demo-cheat"]["prob"], "lo smurf onesto sta sotto il cheater evidente")

    storia = [{"date": f"2026-10-{d:02d}", "match_id": f"m{d}", "last_change": c, "refunded_rr": 0}
              for d, c in zip(range(1, 9), [18, -16, 21, -19, -14, 20, 17, 19])]
    storia[-1]["refunded_rr"] = 35        # = 16 + 19 ? no: -19 -16 -> 35
    ev = interpreta_rimborsi(storia)
    verifica(ev and ["m2", "m4"] in ev[0]["combinazioni_B"], "Fase 1: il rimborso di 35 RR viene ricondotto a 16+19")
    print("\nRISULTATO:", "tutte le verifiche superate" if ok else "ALCUNE VERIFICHE FALLITE")
    return ok


# ════════════════════════════════════════════════════════════════════════════
# AVVIO
# ════════════════════════════════════════════════════════════════════════════

def avvia_dal_vivo(demo: bool = False):
    print("=" * 62)
    print(f"  VALOCHECK {VERSIONE}" + ("  -  MODALITA' DEMO (dati simulati)" if demo else ""))
    print("=" * 62)
    assicura_cartelle()
    print("Aggiorno i dati di gioco (agenti, mappe, versione client)...")
    contenuti.aggiorna()
    db = Database(os.path.join(DATI, "demo.db")) if demo else Database()
    api = FintaAPI() if demo else RiotAPI()
    an = Analizzatore(api, db, dati_reali=not demo)
    mon = Monitor(api, an, db)
    url = avvia_web(mon)
    threading.Thread(target=mon.ciclo, daemon=True).start()
    if not demo:
        def ciclo_condivisione():
            while True:
                sincronizza(db)
                time.sleep(300)
        threading.Thread(target=ciclo_condivisione, daemon=True).start()
    print(f"\nPagina dei risultati: {url}")
    print("Si apre da sola nel browser. Lascia aperta QUESTA finestra mentre giochi.")
    print("Per chiudere Valocheck: chiudi questa finestra (o premi Ctrl+C).\n")
    try:
        webbrowser.open(url)
    except Exception:
        pass
    ultimo = ""
    try:
        while True:
            riga = f"[{datetime.now():%H:%M:%S}] {mon.stato}: {mon.messaggio}"
            if mon.messaggio != ultimo:
                print(riga)
                ultimo = mon.messaggio
            time.sleep(2)
    except KeyboardInterrupt:
        print("\nValocheck chiuso.")


def main(argv: List[str]):
    modo = (argv[1] if len(argv) > 1 else "").lower()
    try:
        if modo == "demo":
            avvia_dal_vivo(demo=True)
        elif modo == "test":
            sys.exit(0 if esegui_test() else 1)
        elif modo == "fase0":
            fase0()
        elif modo == "fase0b":
            fase0b()
        elif modo == "fase1":
            fase1()
        elif modo == "fase2":
            fase2()
        elif modo == "esporta":
            esporta()
        elif modo == "diagnosi":
            diagnosi()
        elif modo == "sincronizza":
            sincronizza_menu()
        else:
            avvia_dal_vivo()
    except SystemExit:
        raise
    except Exception:
        traceback.print_exc()
        print("\nSi e' verificato un errore. Copia il testo qui sopra e mandamelo.")
        attendi_invio()


if __name__ == "__main__":
    main(sys.argv)
