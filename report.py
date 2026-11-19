import argparse
import collections
import csv
import gzip
import hashlib
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
MAX_SEG = 20 * 60
MIN_FILAS = 0.9
MAX_BAJAS = 100
MAX_VARIACION = 0.02
ALERTA = 2 * 3600
REPITE = 6 * 3600
NOMBRES = (os.environ.get("NOMBRES") or "datos,datos2").split(",")
RAMAS = {"datos": (0, "ci.yml"), "datos2": (1, "ci2.yml")}
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


def nuevo(lista, corto, detalle=""):
    lista.append((corto, detalle))


def linea(corto, detalle):
    return f"- {corto}" + (f": {detalle}" if detalle else "")


def nombre_de(rama):
    k = RAMAS[rama][0]
    return NOMBRES[k] if len(NOMBRES) > k else rama


def api(ruta, datos=None):
    req = urllib.request.Request("https://api.github.com/repos/" + REPO + ruta,
                                 data=None if datos is None else json.dumps(datos).encode(),
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
                datos = r.read()
            if ruta.endswith(".json"):
                return json.loads(datos)
            return list(csv.DictReader(io.StringIO(gzip.decompress(datos).decode())))
        except Exception:
            if intento == 3:
                raise
            time.sleep(2 ** intento)


def todas(sha, rutas):
    with ThreadPoolExecutor(8) as ex:
        return list(ex.map(lambda p: bajar(sha, p), rutas))


def instante(ruta):
    base = os.path.basename(ruta)[:18]
    return int(datetime.strptime(base, "%Y-%m-%dT%H%M%SZ").replace(tzinfo=timezone.utc).timestamp())


def arbol(rama):
    b = api(f"/branches/{rama}")
    a = api(f"/git/trees/{b['commit']['commit']['tree']['sha']}?recursive=1")
    if a.get("truncated"):
        raise RuntimeError(f"la lista de ficheros de {rama} viene truncada")
    return b["commit"]["sha"], [x["path"] for x in a["tree"] if x["type"] == "blob"]


def fotos(rutas, carpeta):
    return sorted((instante(p), os.path.basename(p)[18:-7], p) for p in rutas
                  if p.startswith(carpeta + "/") and p.endswith(".csv.gz"))


def controles(sha, rutas, desde):
    m = [p for p in rutas if p.startswith("m/") and p.endswith(".json") and instante(p) >= desde]
    return dict(zip(map(instante, m), todas(sha, m)))


def control(nombre, ctl, fotos_v, fallos):
    """fotos_v: {media hora: instante de la foto}. Devuelve {media hora: control} y una linea de resumen."""
    c = {s: ctl[t] for s, t in fotos_v.items() if t in ctl}
    if ctl:
        sin = [s for s, t in fotos_v.items() if t >= min(ctl) and t not in ctl]
        if sin:
            nuevo(fallos, f"{nombre}: {n_(len(sin), 'foto', 'fotos')} sin fichero de control", tramos(sin))
    lentas = [s for s, d in c.items() if d.get("seg", 0) > MAX_SEG]
    if lentas:
        nuevo(fallos, f"{nombre}: {n_(len(lentas), 'foto tardó', 'fotos tardaron')} más de {MAX_SEG // 60} min "
                      f"(a los 25 se corta)", tramos(lentas))
    seg = [d.get("seg", 0) / 60 for d in c.values()]
    if not seg:
        return c, "  Duración de cada foto: aún no hay ficheros de control"
    return c, f"  Duración de cada foto: mediana {num(median(seg), 1)} min, máxima {num(max(seg), 1)} min"


def cadena(wf, desde, nombre, fallos):
    q = urllib.parse.quote(">=" + datetime.fromtimestamp(desde, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"))
    runs = api(f"/actions/workflows/{wf}/runs?per_page=100&created={q}")["workflow_runs"]
    vivas = [r for r in runs if r["event"] == "workflow_dispatch" and r["status"] != "completed"]
    malas = [r for r in runs if r["conclusion"] in ("failure", "timed_out", "startup_failure")]
    if not vivas:
        nuevo(fallos, f"{nombre}: la cadena está PARADA", "ninguna ejecución en curso ni en espera")
    if malas:
        tipos = ", ".join(f"{k} ×{x}" for k, x in collections.Counter(r["conclusion"] for r in malas).items())
        nuevo(fallos, f"{nombre}: {n_(len(malas), 'ejecución fallida', 'ejecuciones fallidas')} en 24 h",
              f"{tipos}; la primera: {malas[0]['html_url']}")
    return f"en marcha ({n_(len(vivas), 'ejecución', 'ejecuciones')} en curso o en espera)" if vivas else "PARADA", len(malas)


def ventana(ahora, primera, ultima):
    fin = max((ahora - MARGEN) // PASO * PASO, ultima)
    inicio = max(fin - (N - 1) * PASO, primera)
    return inicio, fin, list(range(inicio, fin + 1, PASO))


def variacion(nombre, que, n0, n1, novedades):
    if n0 and abs(n1 - n0) > MAX_VARIACION * n0:
        nuevo(novedades, f"{nombre}: {que} {'suben' if n1 > n0 else 'bajan'} de {num(n0)} a {num(n1)} en 24 h "
                         f"({'+' if n1 > n0 else ''}{num(100 * (n1 - n0) / n0, 1)} %)")


def faltan_fotos(nombre, faltan, fallos):
    if faltan:
        nuevo(fallos, f"{nombre}: {'falta' if len(faltan) == 1 else 'faltan'} {n_(len(faltan), 'foto', 'fotos')}",
              tramos(faltan))


def bot1(ahora, fallos, novedades):
    nombre = nombre_de("datos")
    sha, rutas = arbol("datos")
    fs = fotos(rutas, "estado")
    por_slot = {t // PASO * PASO: (t, p) for t, m, p in fs}
    inicio, fin, slots = ventana(ahora, min(por_slot), max(por_slot))
    usar = [s for s in sorted(por_slot) if inicio - PASO <= s <= fin]
    datos = dict(zip(usar, todas(sha, [por_slot[s][1] for s in usar])))
    filas, cambios, prev = {}, {}, None
    for s in usar:
        filas[s] = {(r["site_id"], r["potencia"]): r["estado"] for r in datos[s]}
        if prev is not None and s - prev == PASO:
            comunes = filas[s].keys() & filas[prev].keys()
            cambios[s] = sum(filas[s][k] != filas[prev][k] for k in comunes)
        prev = s
    t_ultima = max(t for t, p in por_slot.values())
    faltan = [s for s in slots if s not in por_slot]
    n = {s: len(filas[s]) for s in slots if s in filas}
    med = median(n.values()) if n else 0
    incompletas = [s for s in n if n[s] < MIN_FILAS * med]
    congeladas = [s for s, c in cambios.items() if c == 0 and s >= inicio]
    if ahora - t_ultima > MAX_EDAD:
        nuevo(fallos, f"{nombre}: no hay fotos desde las {hora(t_ultima, True)}")
    faltan_fotos(nombre, faltan, fallos)
    if incompletas:
        nuevo(fallos, f"{nombre}: {n_(len(incompletas), 'foto', 'fotos')} con menos del {MIN_FILAS:.0%} de las "
                      f"estaciones habituales", tramos(incompletas))
    if congeladas:
        nuevo(fallos, f"{nombre}: {n_(len(congeladas), 'foto', 'fotos')} sin ningún cambio de estado "
                      f"(¿datos congelados?)", tramos(congeladas))
    ctl, linea_ctl = control(nombre, controles(sha, rutas, inicio), {s: por_slot[s][0] for s in n}, fallos)
    zonas = [s for s, d in ctl.items() if d.get("fallos")]
    if zonas:
        nuevo(fallos, f"{nombre}: {n_(len(zonas), 'foto', 'fotos')} con zonas del mapa sin descargar "
                      f"(les faltan estaciones)", tramos(zonas))
    if n:
        variacion(nombre, "las estaciones", n[min(n)], n[max(n)], novedades)
    estado, malas = cadena("ci.yml", ahora - 86400, nombre, fallos)
    ult = filas[max(n)] if n else {}
    c = collections.Counter(ult.values())
    reparto = " · ".join(f"{v} {num(100 * c.get(k, 0) / max(len(ult), 1), 1)} %" for k, v in E1.items())
    cn = [cambios[s] for s in n if s in cambios]
    return "\n".join([
        f"{nombre.upper()} (rama datos)",
        f"  Fotos: {len(n)}/{len(slots)} desde las {hora(inicio, True)} · última {hora(t_ultima, True)}",
        f"  Cadena: {estado} · fallidas en 24 h: {malas}",
        f"  Estaciones por foto: {num(n[max(n)]) if n else '-'} en la última (mediana {num(med)}, "
        f"mínimo {num(min(n.values())) if n else '-'})",
        f"  Estaciones que cambian de estado entre fotos seguidas: mediana {num(median(cn)) if cn else '-'}"
        + (f" ({num(100 * median(cn) / max(med, 1), 1)} %)" if cn else ""),
        linea_ctl + (f" · fotos con zonas fallidas: {len(zonas)}" if ctl else ""),
        f"  Última foto: {reparto}",
    ])


def bot2(ahora, fallos, novedades):
    nombre = nombre_de("datos2")
    sha, rutas = arbol("datos2")
    fs = fotos(rutas, "x")
    inicio, fin, slots = ventana(ahora, fs[0][0] // PASO * PASO, fs[-1][0] // PASO * PASO)
    completas = [t for t, m, p in fs if m == "_f" and t // PASO * PASO <= inicio]
    desde = max(completas) if completas else fs[0][0]
    usar = [(t, m, p) for t, m, p in fs if t >= desde]
    ctl_todos = controles(sha, rutas, desde)
    estado, serie, desajustes = {}, {}, []
    for k, ((t, m, p), filas) in enumerate(zip(usar, todas(sha, [p for t, m, p in usar]))):
        antes = set(estado)
        if m == "_f":
            estado = {}
        for r in filas:
            i, c = int(r["i"]), int(r["s"])
            if c < 0:
                estado.pop(i, None)
            else:
                estado[i] = (c, r["u"])
        d = ctl_todos.get(t)
        if d and d.get("f"):
            firma = hashlib.sha1("".join(f"{i},{c},{u}\n" for i, (c, u) in sorted(estado.items())).encode()).hexdigest()
            if firma != d["f"]:
                desajustes.append((t // PASO * PASO, len(estado), d["n"]))
        serie[t // PASO * PASO] = {"t": t, "m": m, "n": len(estado), "filas": len(filas),
                                   "altas": len(estado.keys() - antes) if k else 0,
                                   "bajas": len(antes - estado.keys()) if k else 0}
    v = {s: serie[s] for s in slots if s in serie}
    t_ultima = max(x["t"] for x in serie.values())
    faltan = [s for s in slots if s not in serie]
    parciales = [s for s in v if v[s]["m"] == "_p"]
    congeladas = [s for s in v if v[s]["m"] == "" and v[s]["filas"] == 0]
    ult_f = max([t for t, m, p in fs if m == "_f"], default=0)
    n0, n1 = (v[min(v)]["n"], v[max(v)]["n"]) if v else (0, 0)
    desajustes = [x for x in desajustes if x[0] >= inicio]
    if desajustes:
        s, rec, n_api = desajustes[0]
        nuevo(fallos, f"{nombre}: lo guardado NO coincide con lo que dio la API en "
                      f"{n_(len(desajustes), 'foto', 'fotos')} (posible fallo del bot)",
              f"{tramos([x[0] for x in desajustes])}; en la primera, " +
              (f"{num(rec)} puntos guardados frente a {num(n_api)} de la API" if rec != n_api
               else "mismos puntos pero con estados distintos"))
    if ahora - t_ultima > MAX_EDAD:
        nuevo(fallos, f"{nombre}: no hay fotos desde las {hora(t_ultima, True)}")
    faltan_fotos(nombre, faltan, fallos)
    if parciales:
        nuevo(fallos, f"{nombre}: {n_(len(parciales), 'foto parcial', 'fotos parciales')} (páginas fallidas)",
              tramos(parciales))
    if congeladas:
        nuevo(fallos, f"{nombre}: {n_(len(congeladas), 'foto', 'fotos')} sin ningún cambio (¿datos congelados?)",
              tramos(congeladas))
    if ahora - ult_f > 26 * 3600:
        nuevo(fallos, f"{nombre}: no hay foto completa desde las {hora(ult_f, True)}",
              "debería haber una cada día a las 00:00 UTC")
    ctl, linea_ctl = control(nombre, ctl_todos, {s: v[s]["t"] for s in v}, fallos)
    errores = collections.Counter()
    for d in ctl.values():
        errores.update(d.get("mot") or {})
    con429 = [s for s, d in ctl.items() if (d.get("mot") or {}).get("429")]
    if con429:
        nuevo(fallos, f"{nombre}: el servidor respondió 429 (límite de peticiones) en {n_(len(con429), 'foto', 'fotos')}",
              tramos(con429))
    dic = [r for d in todas(sha, sorted(p for p in rutas if p.startswith("i/"))) for r in d]
    ids = {int(r["i"]) for r in dic}
    if len(ids) != len(dic) or len({(r["l"], r["e"]) for r in dic}) != len(dic) or max(ids) + 1 != len(ids):
        nuevo(fallos, f"{nombre}: el diccionario de puntos (carpeta i/) tiene índices o claves repetidos o huecos")
    if any(i >= len(ids) for i in estado):
        nuevo(fallos, f"{nombre}: hay puntos con un índice que no está en el diccionario")
    for s in v:
        if v[s]["bajas"] > MAX_BAJAS:
            nuevo(novedades, f"{nombre}: {num(v[s]['bajas'])} puntos dejan de aparecer de golpe",
                  f"foto de las {hora(s, True)}")
    variacion(nombre, "los puntos", n0, n1, novedades)
    est, malas = cadena("ci2.yml", ahora - 86400, nombre, fallos)
    c = collections.Counter(s for s, u in estado.values())
    otros = sum(x for k, x in c.items() if k not in E2)
    reparto = " · ".join([f"{e} {num(100 * c.get(k, 0) / max(len(estado), 1), 1)} %" for k, e in E2.items()] +
                         [f"otros {num(100 * otros / max(len(estado), 1), 1)} %"])
    d_ult = ctl_todos.get(t_ultima)
    med_c = median([x["filas"] for x in v.values() if x["m"] != "_f"] or [0])
    err = ", ".join(f"{k} ×{x}" for k, x in errores.most_common()) if errores else "ninguno"
    if errores and not parciales:
        err += " (todos recuperados al reintentar)"
    return "\n".join([
        f"{nombre.upper()} (rama datos2)",
        f"  Fotos: {len(v)}/{len(slots)} desde las {hora(inicio, True)} · parciales: {len(parciales)} · "
        f"última {hora(t_ultima, True)}",
        f"  Cadena: {est} · fallidas en 24 h: {malas}",
        f"  Puntos: {num(n1)}" + (f" (la API dio {num(d_ult['n'])} en la última foto)" if d_ult else "") +
        f" · al principio de la ventana {num(n0)} · altas {num(sum(x['altas'] for x in v.values()))}"
        f" · bajas {num(sum(x['bajas'] for x in v.values()))}",
        f"  Puntos que cambian entre fotos seguidas: mediana {num(med_c)} ({num(100 * med_c / max(n1, 1), 1)} %)",
        linea_ctl,
        f"  Comprobación contra la API: {n_(len([d for d in ctl.values() if d.get('f')]), 'foto comprobada', 'fotos comprobadas')}"
        f", {len(desajustes)} no coinciden",
        f"  Errores de red: {err}",
        f"  Última foto: {reparto}",
        f"  Última foto completa: {hora(ult_f, True)}",
    ])


def informe():
    ahora = int(time.time())
    fallos, novedades, bloques = [], [], []
    for f in (bot1, bot2):
        try:
            bloques.append(f(ahora, fallos, novedades))
        except Exception:
            nuevo(fallos, f"no se pudo revisar {f.__name__}", traceback.format_exc(limit=1).strip().splitlines()[-1])
    try:
        tam = f"{num(api('')['size'] / 1024)} MB"
    except Exception:
        tam = "?"
    hoy = datetime.fromtimestamp(ahora, TZ)
    if fallos:
        asunto = f"AVISO · {fallos[0][0]}" + (f" (+{len(fallos) - 1})" if len(fallos) > 1 else "") + f" · {hoy:%d/%m}"
    else:
        asunto = f"OK · informe del {hoy:%d/%m}" + (
            f" · {n_(len(novedades), 'novedad', 'novedades')} en los datos" if novedades else "")
    cuerpo = [f"Informe del {hoy:%d/%m/%Y} a las {hoy:%H:%M} (hora peninsular). Ventana: últimas 24 h.", ""]
    if fallos:
        cuerpo += ["FALLOS DE LOS BOTS (hay que revisar)"] + [linea(*x) for x in fallos]
    else:
        cuerpo += ["Los bots funcionan bien: no falta ninguna foto, no hay fotos parciales ni ejecuciones fallidas, "
                   "y lo guardado coincide con lo que dio la API."]
    if novedades:
        cuerpo += ["", "NOVEDADES EN LOS DATOS (solo informativo: no es un fallo de los bots)"]
        cuerpo += [linea(*x) for x in novedades]
    cuerpo += ["", "\n\n".join(bloques), "", f"Tamaño del repo: {tam}", f"https://github.com/{REPO}/actions"]
    return asunto, "\n".join(cuerpo) + "\n"


def vigilar(rama, prueba=False):
    """Aviso inmediato si la rama lleva mas de 2 h sin fotos; se repite cada 6 h (marca en el ultimo commit)."""
    b = api(f"/branches/{rama}")
    sha = b["commit"]["sha"]
    t = datetime.fromisoformat(b["commit"]["commit"]["committer"]["date"].replace("Z", "+00:00")).timestamp()
    edad = time.time() - t
    if edad < ALERTA and not prueba:
        return "-"
    marca = "wp" if prueba else f"w{int((edad - ALERTA) // REPITE)}"
    if any(s["context"] == marca for s in api(f"/commits/{sha}/statuses?per_page=100")):
        return "-"
    nombre = nombre_de(rama)
    fallos = []
    est, malas = cadena(RAMAS[rama][1], time.time() - 86400, nombre, fallos)
    asunto = ("PRUEBA · " if prueba else "") + f"ALERTA · {nombre} sin fotos desde las {hora(t, True)}"
    cuerpo = "\n".join([
        f"La última foto de {nombre} (rama {rama}) se guardó a las {hora(t, True)} (hora peninsular), "
        f"hace {num(edad / 3600, 1)} h.",
        f"Cadena: {est} · ejecuciones fallidas en 24 h: {malas}",
        *[linea(*x) for x in fallos],
        "",
        f"https://github.com/{REPO}/actions/workflows/{RAMAS[rama][1]}",
        "",
        "Este aviso lo manda el otro bot. Se repite cada 6 h mientras no haya fotos nuevas.",
    ]) + "\n"
    enviar(asunto, cuerpo)
    api(f"/statuses/{sha}", {"state": "success", "context": marca})
    return "ok"


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
    ap.add_argument("--vigilar", default="", choices=["", *RAMAS])
    ap.add_argument("--prueba", action="store_true")
    a = ap.parse_args()
    if a.vigilar:
        print(vigilar(a.vigilar, a.prueba))
        return
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
