#!/usr/bin/env python3
"""
Kicktipp Auto-Tipper
====================

Loggt sich automatisch bei kicktipp.de ein und gibt Tipps ab –
aber NUR kurz vor Anpfiff (konfigurierbares Zeitfenster), damit du
deine Spielanalysen vorher in Ruhe machen kannst.

Workflow:
  1. `python kicktipp_tipper.py sync`   -> holt die anstehenden Spiele und legt sie
                                           in tips.json an (mit leeren Tipps).
  2. Du trägst nach deiner Analyse die Ergebnisse in tips.json ein (z.B. "2:1").
  3. `python kicktipp_tipper.py run --lead 90m`  -> sendet alle Tipps ab, deren Spiel
                                           in den nächsten 90 Min beginnt. Per Cron alle
                                           paar Minuten ausgeführt = "tippt automatisch
                                           kurz davor".

Konfiguration über Umgebungsvariablen:
  KICKTIPP_EMAIL       deine E-Mail
  KICKTIPP_PASSWORD    dein Passwort
  KICKTIPP_COMMUNITY   Name deiner Tipprunde (steht in der URL: kicktipp.de/<community>/...)

Hinweis: Automatischer Zugriff kann gegen die Nutzungsbedingungen von Kicktipp verstoßen.
Nutze nur deinen eigenen Account und halte die Abfragefrequenz niedrig.
"""

import argparse
import json
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

BASE = "https://www.kicktipp.de"
LOGIN_URL = BASE + "/info/profil/login"

TOKEN_FILE = Path("login_token.txt")
TIPS_FILE = Path("tips.json")

USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

# Datum + Uhrzeit, z.B. "06.06.26 18:00" (Jahr 2- oder 4-stellig)
_DT_RE = re.compile(r"(\d{2})\.(\d{2})\.(\d{2,4}).*?(\d{1,2}):(\d{2})")
# Nur Uhrzeit, falls in der Zeile kein Datum steht (gilt dann das vorige Datum)
_TIME_RE = re.compile(r"(\d{1,2}):(\d{2})")


@dataclass
class Match:
    home: str
    away: str
    kickoff: datetime | None
    heim_field: str | None          # name-Attribut des Heim-Eingabefelds
    gast_field: str | None          # name-Attribut des Gast-Eingabefelds
    current_heim: str = ""          # bereits abgegebener Tipp (Heim)
    current_gast: str = ""          # bereits abgegebener Tipp (Gast)
    odds: str = ""                  # Quoten als Text, z.B. "2.10 / 3.30 / 3.05"

    @property
    def key(self) -> str:
        return f"{self.home} vs {self.away}"

    @property
    def already_placed(self) -> bool:
        return bool(self.current_heim.strip() or self.current_gast.strip())


# --------------------------------------------------------------------------- #
# Login / Session
# --------------------------------------------------------------------------- #
def new_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({"User-Agent": USER_AGENT})
    return s


def login(session: requests.Session, email: str, password: str) -> str:
    """Loggt ein und gibt den login-Cookie (Token) zurück."""
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
    """Stellt sicher, dass die Session eingeloggt ist (Token-Datei oder Login)."""
    if TOKEN_FILE.exists():
        token = TOKEN_FILE.read_text().strip()
        if token:
            session.cookies.set("login", token, domain="www.kicktipp.de")
            return

    email = os.environ.get("KICKTIPP_EMAIL")
    password = os.environ.get("KICKTIPP_PASSWORD")
    if not email or not password:
        sys.exit("Bitte KICKTIPP_EMAIL und KICKTIPP_PASSWORD setzen "
                 "(oder login_token.txt anlegen).")
    token = login(session, email, password)
    TOKEN_FILE.write_text(token)
    print("Login erfolgreich, Token in login_token.txt gespeichert.")


# --------------------------------------------------------------------------- #
# Spiele einlesen
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
        year = int(y)
        if year < 100:
            year += 2000
        return datetime(year, int(mo), int(d), int(h), int(mi))
    t = _TIME_RE.search(text)
    if t and last is not None:
        h, mi = t.groups()
        return last.replace(hour=int(h), minute=int(mi))
    return last


def fetch_matches(session: requests.Session, community: str,
                  matchday: int | None = None) -> list[Match]:
    resp = session.get(tippabgabe_url(community, matchday), timeout=30)
    soup = BeautifulSoup(resp.text, "html.parser")
    content = soup.find(id="kicktipp-content")
    if content is None:
        raise RuntimeError("Tippabgabe-Seite nicht gefunden – Community/Token korrekt?")
    tbody = content.find("tbody")
    if tbody is None:
        return []

    matches: list[Match] = []
    last_dt: datetime | None = None
    for tr in tbody.find_all("tr"):
        tds = tr.find_all("td")
        if len(tds) < 4:
            continue

        kickoff = parse_kickoff(tds[0].get_text(" ", strip=True), last_dt)
        last_dt = kickoff or last_dt

        home = tds[1].get_text(strip=True)
        away = tds[2].get_text(strip=True)
        if not home or not away:
            continue

        heim_in = tds[3].find("input", id=lambda x: x and x.endswith("_heimTipp"))
        gast_in = tds[3].find("input", id=lambda x: x and x.endswith("_gastTipp"))

        odds = ""
        if len(tds) > 4:
            odds = " ".join(tds[4].get_text(" ", strip=True).split())

        matches.append(Match(
            home=home,
            away=away,
            kickoff=kickoff,
            heim_field=heim_in.get("name") if heim_in else None,
            gast_field=gast_in.get("name") if gast_in else None,
            current_heim=(heim_in.get("value", "") if heim_in else ""),
            current_gast=(gast_in.get("value", "") if gast_in else ""),
            odds=odds,
        ))
    return matches


# --------------------------------------------------------------------------- #
# Tipps abgeben
# --------------------------------------------------------------------------- #
def submit_form(session: requests.Session, community: str,
                overrides: dict[str, str], matchday: int | None = None) -> None:
    """Lädt das Formular frisch, setzt die overrides (feldname -> wert) und sendet."""
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
    """'2:1' oder '2-1' -> ('2','1'). Leere Strings -> None."""
    if not value or not value.strip():
        return None
    parts = re.split(r"[:\-]", value.strip())
    if len(parts) != 2:
        return None
    h, g = parts[0].strip(), parts[1].strip()
    if not h.isdigit() or not g.isdigit():
        return None
    return h, g


def odds_based_tip(odds: str) -> tuple[str, str]:
    """Leitet aus den Quoten (Heim / Unentschieden / Gast) einen Tipp ab.

    Niedrigere Quote = wahrscheinlicheres Ergebnis. Je klarer der Favorit
    laut Quoten ist, desto höher fällt der getippte Sieg aus.
    """
    nums = re.findall(r"\d+[.,]\d+", odds)
    if len(nums) < 3:
        return ("1", "1")  # keine Quoten -> sicherer Standard, damit immer getippt wird
    home, draw, away = (float(n.replace(",", ".")) for n in nums[:3])
    if min(home, draw, away) <= 0:
        return ("1", "1")

    # Quoten -> implizite (normierte) Wahrscheinlichkeiten
    inv = [1 / home, 1 / draw, 1 / away]
    total = sum(inv)
    p_home, p_draw, p_away = (x / total for x in inv)

    # Unentschieden ist das wahrscheinlichste Ergebnis -> 1:1
    if p_draw >= p_home and p_draw >= p_away:
        return ("1", "1")

    if p_home >= p_away:
        fav_p, dog_p, fav_home = p_home, p_away, True
    else:
        fav_p, dog_p, fav_home = p_away, p_home, False

    margin = fav_p - dog_p          # wie dominant ist der Favorit?
    if margin < 0.25:
        score = (2, 1)              # leichter Favorit
    elif margin < 0.45:
        score = (2, 0)              # klarer Favorit
    else:
        score = (3, 1)              # haushoher Favorit

    return (str(score[0]), str(score[1])) if fav_home else (str(score[1]), str(score[0]))


# --------------------------------------------------------------------------- #
# tips.json
# --------------------------------------------------------------------------- #
def load_tips() -> dict[str, str]:
    if TIPS_FILE.exists():
        return json.loads(TIPS_FILE.read_text(encoding="utf-8"))
    return {}


def save_tips(tips: dict[str, str]) -> None:
    TIPS_FILE.write_text(json.dumps(tips, indent=2, ensure_ascii=False), encoding="utf-8")


# --------------------------------------------------------------------------- #
# Hilfsfunktionen
# --------------------------------------------------------------------------- #
def parse_duration(text: str) -> timedelta:
    m = re.fullmatch(r"(\d+)([mhd])", text.strip())
    if not m:
        sys.exit(f"Ungültige Dauer '{text}'. Format: 90m, 2h, 1d")
    n, unit = int(m.group(1)), m.group(2)
    return {"m": timedelta(minutes=n),
            "h": timedelta(hours=n),
            "d": timedelta(days=n)}[unit]


def fmt_delta(td: timedelta) -> str:
    secs = int(td.total_seconds())
    if secs < 0:
        return "gestartet"
    h, rem = divmod(secs, 3600)
    m, _ = divmod(rem, 60)
    return f"{h}h {m}m" if h else f"{m}m"


# --------------------------------------------------------------------------- #
# Kommandos
# --------------------------------------------------------------------------- #
def get_community(args) -> str:
    community = args.community or os.environ.get("KICKTIPP_COMMUNITY")
    if not community:
        sys.exit("Bitte --community angeben oder KICKTIPP_COMMUNITY setzen.")
    return community


def cmd_sync(args):
    session = new_session()
    ensure_session(session)
    matches = fetch_matches(session, get_community(args), args.matchday)
    tips = load_tips()
    added = 0
    for mt in matches:
        if mt.key not in tips:
            tips[mt.key] = ""   # leer = noch kein Tipp
            added += 1
    save_tips(tips)
    print(f"{len(matches)} Spiele gefunden, {added} neu in {TIPS_FILE} angelegt.")
    cmd_list(args, session=session, matches=matches)


def cmd_list(args, session=None, matches=None):
    if session is None:
        session = new_session()
        ensure_session(session)
    if matches is None:
        matches = fetch_matches(session, get_community(args), args.matchday)
    tips = load_tips()
    now = datetime.now()
    print(f"\n{'Spiel':40} {'Anpfiff':17} {'in':>8}  {'Tipp':6} {'Status':10} Quoten")
    print("-" * 100)
    for mt in matches:
        ko = mt.kickoff.strftime("%d.%m. %H:%M") if mt.kickoff else "?"
        rest = fmt_delta(mt.kickoff - now) if mt.kickoff else "?"
        tip = tips.get(mt.key, "") or "-"
        status = f"steht {mt.current_heim}:{mt.current_gast}" if mt.already_placed else "offen"
        print(f"{mt.key[:40]:40} {ko:17} {rest:>8}  {tip:6} {status:10} {mt.odds}")


def cmd_run(args):
    session = new_session()
    ensure_session(session)
    community = get_community(args)
    lead = parse_duration(args.lead)
    matches = fetch_matches(session, community, args.matchday)
    tips = load_tips()
    now = datetime.now()

    overrides: dict[str, str] = {}
    planned = []
    for mt in matches:
        if mt.kickoff is None or mt.heim_field is None or mt.gast_field is None:
            continue
        delta = mt.kickoff - now
        # Nur Spiele, die JETZT im Fenster vor Anpfiff liegen ("kurz davor")
        if not (timedelta(0) <= delta <= lead):
            continue
        if mt.already_placed and not args.override:
            continue

        tip = parse_tip(tips.get(mt.key, ""))
        if tip is None and args.auto_odds:
            tip = odds_based_tip(mt.odds)
        if tip is None and getattr(args, "default_tip", ""):
            tip = parse_tip(args.default_tip)
        if tip is None:
            print(f"[!] {mt.key}: Anpfiff in {fmt_delta(delta)}, aber kein Tipp ermittelbar (keine Quoten?).")
            continue

        overrides[mt.heim_field] = tip[0]
        overrides[mt.gast_field] = tip[1]
        planned.append((mt, tip))

    if not planned:
        print("Nichts zu tun (kein Spiel im Zeitfenster oder alle Tipps bereits gesetzt).")
        return

    for mt, tip in planned:
        print(f"-> {mt.key}: tippe {tip[0]}:{tip[1]} (Anpfiff {mt.kickoff:%d.%m. %H:%M})")

    if args.dry_run:
        print("[DRY-RUN] Es wurde nichts gesendet.")
        return

    submit_form(session, community, overrides, args.matchday)
    print(f"{len(planned)} Tipp(s) abgegeben.")


def cmd_watch(args):
    """Dauerläufer als Alternative zu Cron: prüft alle --interval Minuten."""
    interval = max(1, args.interval) * 60
    print(f"Watch-Modus: prüfe alle {args.interval} Min (Fenster {args.lead}). Strg+C zum Beenden.")
    while True:
        try:
            cmd_run(args)
        except Exception as exc:  # nicht abstürzen, nur loggen
            print(f"[Fehler] {exc}")
        time.sleep(interval)


# --------------------------------------------------------------------------- #
def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Kicktipp Auto-Tipper")
    p.add_argument("--community", help="Name der Tipprunde (sonst KICKTIPP_COMMUNITY)")
    p.add_argument("--matchday", type=int, help="Spieltag (1..n), sonst aktueller")
    sub = p.add_subparsers(dest="cmd", required=True)

    sp = sub.add_parser("sync", help="Spiele holen und in tips.json anlegen")
    sp.set_defaults(func=cmd_sync)

    lp = sub.add_parser("list", help="Spiele, Tipps und Status anzeigen")
    lp.set_defaults(func=cmd_list)

    rp = sub.add_parser("run", help="Tipps abgeben, die im Zeitfenster vor Anpfiff liegen")
    rp.add_argument("--lead", default="90m", help="Zeitfenster vor Anpfiff (z.B. 90m, 2h)")
    rp.add_argument("--dry-run", action="store_true", help="nur anzeigen, nichts senden")
    rp.add_argument("--override", action="store_true", help="bereits gesetzte Tipps überschreiben")
    rp.add_argument("--auto-odds", action="store_true",
                    help="fehlende Tipps notfalls aus den Quoten ableiten")
    rp.add_argument("--default-tip", default="",
                    help="Fallback-Tipp, falls keine Quoten vorhanden sind (z.B. 1:1)")
    rp.set_defaults(func=cmd_run)

    wp = sub.add_parser("watch", help="Dauerläufer statt Cron")
    wp.add_argument("--lead", default="90m")
    wp.add_argument("--interval", type=int, default=15, help="Prüfintervall in Minuten")
    wp.add_argument("--dry-run", action="store_true")
    wp.add_argument("--override", action="store_true")
    wp.add_argument("--auto-odds", action="store_true")
    wp.add_argument("--default-tip", default="")
    wp.set_defaults(func=cmd_watch)
    return p


def main():
    args = build_parser().parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
