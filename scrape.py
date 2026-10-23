#!/usr/bin/env python3
"""
Verfprijzen ophalen van Verfwinkel.nl.

Draait dagelijks via GitHub Actions. Leest producten.json, haalt elke
productpagina op en schrijft:
  data/prijzen.json   -> laatste prijs per product (dit leest je app)
  data/historie.csv   -> een regel per product/variant per dag

Prijzen op de site staan standaard incl. btw; excl. btw rekenen we zelf uit.

Lokaal een opgeslagen pagina testen:
  python scrape.py --html pagina.html
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = ROOT / "producten.json"
DATA = ROOT / "data"
DEBUG = ROOT / "debug"
PRIJZEN = DATA / "prijzen.json"
HISTORIE = DATA / "historie.csv"

USER_AGENT = "VerfprijzenBot/1.0 (persoonlijke prijscheck voor schildersbedrijf, 1x per dag)"
WACHT_SECONDEN = 8  # beleefd: niet te snel achter elkaar (site geeft anders 429)


# ---------------------------------------------------------------- hulpjes

def nu_nl():
    return dt.datetime.now(ZoneInfo("Europe/Amsterdam")).isoformat(timespec="seconds")


def naar_getal(waarde):
    """'41,70' / '41.70' / 41.7 / '€ 1.041,70' -> 41.7"""
    if waarde is None:
        return None
    if isinstance(waarde, (int, float)):
        return float(waarde)
    s = re.sub(r"[^\d,.\-]", "", str(waarde))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def excl(incl, btw):
    return None if incl is None else round(incl / (1 + btw), 2)


def object_vanaf(tekst, start):
    """Parse het JSON-object dat op positie `start` met '{' begint (accolades tellen)."""
    diepte, in_str, esc = 0, False, False
    for i in range(start, len(tekst)):
        c = tekst[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            diepte += 1
        elif c == "}":
            diepte -= 1
            if diepte == 0:
                try:
                    return json.loads(tekst[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def blok_na(tekst, sleutel):
    """Pak het JSON-object dat direct na "sleutel": begint."""
    m = re.search(r'"%s"\s*:\s*\{' % re.escape(sleutel), tekst)
    return object_vanaf(tekst, m.end() - 1) if m else None


def zoek_sleutel(obj, sleutel):
    if isinstance(obj, dict):
        if sleutel in obj:
            return obj[sleutel]
        for v in obj.values():
            r = zoek_sleutel(v, sleutel)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = zoek_sleutel(v, sleutel)
            if r is not None:
                return r
    return None


def haal(sessie, url, pogingen=4):
    """GET met nieuwe pogingen bij 429/5xx (Retry-After respecteren)."""
    for poging in range(pogingen):
        r = sessie.get(url, timeout=30)
        if r.status_code not in (429, 500, 502, 503, 504) or poging == pogingen - 1:
            r.raise_for_status()
            return r
        wacht = r.headers.get("Retry-After")
        wacht = int(wacht) if wacht and wacht.isdigit() else 30 * (poging + 1)
        print(f"     {r.status_code} op {url}, {wacht}s wachten en opnieuw")
        time.sleep(min(wacht, 120))
    raise RuntimeError("onbereikbaar")


# ---------------------------------------------------------------- robots.txt

class Robots:
    """Simpele robots.txt-check die ook wildcards (*) en $ begrijpt."""

    def __init__(self, sessie, basis):
        self.regels, self.sitemaps = [], []
        try:
            r = sessie.get(basis + "/robots.txt", timeout=20)
            tekst = r.text if r.ok else ""
        except requests.RequestException:
            tekst = ""
        geldt = False
        for regel in tekst.splitlines():
            regel = regel.split("#", 1)[0].strip()
            if ":" not in regel:
                continue
            veld, waarde = [x.strip() for x in regel.split(":", 1)]
            veld = veld.lower()
            if veld == "user-agent":
                geldt = waarde == "*"
            elif veld == "disallow" and geldt and waarde:
                patroon = re.escape(waarde).replace(r"\*", ".*")
                if patroon.endswith(r"\$"):
                    patroon = patroon[:-2] + "$"
                self.regels.append(re.compile(patroon if patroon.startswith(".*") else "^" + patroon))
            elif veld == "sitemap":
                self.sitemaps.append(waarde)

    def mag(self, url):
        p = urlparse(url)
        pad = p.path + ("?" + p.query if p.query else "")
        return not any(r.search(pad) for r in self.regels)


# ---------------------------------------------------------------- uitlezen

def lees_jsonld(soup):
    """Zoek Product / ProductGroup in JSON-LD."""
    producten = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "", strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        stapel = data if isinstance(data, list) else [data]
        while stapel:
            d = stapel.pop()
            if isinstance(d, dict):
                t = d.get("@type")
                t = t if isinstance(t, list) else [t]
                if "Product" in t or "ProductGroup" in t:
                    producten.append(d)
                stapel.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stapel.extend(d)
    if not producten:
        return None

    hoofd = producten[0]
    varianten, prijzen = [], []

    def offer_prijs(offers):
        offers = offers if isinstance(offers, list) else [offers]
        uit = []
        for o in offers:
            if not isinstance(o, dict):
                continue
            p = naar_getal(o.get("price") or o.get("lowPrice"))
            if p is None and isinstance(o.get("priceSpecification"), dict):
                p = naar_getal(o["priceSpecification"].get("price"))
            if p is not None:
                uit.append((o.get("name") or o.get("sku"), p))
        return uit

    for v in hoofd.get("hasVariant") or []:
        for label, p in offer_prijs(v.get("offers")):
            varianten.append({"label": v.get("name") or label or v.get("sku"), "incl": p})
    for label, p in offer_prijs(hoofd.get("offers")):
        prijzen.append(p)
        if label and not hoofd.get("hasVariant"):
            varianten.append({"label": label, "incl": p})

    alles = [v["incl"] for v in varianten] + prijzen
    if not alles:
        return None
    return {
        "naam": hoofd.get("name"),
        "sku": hoofd.get("sku"),
        "ean": hoofd.get("gtin13") or hoofd.get("gtin"),
        "vanaf_incl": min(alles),
        "varianten": varianten if len(varianten) > 1 else [],
        "methode": "json-ld",
    }


def configs_in_pagina(html):
    """Alle Magento/Hyva configurable-configs ({"attributes":..., "optionPrices":...}) op de pagina."""
    gevonden = []
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="text/x-magento-init"):
        try:
            sp = zoek_sleutel(json.loads(tag.string or ""), "spConfig")
        except json.JSONDecodeError:
            continue
        if sp:
            gevonden.append(sp)
    for sleutel in ("spConfig", "jsonConfig"):
        sp = blok_na(html, sleutel)
        if sp:
            gevonden.append(sp)
    # Verfwinkel.nl (Hyva): initConfigurableOptions...(id, {"attributes": ...})
    for m in re.finditer(r'\{"attributes"\s*:', html):
        sp = object_vanaf(html, m.start())
        if sp and "optionPrices" in sp:
            gevonden.append(sp)
    uniek, gezien = [], set()
    for c in gevonden:
        if isinstance(c, dict) and c.get("optionPrices"):
            sleutel = json.dumps(c, sort_keys=True)
            if sleutel not in gezien:
                gezien.add(sleutel)
                uniek.append(c)
    return uniek


def varianten_uit_config(sp):
    labels = {}
    for attr in (sp.get("attributes") or {}).values():
        for optie in attr.get("options", []):
            for pid in optie.get("products", []):
                labels.setdefault(str(pid), []).append(optie.get("label", "").strip())
    varianten = []
    for pid, pr in sp["optionPrices"].items():
        prijs = naar_getal((pr.get("finalPrice") or {}).get("amount"))
        if prijs is None:
            continue
        varianten.append({"label": " / ".join(labels.get(str(pid), [])) or f"variant {pid}", "incl": round(prijs, 2)})
    varianten.sort(key=lambda v: v["incl"])
    return varianten


def lees_magento_varianten(html, vanaf_prijs=None):
    """Prijs per verpakking. Kiest de config van het hoofdproduct, niet die van
    'Maak je klus compleet'-producten verderop op de pagina."""
    configs = configs_in_pagina(html)
    if not configs:
        return []
    m = re.search(r'name="product"\s+value="(\d+)"', html) or re.search(r'value="(\d+)"\s+name="product"', html)
    hoofd_id = m.group(1) if m else None
    if hoofd_id:
        for c in configs:
            if str(c.get("productId")) == hoofd_id:
                return varianten_uit_config(c)
    if vanaf_prijs is not None:
        # De 'vanaf'-prijs van de pagina moet een van de verpakkingsprijzen zijn.
        for c in configs:
            v = varianten_uit_config(c)
            if v and any(abs(x["incl"] - vanaf_prijs) < 0.011 for x in v):
                return v
        return []
    return varianten_uit_config(configs[0]) if len(configs) == 1 else []


def lees_los(soup):
    """Laatste redmiddel: meta-tags of data-price-amount."""
    for sel, attr in [('meta[property="product:price:amount"]', "content"),
                      ('[itemprop="price"]', "content"),
                      ('[data-price-type="finalPrice"]', "data-price-amount"),
                      ("[data-price-amount]", "data-price-amount")]:
        tag = soup.select_one(sel)
        if tag and naar_getal(tag.get(attr)) is not None:
            return naar_getal(tag.get(attr)), sel
    return None, None


def lees_pagina(html):
    soup = BeautifulSoup(html, "html.parser")
    titel = soup.select_one("h1")
    resultaat = lees_jsonld(soup) or {"naam": None, "sku": None, "ean": None,
                                      "vanaf_incl": None, "varianten": [], "methode": None}
    if not resultaat["naam"] and titel:
        resultaat["naam"] = titel.get_text(" ", strip=True)

    varianten = lees_magento_varianten(html, resultaat["vanaf_incl"])
    if varianten:
        resultaat["varianten"] = varianten
        resultaat["vanaf_incl"] = min(v["incl"] for v in varianten)
        resultaat["methode"] = (resultaat["methode"] + "+" if resultaat["methode"] else "") + "magento-varianten"

    if resultaat["vanaf_incl"] is None:
        prijs, sel = lees_los(soup)
        if prijs is not None:
            resultaat["vanaf_incl"], resultaat["methode"] = prijs, f"html ({sel})"

    # Verbruik uit de specificaties, bijv. "Verbruik per liter : 10 Vierkante meter"
    tekst = soup.get_text(" ", strip=True)
    m = re.search(r"Verbruik per liter\s*:?\s*(?:ca\.?\s*)?([\d]+(?:[.,]\d+)?)\s*(?:m2|m²|vierkante)", tekst, re.I)
    resultaat["verbruik_m2_per_liter"] = naar_getal(m.group(1)) if m else None
    for v in resultaat["varianten"]:
        v["liter"] = inhoud_liter(v["label"])
    return resultaat


def inhoud_liter(label):
    """'2,5 liter' -> 2.5, '500 ml' -> 0.5, '1 liter Donkere kleur' -> 1.0, 'Groot' -> None"""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|l\b|ltr|liter)", label or "", re.I)
    if not m:
        return None
    getal = naar_getal(m.group(1))
    return round(getal / 1000, 3) if m.group(2).lower() == "ml" else getal


# ---------------------------------------------------------------- sitemap

def zoek_in_sitemap(sessie, robots, zoekwoorden, bekend):
    if not zoekwoorden:
        return []
    locs, wachtrij, gezien = [], list(robots.sitemaps), 0
    while wachtrij and gezien < 15:
        url = wachtrij.pop(0)
        gezien += 1
        try:
            r = sessie.get(url, timeout=30)
            boom = ElementTree.fromstring(r.content)
        except (requests.RequestException, ElementTree.ParseError):
            continue
        for el in boom.iter():
            if el.tag.endswith("loc") and el.text:
                loc = el.text.strip()
                (wachtrij if loc.endswith(".xml") else locs).append(loc)
        time.sleep(1)

    extra = []
    for woord, aantal in zoekwoorden.items():
        treffers = [u for u in locs
                    if u.endswith(".html") and woord.lower() in urlparse(u).path.rsplit("/", 1)[-1].lower()
                    and u not in bekend and robots.mag(u)]
        for u in sorted(set(treffers))[:aantal]:
            slug = urlparse(u).path.rsplit("/", 1)[-1].removesuffix(".html")
            extra.append({"id": slug, "merk": woord.capitalize(),
                          "naam": slug.replace("-", " ").title(), "url": u, "via_sitemap": True})
    return extra


# ---------------------------------------------------------------- opslaan

def schrijf_historie(records, datum):
    rijen = []
    if HISTORIE.exists():
        with HISTORIE.open(newline="", encoding="utf-8") as f:
            rijen = [r for r in csv.DictReader(f) if r["datum"] != datum]  # vandaag opnieuw schrijven
    for rec in records:
        if rec["status"] != "ok":
            continue
        regels = rec["varianten"] or [{"label": "vanaf", "incl": rec["vanaf_incl"], "excl": rec["vanaf_excl"]}]
        for v in regels:
            rijen.append({"datum": datum, "id": rec["id"], "variant": v["label"],
                          "incl": f'{v["incl"]:.2f}', "excl": f'{v["excl"]:.2f}'})
    with HISTORIE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["datum", "id", "variant", "incl", "excl"])
        w.writeheader()
        w.writerows(rijen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="lees een opgeslagen HTML-pagina in plaats van de site")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    btw = cfg.get("btw", 0.21)

    if args.html:
        r = lees_pagina(Path(args.html).read_text(encoding="utf-8", errors="ignore"))
        r["vanaf_excl"] = excl(r["vanaf_incl"], btw)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r["vanaf_incl"] is not None else 1

    DATA.mkdir(exist_ok=True)
    vorige = {}
    if PRIJZEN.exists():
        vorige = {p["id"]: p for p in json.loads(PRIJZEN.read_text(encoding="utf-8")).get("producten", [])}

    sessie = requests.Session()
    sessie.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"})
    basis = "https://www." + cfg["winkel"]
    robots = Robots(sessie, basis)

    producten = list(cfg["producten"])
    producten += zoek_in_sitemap(sessie, robots, cfg.get("zoek_in_sitemap"), {p["url"] for p in producten})

    records, gelukt = [], 0
    for p in producten:
        rec = {"id": p["id"], "merk": p.get("merk"), "naam": p.get("naam"), "url": p["url"],
               "winkel": cfg["winkel"], "opgehaald": nu_nl()}
        try:
            if not robots.mag(p["url"]):
                raise RuntimeError("robots.txt staat deze pagina niet toe")
            resp = haal(sessie, p["url"])
            info = lees_pagina(resp.text)
            if info["vanaf_incl"] is None:
                DEBUG.mkdir(exist_ok=True)
                (DEBUG / f'{p["id"]}.html').write_text(resp.text, encoding="utf-8")
                raise RuntimeError("geen prijs gevonden op pagina (HTML bewaard in debug/)")
            rec.update({
                "status": "ok",
                "naam": p.get("naam") or info["naam"],
                "pagina_naam": info["naam"],
                "sku": info["sku"], "ean": info["ean"],
                "vanaf_incl": info["vanaf_incl"],
                "vanaf_excl": excl(info["vanaf_incl"], btw),
                "varianten": [{**v, "excl": excl(v["incl"], btw)} for v in info["varianten"]],
                "methode": info["methode"],
                "verbruik_m2_per_liter": p.get("verbruik_m2_per_liter") or info.get("verbruik_m2_per_liter"),
                "laatst_ok": rec["opgehaald"],
            })
            oud = vorige.get(p["id"], {}).get("vanaf_incl")
            rec["verschil_incl"] = None if oud is None else round(rec["vanaf_incl"] - oud, 2)
            gelukt += 1
            print(f'OK   {p["id"]:30} vanaf € {rec["vanaf_incl"]:.2f} ({len(rec["varianten"])} varianten, {info["methode"]})')
        except Exception as e:  # noqa: BLE001 - per product doorgaan
            oud = vorige.get(p["id"])
            if oud and oud.get("vanaf_incl") is not None:
                rec = {**oud, "status": "verouderd", "fout": str(e), "opgehaald": rec["opgehaald"]}
            else:
                rec.update({"status": "fout", "fout": str(e), "vanaf_incl": None,
                            "vanaf_excl": None, "varianten": []})
            print(f'FOUT {p["id"]:30} {e}')
        records.append(rec)
        time.sleep(WACHT_SECONDEN)

    vandaag = dt.datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    PRIJZEN.write_text(json.dumps({
        "bijgewerkt": nu_nl(), "winkel": cfg["winkel"], "btw": btw,
        "prijzen_zijn": "incl. btw zoals op de site (particulier); excl. = incl / (1 + btw)",
        "producten": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    schrijf_historie(records, vandaag)

    print(f"\n{gelukt}/{len(producten)} producten bijgewerkt.")
    return 0 if gelukt else 1


if __name__ == "__main__":
    sys.exit(main())
#!/usr/bin/env python3
"""
Verfprijzen ophalen van Verfwinkel.nl.

Draait dagelijks via GitHub Actions. Leest producten.json, haalt elke
productpagina op en schrijft:
  data/prijzen.json   -> laatste prijs per product (dit leest je app)
  data/historie.csv   -> een regel per product/variant per dag

Prijzen op de site staan standaard incl. btw; excl. btw rekenen we zelf uit.

Lokaal een opgeslagen pagina testen:
  python scrape.py --html pagina.html
"""
import argparse
import csv
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import urlparse
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup

ROOT = Path(__file__).parent
CONFIG = ROOT / "producten.json"
DATA = ROOT / "data"
DEBUG = ROOT / "debug"
PRIJZEN = DATA / "prijzen.json"
HISTORIE = DATA / "historie.csv"

USER_AGENT = "VerfprijzenBot/1.0 (persoonlijke prijscheck voor schildersbedrijf, 1x per dag)"
WACHT_SECONDEN = 3  # beleefd: niet te snel achter elkaar


# ---------------------------------------------------------------- hulpjes

def nu_nl():
    return dt.datetime.now(ZoneInfo("Europe/Amsterdam")).isoformat(timespec="seconds")


def naar_getal(waarde):
    """'41,70' / '41.70' / 41.7 / '€ 1.041,70' -> 41.7"""
    if waarde is None:
        return None
    if isinstance(waarde, (int, float)):
        return float(waarde)
    s = re.sub(r"[^\d,.\-]", "", str(waarde))
    if not s:
        return None
    if "," in s and "." in s:
        s = s.replace(".", "").replace(",", ".") if s.rfind(",") > s.rfind(".") else s.replace(",", "")
    elif "," in s:
        s = s.replace(",", ".")
    try:
        return float(s)
    except ValueError:
        return None


def excl(incl, btw):
    return None if incl is None else round(incl / (1 + btw), 2)


def blok_na(tekst, sleutel):
    """Pak het JSON-object dat direct na "sleutel": begint (accolades tellen)."""
    m = re.search(r'"%s"\s*:\s*\{' % re.escape(sleutel), tekst)
    if not m:
        return None
    start = m.end() - 1
    diepte, in_str, esc = 0, False, False
    for i in range(start, len(tekst)):
        c = tekst[i]
        if in_str:
            if esc:
                esc = False
            elif c == "\\":
                esc = True
            elif c == '"':
                in_str = False
            continue
        if c == '"':
            in_str = True
        elif c == "{":
            diepte += 1
        elif c == "}":
            diepte -= 1
            if diepte == 0:
                try:
                    return json.loads(tekst[start:i + 1])
                except json.JSONDecodeError:
                    return None
    return None


def zoek_sleutel(obj, sleutel):
    if isinstance(obj, dict):
        if sleutel in obj:
            return obj[sleutel]
        for v in obj.values():
            r = zoek_sleutel(v, sleutel)
            if r is not None:
                return r
    elif isinstance(obj, list):
        for v in obj:
            r = zoek_sleutel(v, sleutel)
            if r is not None:
                return r
    return None


# ---------------------------------------------------------------- robots.txt

class Robots:
    """Simpele robots.txt-check die ook wildcards (*) en $ begrijpt."""

    def __init__(self, sessie, basis):
        self.regels, self.sitemaps = [], []
        try:
            r = sessie.get(basis + "/robots.txt", timeout=20)
            tekst = r.text if r.ok else ""
        except requests.RequestException:
            tekst = ""
        geldt = False
        for regel in tekst.splitlines():
            regel = regel.split("#", 1)[0].strip()
            if ":" not in regel:
                continue
            veld, waarde = [x.strip() for x in regel.split(":", 1)]
            veld = veld.lower()
            if veld == "user-agent":
                geldt = waarde == "*"
            elif veld == "disallow" and geldt and waarde:
                patroon = re.escape(waarde).replace(r"\*", ".*")
                if patroon.endswith(r"\$"):
                    patroon = patroon[:-2] + "$"
                self.regels.append(re.compile(patroon if patroon.startswith(".*") else "^" + patroon))
            elif veld == "sitemap":
                self.sitemaps.append(waarde)

    def mag(self, url):
        p = urlparse(url)
        pad = p.path + ("?" + p.query if p.query else "")
        return not any(r.search(pad) for r in self.regels)


# ---------------------------------------------------------------- uitlezen

def lees_jsonld(soup):
    """Zoek Product / ProductGroup in JSON-LD."""
    producten = []
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "", strict=False)
        except (json.JSONDecodeError, TypeError):
            continue
        stapel = data if isinstance(data, list) else [data]
        while stapel:
            d = stapel.pop()
            if isinstance(d, dict):
                t = d.get("@type")
                t = t if isinstance(t, list) else [t]
                if "Product" in t or "ProductGroup" in t:
                    producten.append(d)
                stapel.extend(v for v in d.values() if isinstance(v, (dict, list)))
            elif isinstance(d, list):
                stapel.extend(d)
    if not producten:
        return None

    hoofd = producten[0]
    varianten, prijzen = [], []

    def offer_prijs(offers):
        offers = offers if isinstance(offers, list) else [offers]
        uit = []
        for o in offers:
            if not isinstance(o, dict):
                continue
            p = naar_getal(o.get("price") or o.get("lowPrice"))
            if p is None and isinstance(o.get("priceSpecification"), dict):
                p = naar_getal(o["priceSpecification"].get("price"))
            if p is not None:
                uit.append((o.get("name") or o.get("sku"), p))
        return uit

    for v in hoofd.get("hasVariant") or []:
        for label, p in offer_prijs(v.get("offers")):
            varianten.append({"label": v.get("name") or label or v.get("sku"), "incl": p})
    for label, p in offer_prijs(hoofd.get("offers")):
        prijzen.append(p)
        if label and not hoofd.get("hasVariant"):
            varianten.append({"label": label, "incl": p})

    alles = [v["incl"] for v in varianten] + prijzen
    if not alles:
        return None
    return {
        "naam": hoofd.get("name"),
        "sku": hoofd.get("sku"),
        "ean": hoofd.get("gtin13") or hoofd.get("gtin"),
        "vanaf_incl": min(alles),
        "varianten": varianten if len(varianten) > 1 else [],
        "methode": "json-ld",
    }


def lees_magento_varianten(html):
    """Magento configurable product: spConfig/jsonConfig met prijs per maat."""
    sp = None
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", type="text/x-magento-init"):
        try:
            sp = zoek_sleutel(json.loads(tag.string or ""), "spConfig")
        except json.JSONDecodeError:
            continue
        if sp:
            break
    if not sp:
        sp = blok_na(html, "spConfig") or blok_na(html, "jsonConfig")
    if not sp or "optionPrices" not in sp:
        return []

    labels = {}
    for attr in (sp.get("attributes") or {}).values():
        for optie in attr.get("options", []):
            for pid in optie.get("products", []):
                labels.setdefault(str(pid), []).append(optie.get("label", "").strip())

    varianten = []
    for pid, pr in sp["optionPrices"].items():
        prijs = naar_getal((pr.get("finalPrice") or {}).get("amount"))
        if prijs is None:
            continue
        varianten.append({"label": " / ".join(labels.get(str(pid), [])) or f"variant {pid}", "incl": prijs})
    varianten.sort(key=lambda v: v["incl"])
    return varianten


def lees_los(soup):
    """Laatste redmiddel: meta-tags of data-price-amount."""
    for sel, attr in [('meta[property="product:price:amount"]', "content"),
                      ('[itemprop="price"]', "content"),
                      ('[data-price-type="finalPrice"]', "data-price-amount"),
                      ("[data-price-amount]", "data-price-amount")]:
        tag = soup.select_one(sel)
        if tag and naar_getal(tag.get(attr)) is not None:
            return naar_getal(tag.get(attr)), sel
    return None, None


def lees_pagina(html):
    soup = BeautifulSoup(html, "html.parser")
    titel = soup.select_one("h1")
    resultaat = lees_jsonld(soup) or {"naam": None, "sku": None, "ean": None,
                                      "vanaf_incl": None, "varianten": [], "methode": None}
    if not resultaat["naam"] and titel:
        resultaat["naam"] = titel.get_text(" ", strip=True)

    varianten = lees_magento_varianten(html)
    if varianten:
        resultaat["varianten"] = varianten
        resultaat["vanaf_incl"] = min(v["incl"] for v in varianten)
        resultaat["methode"] = (resultaat["methode"] + "+" if resultaat["methode"] else "") + "magento-varianten"

    if resultaat["vanaf_incl"] is None:
        prijs, sel = lees_los(soup)
        if prijs is not None:
            resultaat["vanaf_incl"], resultaat["methode"] = prijs, f"html ({sel})"

    # Verbruik uit de specificaties, bijv. "Verbruik per liter : 10 Vierkante meter"
    tekst = soup.get_text(" ", strip=True)
    m = re.search(r"Verbruik per liter\s*:?\s*(?:ca\.?\s*)?([\d]+(?:[.,]\d+)?)\s*(?:m2|m²|vierkante)", tekst, re.I)
    resultaat["verbruik_m2_per_liter"] = naar_getal(m.group(1)) if m else None
    for v in resultaat["varianten"]:
        v["liter"] = inhoud_liter(v["label"])
    return resultaat


def inhoud_liter(label):
    """'2,5 liter' -> 2.5, '500 ml' -> 0.5, '1 liter Donkere kleur' -> 1.0, 'Groot' -> None"""
    m = re.search(r"(\d+(?:[.,]\d+)?)\s*(ml|l\b|ltr|liter)", label or "", re.I)
    if not m:
        return None
    getal = naar_getal(m.group(1))
    return round(getal / 1000, 3) if m.group(2).lower() == "ml" else getal


# ---------------------------------------------------------------- sitemap

def zoek_in_sitemap(sessie, robots, zoekwoorden, bekend):
    if not zoekwoorden:
        return []
    locs, wachtrij, gezien = [], list(robots.sitemaps), 0
    while wachtrij and gezien < 15:
        url = wachtrij.pop(0)
        gezien += 1
        try:
            r = sessie.get(url, timeout=30)
            boom = ElementTree.fromstring(r.content)
        except (requests.RequestException, ElementTree.ParseError):
            continue
        for el in boom.iter():
            if el.tag.endswith("loc") and el.text:
                loc = el.text.strip()
                (wachtrij if loc.endswith(".xml") else locs).append(loc)
        time.sleep(1)

    extra = []
    for woord, aantal in zoekwoorden.items():
        treffers = [u for u in locs
                    if u.endswith(".html") and woord.lower() in urlparse(u).path.rsplit("/", 1)[-1].lower()
                    and u not in bekend and robots.mag(u)]
        for u in sorted(set(treffers))[:aantal]:
            slug = urlparse(u).path.rsplit("/", 1)[-1].removesuffix(".html")
            extra.append({"id": slug, "merk": woord.capitalize(),
                          "naam": slug.replace("-", " ").title(), "url": u, "via_sitemap": True})
    return extra


# ---------------------------------------------------------------- opslaan

def schrijf_historie(records, datum):
    rijen = []
    if HISTORIE.exists():
        with HISTORIE.open(newline="", encoding="utf-8") as f:
            rijen = [r for r in csv.DictReader(f) if r["datum"] != datum]  # vandaag opnieuw schrijven
    for rec in records:
        if rec["status"] != "ok":
            continue
        regels = rec["varianten"] or [{"label": "vanaf", "incl": rec["vanaf_incl"], "excl": rec["vanaf_excl"]}]
        for v in regels:
            rijen.append({"datum": datum, "id": rec["id"], "variant": v["label"],
                          "incl": f'{v["incl"]:.2f}', "excl": f'{v["excl"]:.2f}'})
    with HISTORIE.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["datum", "id", "variant", "incl", "excl"])
        w.writeheader()
        w.writerows(rijen)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--html", help="lees een opgeslagen HTML-pagina in plaats van de site")
    args = ap.parse_args()

    cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
    btw = cfg.get("btw", 0.21)

    if args.html:
        r = lees_pagina(Path(args.html).read_text(encoding="utf-8", errors="ignore"))
        r["vanaf_excl"] = excl(r["vanaf_incl"], btw)
        print(json.dumps(r, indent=2, ensure_ascii=False))
        return 0 if r["vanaf_incl"] is not None else 1

    DATA.mkdir(exist_ok=True)
    vorige = {}
    if PRIJZEN.exists():
        vorige = {p["id"]: p for p in json.loads(PRIJZEN.read_text(encoding="utf-8")).get("producten", [])}

    sessie = requests.Session()
    sessie.headers.update({"User-Agent": USER_AGENT, "Accept-Language": "nl-NL,nl;q=0.9"})
    basis = "https://www." + cfg["winkel"]
    robots = Robots(sessie, basis)

    producten = list(cfg["producten"])
    producten += zoek_in_sitemap(sessie, robots, cfg.get("zoek_in_sitemap"), {p["url"] for p in producten})

    records, gelukt = [], 0
    for p in producten:
        rec = {"id": p["id"], "merk": p.get("merk"), "naam": p.get("naam"), "url": p["url"],
               "winkel": cfg["winkel"], "opgehaald": nu_nl()}
        try:
            if not robots.mag(p["url"]):
                raise RuntimeError("robots.txt staat deze pagina niet toe")
            resp = sessie.get(p["url"], timeout=30)
            resp.raise_for_status()
            info = lees_pagina(resp.text)
            if info["vanaf_incl"] is None:
                DEBUG.mkdir(exist_ok=True)
                (DEBUG / f'{p["id"]}.html').write_text(resp.text, encoding="utf-8")
                raise RuntimeError("geen prijs gevonden op pagina (HTML bewaard in debug/)")
            rec.update({
                "status": "ok",
                "naam": p.get("naam") or info["naam"],
                "pagina_naam": info["naam"],
                "sku": info["sku"], "ean": info["ean"],
                "vanaf_incl": info["vanaf_incl"],
                "vanaf_excl": excl(info["vanaf_incl"], btw),
                "varianten": [{**v, "excl": excl(v["incl"], btw)} for v in info["varianten"]],
                "methode": info["methode"],
                "verbruik_m2_per_liter": p.get("verbruik_m2_per_liter") or info.get("verbruik_m2_per_liter"),
                "laatst_ok": rec["opgehaald"],
            })
            oud = vorige.get(p["id"], {}).get("vanaf_incl")
            rec["verschil_incl"] = None if oud is None else round(rec["vanaf_incl"] - oud, 2)
            gelukt += 1
            print(f'OK   {p["id"]:30} vanaf € {rec["vanaf_incl"]:.2f} ({len(rec["varianten"])} varianten, {info["methode"]})')
        except Exception as e:  # noqa: BLE001 - per product doorgaan
            oud = vorige.get(p["id"])
            if oud and oud.get("vanaf_incl") is not None:
                rec = {**oud, "status": "verouderd", "fout": str(e), "opgehaald": rec["opgehaald"]}
            else:
                rec.update({"status": "fout", "fout": str(e), "vanaf_incl": None,
                            "vanaf_excl": None, "varianten": []})
            print(f'FOUT {p["id"]:30} {e}')
        records.append(rec)
        time.sleep(WACHT_SECONDEN)

    vandaag = dt.datetime.now(ZoneInfo("Europe/Amsterdam")).date().isoformat()
    PRIJZEN.write_text(json.dumps({
        "bijgewerkt": nu_nl(), "winkel": cfg["winkel"], "btw": btw,
        "prijzen_zijn": "incl. btw zoals op de site (particulier); excl. = incl / (1 + btw)",
        "producten": records,
    }, indent=2, ensure_ascii=False), encoding="utf-8")
    schrijf_historie(records, vandaag)

    print(f"\n{gelukt}/{len(producten)} producten bijgewerkt.")
    return 0 if gelukt else 1


if __name__ == "__main__":
    sys.exit(main())
