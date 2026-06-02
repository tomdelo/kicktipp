#!/usr/bin/env python3
"""
Kicktipp Auto-Tipper – vollautomatisch mit echten Wettquoten
============================================================

Loggt sich bei kicktipp.de ein, liest die anstehenden Spiele, holt für jedes
Spiel im Zeitfenster vor Anpfiff echte Buchmacher-Quoten von The Odds API,
leitet daraus ein Ergebnis ab und gibt den Tipp ab.

Konfiguration über Umgebungsvariablen (in GitHub: "Secrets"):
  KICKTIPP_EMAIL       deine E-Mail
  KICKTIPP_PASSWORD    dein Passwort
  KICKTIPP_COMMUNITY   Name deiner Tipprunde (URL-Teil, z. B. wm26mv)
  ODDS_API_KEY         kostenloser Key von https://the-odds-api.com

Befehle:
  python kicktipp_tipper.py list                 # alle Spiele + erkannte Quoten/Tipps anzeigen (Test!)
  python kicktipp_tipper.py run --lead 3h        # Tipps im 3-Std-Fenster vor Anpfiff abgeben
  python kicktipp_tipper.py run --lead 3h --dry-run   # nur anzeigen, nichts senden

Hinweis: Automatischer Zugriff kann gegen die Kicktipp-AGB verstoßen. Eigener
Account, eigenes Risiko, niedrige Frequenz.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.kicktipp.de"
LOGIN_URL = BASE + "/info/profil/login"
ODDS_URL = "https://api.the-odds-api.com/v4/sports/soccer_fifa_world_cup/odds"

TOKEN_FILE = Path("login_token.txt")
TIPS_FILE = Path("tips.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_DT_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2,4}).*?(\d{1,2}):(\d{2})")
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")

# Deutsche Kicktipp-Namen -> englische Namen (wie The Odds API sie liefert)
TEAM_DE_TO_EN = {
    "Ägypten": "Egypt", "Algerien": "Algeria", "Argentinien": "Argentina",
    "Australien": "Australia", "Belgien": "Belgium", "Bosnien-Herzegowina": "Bosnia and Herzegovina",
    "Brasilien": "Brazil", "Chile": "Chile", "Costa Rica": "Costa Rica",
    "Curaçao": "Curacao", "Dänemark": "Denmark", "Deutschland": "Germany",
    "Ecuador": "Ecuador", "Elfenbeinküste": "Ivory Coast", "England": "England",
    "Frankreich": "France", "Ghana": "Ghana", "Haiti": "Haiti", "Honduras": "Honduras",
    "Iran": "Iran", "Irak": "Iraq", "Italien": "Italy", "Jamaika": "Jamaica",
    "Japan": "Japan", "Jordanien": "Jordan", "Kamerun": "Cameroon", "Kanada": "Canada",
    "Kap Verde": "Cape Verde", "Katar": "Qatar", "Kolumbien": "Colombia",
    "Kroatien": "Croatia", "Marokko": "Morocco", "Mexiko": "Mexico",
    "Neukaledonien": "New Caledonia", "Neuseeland": "New Zealand", "Niederlande": "Netherlands",
    "Nigeria": "Nigeria", "Norwegen": "Norway", "Österreich": "Austria",
    "Panama": "Panama", "Paraguay": "Paraguay", "Peru": "Peru", "Polen": "Poland",
    "Portugal": "Portugal", "Saudi-Arabien": "Saudi Arabia", "Schottland": "Scotland",
    "Schweden": "Sweden", "Schweiz": "Switzerland", "Senegal": "Senegal",
    "Serbien": "Serbia", "Slowakei": "Slovakia", "Slowenien": "Slovenia",
    "Spanien": "Spain", "Südafrika": "South Africa", "Südkorea": "South Korea",
    "Tschechien": "Czech Republic", "Tunesien": "Tunisia", "Türkei": "Turkey",
    "Ukraine": "Ukraine", "Uruguay": "Uruguay", "USA": "USA",
    "Usbekistan": "Uzbekistan", "Vereinigte Arabische Emirate": "United Arab Emirates",
    "Wales": "Wales",
}

# Vereinheitlichung von Schreibweisen, damit DE- und EN-Quelle denselben Schlüssel ergeben
_ALIASES = {
    "unitedstates": "usa", "unitedstatesofamerica": "usa", "us": "usa",
    "southkorea": "southkorea", "korearepublic": "southkorea", "republicofkorea": "southkorea",
    "turkiye": "turkey",
    "czechia": "czechrepublic",
    "cotedivoire": "ivorycoast",
    "bosniaandherzegovina": "bosnia", "bosniaherzegovina": "bosnia",
    "caboverde": "capeverde",
}


def normalize_team(name: str) -> str:
    name = (name or "").strip()
    en = TEAM_DE_TO_EN.get(name, name)
    key = re.sub(r"[^a-z0-9]", "", en.lower())
    return _ALIASES.get(key, key)


@dataclass
class Match:
    home: str
    away: str
    kickoff: datetime | None
    heim_field: str | None
    gast_field: str | None
    current_heim: str = ""
    current_gast: str = ""

    @property
    def key(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def already_placed(self) -> bool:
        return bool(self.current_heim.strip() or self.current_gast.strip())


# --------------------------------------------------------------------------- #
# Login
# --------------------------------------------------------------------------- #
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def login(session: requests.Session, email: str, password: str) -> str:
    resp = session.get(LOGIN_URL, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    form = soup.find("form")
    if form is None:
        raise RuntimeError("Login-Formular nicht gefunden.")
    data = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if name:
            data[name] = inp.get("value", "") or ""
    data["kennung"] = email
    data["passwort"] = password
    action = urljoin(LOGIN_URL, form.get("action") or LOGIN_URL)
    session.post(action, data=data, timeout=30)
    token = session.cookies.get("login")
    if not token:
        raise RuntimeError("Login fehlgeschlagen – E-Mail/Passwort prüfen.")
    return token


def ensure_session(session: requests.Session) -> None:
    if TOKEN_FILE.exists() and TOKEN_FILE.read_text().strip():
        session.cookies.set("login", TOKEN_FILE.read_text().strip(),
                            domain="www.kicktipp.de")
        return
    email = os.environ.get("KICKTIPP_EMAIL")
    password = os.environ.get("KICKTIPP_PASSWORD")
    if not email or not password:
        sys.exit("Bitte KICKTIPP_EMAIL und KICKTIPP_PASSWORD setzen.")
    token = login(session, email, password)
    try:
        TOKEN_FILE.write_text(token)
    except OSError:
        pass
    print("Login erfolgreich.")


# --------------------------------------------------------------------------- #
# Spiele einlesen (robust gegenüber der aktuellen Seitenstruktur)
# --------------------------------------------------------------------------- #
def tippabgabe_url(community: str, matchday: int | None = None) -> str:
    url = f"{BASE}/{community}/tippabgabe"
    if matchday is not None:
        url += f"?&spieltagIndex={matchday}"
    return url


def parse_kickoff(text: str, last: datetime | None) -> datetime | None:
    m = _DT_RE.search(text)
    if m:
        d, mo, y, h, mi = m.groups()
        year = int(y) + 2000 if int(y) < 100 else int(y)
        return datetime(year, int(mo), int(d), int(h), int(mi))
    t = _TIME_RE.search(text)
    if t and last is not None:
        return last.replace(hour=int(t.group(1)), minute=int(t.group(2)))
    return last


def fetch_matches(session: requests.Session, community: str,
                  matchday: int | None = None) -> list[Match]:
    resp = session.get(tippabgabe_url(community, matchday), timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    heim_inputs = soup.find_all("input", id=lambda x: x and x.endswith("_heimTipp"))
    if not heim_inputs:
        raise RuntimeError(
            "Keine Tippfelder gefunden. Mögliche Gründe: nicht eingeloggt, "
            "falscher Community-Name, oder gerade kein Spieltag zur Tippabgabe offen."
        )

    matches: list[Match] = []
    last_dt: datetime | None = None
    for heim_in in heim_inputs:
        row = heim_in.find_parent("tr")
        if row is None:
            continue
        cells = row.find_all("td")
        texts = [c.get_text(" ", strip=True) for c in cells]

        # Datumszelle als Anker finden (sonst Position 0 annehmen)
        anchor = 0
        for i, txt in enumerate(texts):
            if _DT_RE.search(txt) or _TIME_RE.search(txt):
                anchor = i
                break
        kickoff = parse_kickoff(texts[anchor] if texts else "", last_dt)
        last_dt = kickoff or last_dt

        home = texts[anchor + 1] if len(texts) > anchor + 1 else ""
        away = texts[anchor + 2] if len(texts) > anchor + 2 else ""

        gast_in = row.find("input", id=lambda x: x and x.endswith("_gastTipp"))
        matches.append(Match(
            home=home, away=away, kickoff=kickoff,
            heim_field=heim_in.get("name"),
            gast_field=gast_in.get("name") if gast_in else None,
            current_heim=(heim_in.get("value", "") or ""),
            current_gast=(gast_in.get("value", "") if gast_in else "") or "",
        ))
    return matches


# --------------------------------------------------------------------------- #
# Quoten von The Odds API
# --------------------------------------------------------------------------- #
def fetch_odds(api_key: str) -> dict:
    """Holt alle WM-Quoten (1 API-Abruf). Schlüssel: frozenset der zwei Teams."""
    params = {"regions": "eu", "markets": "h2h",
              "oddsFormat": "decimal", "apiKey": api_key}
    try:
        r = requests.get(ODDS_URL, params=params, timeout=30)
    except requests.RequestException as exc:
        print(f"[Quoten] Netzwerkfehler: {exc}")
        return {}
    if r.status_code != 200:
        print(f"[Quoten] API-Fehler {r.status_code}: {r.text[:200]}")
        return {}
    print(f"[Quoten] {r.headers.get('x-requests-remaining', '?')} Abrufe übrig.")

    lookup: dict = {}
    for ev in r.json():
        h = normalize_team(ev.get("home_team", ""))
        a = normalize_team(ev.get("away_team", ""))
        agg: dict[str, float] = {}
        cnt: dict[str, int] = {}
        for bk in ev.get("bookmakers", []):
            for mk in bk.get("markets", []):
                if mk.get("key") != "h2h":
                    continue
                for oc in mk.get("outcomes", []):
                    price = oc.get("price")
                    if not price:
                        continue
                    nm = oc.get("name", "")
                    k = "draw" if nm.lower() == "draw" else normalize_team(nm)
                    agg[k] = agg.get(k, 0.0) + price
                    cnt[k] = cnt.get(k, 0) + 1
        prices = {k: agg[k] / cnt[k] for k in agg}
        if prices:
            lookup[frozenset((h, a))] = prices
    return lookup


def tip_from_prices(home_odd: float, draw_odd: float, away_odd: float) -> tuple[str, str]:
    inv = [1 / home_odd, 1 / draw_odd, 1 / away_odd]
    total = sum(inv)
    p_home, p_draw, p_away = (x / total for x in inv)
    if p_draw >= p_home and p_draw >= p_away:
        return ("1", "1")
    if p_home >= p_away:
        fav_p, dog_p, home_fav = p_home, p_away, True
    else:
        fav_p, dog_p, home_fav = p_away, p_home, False
    margin = fav_p - dog_p
    if margin < 0.25:
        score = (2, 1)
    elif margin < 0.45:
        score = (2, 0)
    else:
        score = (3, 1)
    return (str(score[0]), str(score[1])) if home_fav else (str(score[1]), str(score[0]))


def odds_tip_for(match: Match, lookup: dict) -> tuple[str, str] | None:
    h, a = normalize_team(match.home), normalize_team(match.away)
    prices = lookup.get(frozenset((h, a)))
    if not prices or h not in prices or a not in prices or "draw" not in prices:
        return None
    return tip_from_prices(prices[h], prices["draw"], prices[a])


# --------------------------------------------------------------------------- #
# Tipps abgeben
# --------------------------------------------------------------------------- #
def submit_form(session: requests.Session, community: str,
                overrides: dict[str, str], matchday: int | None = None) -> None:
    url = tippabgabe_url(community, matchday)
    resp = session.get(url, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    marker = soup.find("input", id=lambda x: x and x.endswith("_heimTipp"))
    form = marker.find_parent("form") if marker else None
    if form is None:
        raise RuntimeError("Tippformular nicht gefunden.")
    data: dict[str, str] = {}
    for inp in form.find_all("input"):
        name = inp.get("name")
        if not name:
            continue
        typ = (inp.get("type") or "text").lower()
        if typ in ("checkbox", "radio") and not inp.has_attr("checked"):
            continue
        data[name] = inp.get("value", "") or ""
    data.update(overrides)
    submit = form.find(["button", "input"], attrs={"type": "submit"})
    if submit and submit.get("name"):
        data[submit["name"]] = submit.get("value", "") or ""
    action = urljoin(url, form.get("action") or url)
    session.post(action, data=data, timeout=30)


def parse_tip(value: str) -> tuple[str, str] | None:
    if not value or not value.strip():
        return None
    parts = re.split(r"[:\-]", value.strip())
    if len(parts) != 2 or not parts[0].strip().isdigit() or not parts[1].strip().isdigit():
        return None
    return parts[0].strip(), parts[1].strip()


def load_tips() -> dict[str, str]:
    if TIPS_FILE.exists():
        try:
            return json.loads(TIPS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            return {}
    return {}


# --------------------------------------------------------------------------- #
def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not m:
        sys.exit(f"Ungültige Dauer '{text}'. Format: 90m, 3h, 1d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n), "h": timedelta(hours=n), "d": timedelta(days=n)}[unit]


def fmt_delta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 0:
        return "gestartet"
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


def get_community(args) -> str:
    community = args.community or os.environ.get("KICKTIPP_COMMUNITY")
    if not community:
        sys.exit("Bitte --community angeben oder KICKTIPP_COMMUNITY setzen.")
    return community


# --------------------------------------------------------------------------- #
# Kommandos
# --------------------------------------------------------------------------- #
def cmd_list(args):
    session = new_session()
    ensure_session(session)
    matches = fetch_matches(session, get_community(args), args.matchday)
    api_key = os.environ.get("ODDS_API_KEY")
    lookup = fetch_odds(api_key) if api_key else {}
    now = datetime.now()
    print(f"\n{len(matches)} Spiele erkannt:\n")
    for mt in matches:
        ko = mt.kickoff.strftime("%d.%m. %H:%M") if mt.kickoff else "?"
        rest = fmt_delta(mt.kickoff - now) if mt.kickoff else "?"
        tip = odds_tip_for(mt, lookup) if lookup else None
        tip_txt = f"{tip[0]}:{tip[1]} (Quote)" if tip else "– keine Quote gefunden"
        placed = f" | steht {mt.current_heim}:{mt.current_gast}" if mt.already_placed else ""
        print(f"  {ko}  in {rest:>8}  {mt.home} - {mt.away:<24} -> {tip_txt}{placed}")


def cmd_run(args):
    session = new_session()
    ensure_session(session)
    community = get_community(args)
    lead = parse_duration(args.lead)
    matches = fetch_matches(session, community, args.matchday)
    now = datetime.now()
    tips = load_tips()

    eligible = []
    for mt in matches:
        if mt.kickoff is None or not mt.heim_field or not mt.gast_field:
            continue
        delta = mt.kickoff - now
        if not (timedelta(0) <= delta <= lead):
            continue
        if mt.already_placed and not args.override:
            continue
        eligible.append(mt)

    if not eligible:
        print(f"Nichts zu tun. {len(matches)} Spiele erkannt, keins im Fenster ({args.lead}).")
        for mt in matches[:6]:
            ko = mt.kickoff.strftime("%d.%m. %H:%M") if mt.kickoff else "?"
            print(f"  {ko}  {mt.home} - {mt.away}")
        return

    api_key = os.environ.get("ODDS_API_KEY")
    lookup = fetch_odds(api_key) if api_key else {}
    if not api_key:
        print("[Quoten] ODDS_API_KEY fehlt – ersatzweise 1:1.")

    overrides: dict[str, str] = {}
    planned = []
    for mt in eligible:
        tip = parse_tip(tips.get(mt.key, ""))
        src = "manuell"
        if tip is None:
            tip = odds_tip_for(mt, lookup)
            src = "Quoten"
        if tip is None:
            tip = ("1", "1")
            src = "Fallback 1:1 (keine Quote)"
        overrides[mt.heim_field] = tip[0]
        overrides[mt.gast_field] = tip[1]
        planned.append((mt, tip, src))

    for mt, tip, src in planned:
        print(f"-> {mt.home} {tip[0]}:{tip[1]} {mt.away}  [{src}]  "
              f"(Anpfiff {mt.kickoff:%d.%m. %H:%M})")

    if args.dry_run:
        print("[DRY-RUN] Es wurde nichts gesendet.")
        return
    submit_form(session, community, overrides, args.matchday)
    print(f"{len(planned)} Tipp(s) abgegeben.")


def cmd_watch(args):
    interval = max(1, args.interval) * 60
    print(f"Watch-Modus: alle {args.interval} Min, Fenster {args.lead}. Strg+C beendet.")
    while True:
        try:
            cmd_run(args)
        except Exception as exc:
            print(f"[Fehler] {exc}")
        time.sleep(interval)


def cmd_debug(args):
    """Diagnose: zeigt, was die Tippabgabe-Seite wirklich liefert."""
    session = new_session()
    ensure_session(session)
    url = tippabgabe_url(get_community(args), args.matchday)
    resp = session.get(url, timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")

    print("Angeforderte URL:", url)
    print("Endgültige URL:  ", resp.url)
    print("Status:", resp.status_code, "| Seitenlänge:", len(resp.text))
    title = soup.find("title")
    print("Seitentitel:", title.get_text(strip=True) if title else "-")

    forms = soup.find_all("form")
    print(f"Formulare: {len(forms)} | actions:", [f.get("action") for f in forms][:5])

    inputs = soup.find_all("input")
    print(f"Input-Felder gesamt: {len(inputs)}")
    interesting = [(i.get("name"), i.get("id"), i.get("type")) for i in inputs
                   if (i.get("name") and "tipp" in i.get("name").lower())
                   or (i.get("id") and "tipp" in i.get("id").lower())]
    print(f"Felder mit 'tipp' im Namen/ID: {len(interesting)}")
    for name, iid, typ in interesting[:20]:
        print(f"   name={name!r}  id={iid!r}  type={typ!r}")

    low = resp.text.lower()
    if "kennung" in low or "passwort" in low:
        print("WARNUNG: Die Seite enthält ein Login-Formular -> vermutlich nicht "
              "eingeloggt oder kein Zugriff auf diese Community.")

    ths = [th.get_text(" ", strip=True) for th in soup.find_all("th")]
    if ths:
        print("Tabellenköpfe:", ths[:12])


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kicktipp Auto-Tipper mit echten Quoten")
    p.add_argument("--community", help="Tipprunde (sonst KICKTIPP_COMMUNITY)")
    p.add_argument("--matchday", type=int, help="Spieltag-Index (sonst aktueller)")
    sub = p.add_subparsers(dest="cmd", required=True)

    lp = sub.add_parser("list", help="Spiele + erkannte Quoten/Tipps anzeigen")
    lp.set_defaults(func=cmd_list)

    dp = sub.add_parser("debug", help="Diagnose der Tippabgabe-Seite")
    dp.set_defaults(func=cmd_debug)

    rp = sub.add_parser("run", help="Tipps im Fenster vor Anpfiff abgeben")
    rp.add_argument("--lead", default="3h", help="Zeitfenster vor Anpfiff (z. B. 90m, 3h)")
    rp.add_argument("--dry-run", action="store_true")
    rp.add_argument("--override", action="store_true", help="bestehende Tipps überschreiben")
    rp.set_defaults(func=cmd_run)

    wp = sub.add_parser("watch", help="Dauerläufer statt Cron")
    wp.add_argument("--lead", default="3h")
    wp.add_argument("--interval", type=int, default=15)
    wp.add_argument("--dry-run", action="store_true")
    wp.add_argument("--override", action="store_true")
    wp.set_defaults(func=cmd_watch)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
