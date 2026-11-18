import argparse
import collections
import csv
import glob
import gzip
import hashlib
import json
import os
import random
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from urllib.parse import urlsplit

import requests

URL = os.environ["API_URL2"]
BASE = "{0.scheme}://{0.netloc}".format(urlsplit(URL))
HEADERS = {
    "User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130 Safari/537.36",
    "Accept": "application/json",
    "Content-Type": "application/json",
    "platform": "web",
    "Origin": BASE,
    "Referer": BASE + "/",
}
ZONA = {"latitude_ne": 44.0, "longitude_ne": 4.6, "latitude_sw": 27.5, "longitude_sw": -18.5}
N = 20
HILOS = 3
PAUSA_S = (0.2, 0.6)
REINTENTOS = 5
CODIGOS = {"AVAILABLE": 0, "CHARGING": 1, "OUTOFORDER": 2, "INOPERATIVE": 3, "BLOCKED": 4, "RESERVED": 5,
           "UNKNOWN": 6, "PLANNED": 7, "REMOVED": 8}

local = threading.local()
cerrojo = threading.Lock()
motivos = collections.Counter()
limite = [0.0]
MAX_429 = 5


def anotar(motivo):
    with cerrojo:
        motivos[motivo] += 1


def get(n):
    if not hasattr(local, "s"):
        local.s = requests.Session()
    for intento in range(REINTENTOS):
        if time.time() > limite[0]:
            anotar("t")
            return None
        if motivos["429"] >= MAX_429:
            anotar("p")
            return None
        try:
            r = local.s.post(URL, params={"page": n, "per_page": N}, json=ZONA, headers=HEADERS, timeout=(10, 40))
            if r.status_code == 200:
                try:
                    d = r.json()
                    time.sleep(random.uniform(*PAUSA_S))
                    return d
                except ValueError:
                    anotar("html")
            else:
                anotar(str(r.status_code))
        except Exception as e:
            anotar(type(e).__name__[:16])
        time.sleep(max(0.0, min(2 ** intento + random.random(), limite[0] - time.time())))
    return None


def epoch(texto):
    try:
        return int(datetime.fromisoformat(texto.replace("Z", "+00:00")).timestamp())
    except Exception:
        return ""


def filas(datos):
    for x in datos.get("data") or []:
        for e in x.get("evses") or []:
            ref = e.get("evse_id") or e.get("uid") or e.get("physical_reference") or ""
            kw = max([float(c.get("max_electric_power") or 0) for c in e.get("connectors") or []] or [0]) / 1000
            yield ((x["id"], hashlib.sha1(ref.encode()).hexdigest()[:10]),
                   CODIGOS.get(e.get("status"), 9), epoch(e.get("status_updated_at") or ""), round(kw))


def foto(max_paginas, minutos):
    instante = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    limite[0] = time.time() + minutos * 60
    primera = get(1)
    if primera is None:
        return instante, {}, None, 0
    total = primera["pagination"]["total_pages"]
    if max_paginas:
        total = min(total, max_paginas)
    resto = list(range(2, total + 1))
    with ThreadPoolExecutor(HILOS) as ex:
        paginas = [primera] + list(ex.map(get, resto))
    fallos = sum(p is None for p in paginas)
    vistos = {}
    for p in paginas:
        if p is not None:
            for clave, s, u, kw in filas(p):
                vistos.setdefault(clave, (s, u, kw))
    return instante, vistos, total, fallos


def leer_ids(carpeta):
    ids = {}
    for ruta in sorted(glob.glob(os.path.join(carpeta, "*.csv.gz"))):
        with gzip.open(ruta, "rt", newline="") as f:
            for fila in csv.DictReader(f):
                ids[(fila["l"], fila["e"])] = int(fila["i"])
    return ids


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dir", default="d")
    ap.add_argument("--prev", default="p/prev.json.gz")
    ap.add_argument("--max", type=int, default=0)
    ap.add_argument("--min", type=float, default=25)
    args = ap.parse_args()
    t0 = time.time()
    instante, vistos, total, fallos = foto(args.max, args.min)
    if not vistos:
        print(f"{instante}  0 ({time.time() - t0:.0f} s, {fallos}) {dict(motivos)}", flush=True)
        sys.exit(1)

    dir_i, dir_x = os.path.join(args.dir, "i"), os.path.join(args.dir, "x")
    os.makedirs(dir_i, exist_ok=True)
    os.makedirs(dir_x, exist_ok=True)
    nombre = instante.replace(":", "")
    ids = leer_ids(dir_i)
    nuevos = []
    for clave, (s, u, kw) in vistos.items():
        if clave not in ids:
            ids[clave] = len(ids)
            nuevos.append((ids[clave], clave[0], clave[1], kw))
    if nuevos:
        with gzip.open(os.path.join(dir_i, nombre + ".csv.gz"), "wt", newline="") as f:
            w = csv.writer(f)
            w.writerow(["i", "l", "e", "p"])
            w.writerows(nuevos)

    prev = {}
    if os.path.exists(args.prev):
        try:
            with gzip.open(args.prev, "rt") as f:
                prev = {int(k): tuple(v) for k, v in json.load(f).items()}
        except Exception:
            prev = {}
    actual = {ids[c]: (s, u) for c, (s, u, kw) in vistos.items()}
    diaria = instante[11:13] == "00" and instante[14:16] < "30"
    completa = fallos == 0 and not args.max and (not prev or diaria)
    if completa:
        prev = {}
    cambios = sorted((i, s, u) for i, (s, u) in actual.items() if prev.get(i) != (s, u))
    ruta = os.path.join(dir_x, nombre + ("_f" if completa else "_p" if fallos else "") + ".csv.gz")
    with gzip.open(ruta, "wt", newline="") as f:
        w = csv.writer(f)
        w.writerow(["i", "s", "u"])
        w.writerows(cambios)
    prev.update(actual)
    os.makedirs(os.path.dirname(os.path.abspath(args.prev)), exist_ok=True)
    with gzip.open(args.prev, "wt") as f:
        json.dump({str(k): v for k, v in prev.items()}, f)
    print(f"{instante}  {len(actual)} {len(cambios)} {len(nuevos)} ({time.time() - t0:.0f} s, {total}, {fallos}) {dict(motivos)}",
          flush=True)


if __name__ == "__main__":
    main()
