import argparse
import collections
import csv
import gzip
import io
import json
import os
import smtplib
import time
import traceback
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from statistics import median
from zoneinfo import ZoneInfo

REPO = os.environ.get("GITHUB_REPOSITORY", "")
TOKEN = os.environ.get("GH_TOKEN", "")
TZ = ZoneInfo("Europe/Madrid")
PASO = 1800
N = 48
MARGEN = 20 * 60
MAX_EDAD = 50 * 60
MIN_FILAS = 0.9
MAX_BAJAS = 100
MIN_PUNTOS = 0.98
NOMBRES = (os.environ.get("NOMBRES") or "datos,datos2").split(",")
E1 = {"0": "libre", "1": "fuera", "2": "llena", "3": "desconocido"}
E2 = {0: "libre", 1: "cargando", 2: "averiado", 3: "inoperativo", 6: "desconocido"}


def num(x, d=0):
    return f"{x:,.{d}f}".replace(",", "X").replace(".", ",").replace("X", ".")


def n_(n, uno, varios):
    return f"{num(n)} {uno if n == 1 else varios}"


def hora(s, dia=False):
    return datetime.fromtimestamp(s, TZ).strftime("%d/%m %H:%M" if dia else "%H:%M")


def tramos(slots):
    grupos = []
    for s in sorted(slots):
        if grupos and s == grupos[-1][1] + PASO:
            grupos[-1][1] = s
        else:
            grupos.append([s, s])
    return ", ".join(hora(a, True) if a == b else f"{hora(a, True)}-{hora(b)} ({(b - a) // PASO + 1})"
                     for a, b in grupos)


def api(ruta):
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + ruta,
                                 headers={"Authorization": "Bearer " + TOKEN, "Accept": "application/vnd.github+json"})
    for intento in range(4):
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                return json.load(r)
        except Exception:
            if intento == 3:
                raise
            time.sleep(2 ** intento)


def bajar(sha, ruta):
    url = f"https://raw.githubusercontent.com/{REPO}/{sha}/{ruta}"
    for intento in range(4):
        try:
            with urllib.request.urlopen(url, timeout=60) as r:
                return list(csv.DictReader(io.StringIO(gzip.decompress(r.read()).decode())))
        except Exception:
            if intento == 3:
                raise
            time.sleep(2 ** intento)


def fotos(rama, carpeta):
    b = api(f"/branches/{rama}")
    arbol = api(f"/git/trees/{b['commit']['commit']['tree']['sha']}?recursive=1")
    out = []
    for x in arbol["tree"]:
        p = x["path"]
        if x["type"] == "blob" and p.startswith(carpeta + "/") and p.endswith(".csv.gz"):
            base = os.path.basename(p)[:-7]
            t = datetime.strptime(base[:18], "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc)
            out.append((int(t.timestamp()), base[18:], p))
    return b["commit"]["sha"], sorted(out)


def todas(sha, rutas):
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(lambda p: bajar(sha, p), rutas))


def cadena(wf, desde, nombre, problemas):
    q = urllib.parse.quote(">=" + datetime.fromtimestamp(desde, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    runs = api(f"/actions/workflows/{wf}/runs?per_page=100&created={q}")["workflow_runs"]
    vivas = [r for r in runs if r["event"] == "workflow_dispatch" and r["status"] != "completed"]
    malas = [r for r in runs if r["conclusion"] in ("failure", "timed_out", "startup_failure")]
    if not vivas:
        problemas.append(f"{nombre}: la cadena está PARADA (ninguna ejecución en curso ni en espera)")
    if malas:
        tipos = dict(collections.Counter(r["conclusion"] for r in malas))
        problemas.append(f"{nombre}: {n_(len(malas), 'ejecución fallida', 'ejecuciones fallidas')} en 24 h {tipos}: {malas[0]['html_url']}")
    return f"en marcha ({n_(len(vivas), 'ejecución', 'ejecuciones')} en curso o en espera)" if vivas else "PARADA", len(malas)


def ventana(ahora, primera):
    fin = (ahora - MARGEN) // PASO * PASO
    inicio = max(fin - (N - 1) * PASO, primera)
    return inicio, fin, list(range(inicio, fin + 1, PASO))


def bot1(ahora, problemas):
    nombre = NOMBRES[0]
    sha, fs = fotos("datos", "estado")
    por_slot = {t // PASO * PASO: (t, p) for t, m, p in fs}
    inicio, fin, slots = ventana(ahora, min(por_slot))
    usar = [s for s in sorted(por_slot) if inicio - PASO <= s <= fin]
    datos = dict(zip(usar, todas(sha, [por_slot[s][1] for s in usar])))
    filas, cambios, prev = {}, {}, None
    for s in usar:
        filas[s] = {(r["site_id"], r["potencia"]): r["estado"] for r in datos[s]}
        if prev is not None and s - prev == PASO:
            comunes = filas[s].keys() & filas[prev].keys()
            cambios[s] = sum(filas[s][k] != filas[prev][k] for k in comunes) / max(len(comunes), 1)
        prev = s
    t_ultima = max(t for t, p in por_slot.values())
    faltan = [s for s in slots if s not in por_slot]
    n = {s: len(filas[s]) for s in slots if s in filas}
    med = median(n.values()) if n else 0
    incompletas = [s for s in n if n[s] < MIN_FILAS * med]
    congeladas = [s for s, c in cambios.items() if c == 0 and s >= inicio]
    if ahora - t_ultima > MAX_EDAD:
        problemas.append(f"{nombre}: no hay fotos desde las {hora(t_ultima, True)}")
    if faltan:
        problemas.append(f"{nombre}: {'falta' if len(faltan) == 1 else 'faltan'} {n_(len(faltan), 'foto', 'fotos')}: {tramos(faltan)}")
    if incompletas:
        problemas.append(f"{nombre}: {n_(len(incompletas), 'foto', 'fotos')} con menos del {MIN_FILAS:.0%} de las estaciones "
                         f"habituales: {tramos(incompletas)}")
    if congeladas:
        problemas.append(f"{nombre}: {n_(len(congeladas), 'foto', 'fotos')} sin ningún cambio de estado (¿datos congelados?): "
                         f"{tramos(congeladas)}")
    estado, malas = cadena("ci.yml", ahora - 86400, nombre, problemas)
    ult = filas[max(n)] if n else {}
    c = collections.Counter(ult.values())
    reparto = " · ".join(f"{v} {num(100 * c.get(k, 0) / max(len(ult), 1), 1)} %" for k, v in E1.items())
    cv = [100 * cambios[s] for s in n if s in cambios]
    return "\n".join([
        f"{nombre.upper()} (rama datos)",
        f"  Fotos: {len(n)}/{len(slots)} desde las {hora(inicio, True)} · última {hora(t_ultima, True)}",
        f"  Cadena: {estado} · fallidas en 24 h: {malas}",
        f"  Estaciones por foto: {num(n[max(n)]) if n else '-'} en la última (mediana {num(med)}, "
        f"mínimo {num(min(n.values())) if n else '-'})",
        f"  Cambian de estado entre fotos seguidas: mediana {num(median(cv), 1) if cv else '-'} %",
        f"  Última foto: {reparto}",
    ])


def bot2(ahora, problemas):
    nombre = NOMBRES[1] if len(NOMBRES) > 1 else "datos2"
    sha, fs = fotos("datos2", "x")
    inicio, fin, slots = ventana(ahora, fs[0][0] // PASO * PASO)
    completas = [t for t, m, p in fs if m == "_f" and t // PASO * PASO <= inicio]
    desde = max(completas) if completas else fs[0][0]
    usar = [(t, m, p) for t, m, p in fs if t >= desde]
    estado, serie = {}, {}
    for k, ((t, m, p), filas) in enumerate(zip(usar, todas(sha, [p for t, m, p in usar]))):
        antes = set(estado)
        if m == "_f":
            estado = {}
        for r in filas:
            i, c = int(r["i"]), int(r["s"])
            if c < 0:
                estado.pop(i, None)
            else:
                estado[i] = c
        serie[t // PASO * PASO] = {"t": t, "m": m, "n": len(estado), "filas": len(filas),
                                   "altas": len(estado.keys() - antes) if k else 0,
                                   "bajas": len(antes - estado.keys()) if k else 0}
    v = {s: serie[s] for s in slots if s in serie}
    t_ultima = max(x["t"] for x in serie.values())
    faltan = [s for s in slots if s not in serie]
    parciales = [s for s in v if v[s]["m"] == "_p"]
    congeladas = [s for s in v if v[s]["m"] == "" and v[s]["filas"] == 0]
    bajas = {s: v[s]["bajas"] for s in v if v[s]["bajas"] > MAX_BAJAS}
    ult_f = max([t for t, m, p in fs if m == "_f"], default=0)
    n0, n1 = (v[min(v)]["n"], v[max(v)]["n"]) if v else (0, 0)
    if ahora - t_ultima > MAX_EDAD:
        problemas.append(f"{nombre}: no hay fotos desde las {hora(t_ultima, True)}")
    if faltan:
        problemas.append(f"{nombre}: {'falta' if len(faltan) == 1 else 'faltan'} {n_(len(faltan), 'foto', 'fotos')}: {tramos(faltan)}")
    if parciales:
        problemas.append(f"{nombre}: {n_(len(parciales), 'foto parcial', 'fotos parciales')} (páginas fallidas): {tramos(parciales)}")
    if congeladas:
        problemas.append(f"{nombre}: {n_(len(congeladas), 'foto', 'fotos')} sin ningún cambio (¿datos congelados?): {tramos(congeladas)}")
    for s, b in bajas.items():
        problemas.append(f"{nombre}: {num(b)} puntos desaparecen en la foto de las {hora(s, True)}")
    if n1 < MIN_PUNTOS * n0:
        problemas.append(f"{nombre}: los puntos bajan de {num(n0)} a {num(n1)} en 24 h")
    if ahora - ult_f > 26 * 3600:
        problemas.append(f"{nombre}: no hay foto completa desde las {hora(ult_f, True)} (debería haber una cada día a las 00:00 UTC)")
    est, malas = cadena("ci2.yml", ahora - 86400, nombre, problemas)
    c = collections.Counter(estado.values())
    otros = sum(x for k, x in c.items() if k not in E2)
    reparto = " · ".join([f"{e} {num(100 * c.get(k, 0) / max(len(estado), 1), 1)} %" for k, e in E2.items()] +
                         [f"otros {num(100 * otros / max(len(estado), 1), 1)} %"])
    return "\n".join([
        f"{nombre.upper()} (rama datos2)",
        f"  Fotos: {len(v)}/{len(slots)} desde las {hora(inicio, True)} · parciales: {len(parciales)} · "
        f"última {hora(t_ultima, True)}",
        f"  Cadena: {est} · fallidas en 24 h: {malas}",
        f"  Puntos: {num(n1)} (al principio de la ventana {num(n0)}) · altas {num(sum(x['altas'] for x in v.values()))}"
        f" · bajas {num(sum(x['bajas'] for x in v.values()))}",
        f"  Cambios por foto: mediana {num(median([x['filas'] for x in v.values() if x['m'] != '_f'] or [0]))}",
        f"  Última foto: {reparto}",
        f"  Última foto completa: {hora(ult_f, True)}",
    ])


def informe():
    ahora = int(time.time())
    problemas = []
    bloques = []
    for f in (bot1, bot2):
        try:
            bloques.append(f(ahora, problemas))
        except Exception:
            problemas.append(f"no se pudo revisar {f.__name__}: {traceback.format_exc(limit=1).strip().splitlines()[-1]}")
    hoy = datetime.fromtimestamp(ahora, TZ)
    asunto = (f"AVISO ({len(problemas)})" if problemas else "OK") + f" · informe del {hoy:%d/%m}"
    cabecera = [f"Informe del {hoy:%d/%m/%Y} a las {hoy:%H:%M} (hora peninsular). Ventana: últimas 24 h.", ""]
    if problemas:
        cabecera += ["PROBLEMAS"] + [f"- {p}" for p in problemas]
    else:
        cabecera += ["Todo bien: las dos cadenas están en marcha, no falta ninguna foto, no hay fotos parciales "
                     "ni ejecuciones fallidas."]
    pie = [f"Tamaño del repo: {num(api('')['size'] / 1024)} MB", f"https://github.com/{REPO}/actions"]
    return asunto, "\n".join(cabecera + [""] + ["\n\n".join(bloques)] + [""] + pie) + "\n"


def enviar(asunto, cuerpo):
    usuario, clave = os.environ["MAIL_USER"], os.environ["MAIL_PASS"].replace(" ", "")
    m = EmailMessage()
    m["Subject"] = asunto
    m["From"] = usuario
    m["To"] = os.environ.get("MAIL_TO") or usuario
    m.set_content(cuerpo)
    for intento in range(3):
        try:
            with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
                s.login(usuario, clave)
                s.send_message(m)
            return
        except smtplib.SMTPAuthenticationError:
            raise
        except Exception:
            if intento == 2:
                raise
            time.sleep(30)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--dry", action="store_true")
    a = ap.parse_args()
    if not a.force:
        ahora = datetime.now(TZ)
        nueve = ahora.replace(hour=9, minute=0, second=0, microsecond=0)
        if ahora < nueve - timedelta(minutes=30):
            print("-")
            return
        if ahora < nueve:
            time.sleep((nueve - ahora).total_seconds())
    try:
        asunto, cuerpo = informe()
    except Exception:
        asunto, cuerpo = "AVISO · el informe diario ha fallado", traceback.format_exc()
    if a.dry:
        print(asunto + "\n\n" + cuerpo)
        return
    enviar(asunto, cuerpo)
    if os.environ.get("GITHUB_OUTPUT"):
        with open(os.environ["GITHUB_OUTPUT"], "a") as f:
            f.write("ok=1\n")
    print("ok")


if __name__ == "__main__":
    main()
