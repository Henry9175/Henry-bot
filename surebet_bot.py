#!/usr/bin/env python3
"""
Surebet Bot: detecta surebets (arbitraje deportivo) con The Odds API y avisa por Telegram.
Sin librerías externas: solo Python 3.8 o superior.

  python surebet_bot.py --demo          prueba con datos de ejemplo (no gasta créditos)
  python surebet_bot.py --list-sports   lista los deportes activos (gratis)
  python surebet_bot.py --telegram-id   muestra tu TELEGRAM_CHAT_ID
  python surebet_bot.py --once          un solo escaneo y termina
  python surebet_bot.py                 escanea en bucle

La configuración está en config.env (junto a este archivo).
"""
import argparse
import html
import itertools
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.the-odds-api.com/v4"
EXCHANGES = {"betfair_ex_eu", "betfair_ex_uk", "betfair_ex_au", "matchbook", "smarkets"}
ETIQUETAS = {"Draw": "Empate", "Over": "Más de", "Under": "Menos de"}
ICONOS = {"soccer": "⚽", "basketball": "🏀", "baseball": "⚾", "americanfootball": "🏈",
          "icehockey": "🏒", "tennis": "🎾"}


# ───────────────────────── utilidades ─────────────────────────
def log(msg):
    print(f"[{datetime.now():%H:%M:%S}] {msg}", flush=True)


def cargar_env(ruta):
    """Lee config.env (CLAVE=valor). Las variables del sistema tienen prioridad."""
    if not os.path.exists(ruta):
        return
    with open(ruta, encoding="utf-8") as f:
        for linea in f:
            linea = linea.strip()
            if linea and not linea.startswith("#") and "=" in linea:
                k, v = linea.split("=", 1)
                os.environ.setdefault(k.strip(), v.split(" #")[0].strip().strip("\"'"))


def texto(nombre, defecto=""):
    return os.environ.get(nombre, "").strip() or defecto


def numero(nombre, defecto):
    try:
        return float(texto(nombre, str(defecto)))
    except ValueError:
        sys.exit(f"Valor inválido en {nombre}: debe ser un número.")


def lista(nombre, defecto=""):
    return [x.strip() for x in texto(nombre, defecto).split(",") if x.strip()]


def exigir(valor, nombre):
    if not valor:
        sys.exit(f"Falta {nombre} en config.env")


def describir(e):
    if isinstance(e, urllib.error.HTTPError):
        try:
            detalle = json.loads(e.read().decode("utf-8")).get("message", "")
        except Exception:
            detalle = ""
        return f"HTTP {e.code} {detalle}".strip()
    return str(e)


def http(url, datos=None, reintentos=3, timeout=30):
    """GET/POST con reintentos: pensado para conexiones inestables. Devuelve (json, headers)."""
    ultimo = None
    for i in range(reintentos):
        try:
            req = urllib.request.Request(url, data=datos, headers={"User-Agent": "surebet-bot/1.0"})
            with urllib.request.urlopen(req, timeout=timeout) as r:
                return json.loads(r.read().decode("utf-8")), r.headers
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403, 404, 422):
                raise  # error de configuración: reintentar no sirve
            ultimo = e
        except (urllib.error.URLError, OSError, ValueError) as e:
            ultimo = e
        time.sleep(3 * (i + 1))
    raise ultimo


# ───────────────────────── Telegram ─────────────────────────
def telegram(token, chat, msg):
    datos = urllib.parse.urlencode({"chat_id": chat, "text": msg, "parse_mode": "HTML",
                                    "disable_web_page_preview": "true"}).encode()
    http(f"https://api.telegram.org/bot{token}/sendMessage", datos)


def telegram_chat_ids(token):
    res, _ = http(f"https://api.telegram.org/bot{token}/getUpdates")
    ids = {}
    for u in res.get("result", []):
        chat = (u.get("message") or u.get("channel_post") or {}).get("chat")
        if chat:
            ids[chat["id"]] = chat.get("title") or chat.get("first_name") or chat.get("username") or "?"
    return ids


# ───────────────────────── lógica de arbitraje ─────────────────────────
def fecha(s):
    return datetime.fromisoformat(s.replace("Z", "+00:00"))


def cuota_neta(casa, cuota, comision):
    """En exchanges (Betfair, Matchbook...) la comisión se descuenta de la ganancia."""
    return 1 + (cuota - 1) * (1 - comision) if casa in EXCHANGES else cuota


def repartir(cuotas, capital, paso):
    """Reparte el capital para igualar el retorno en cada resultado y redondea a 'paso'
    probando piso/techo de cada apuesta para quedarse con la mejor combinación."""
    inv = sum(1 / c for c in cuotas)
    opciones = []
    for c in cuotas:
        ideal = capital / inv / c / paso
        opciones.append({math.floor(ideal) * paso, math.ceil(ideal) * paso})
    mejor = None
    for combo in itertools.product(*opciones):
        if min(combo) <= 0:
            continue
        total = sum(combo)
        retorno = min(s * c for s, c in zip(combo, cuotas))
        pct = (retorno - total) / total * 100
        if mejor is None or pct > mejor["pct"]:
            mejor = {"pct": pct, "stakes": combo, "total": total, "retorno": retorno}
    return mejor


def buscar_surebets(eventos, ahora, C):
    encontradas = []
    for ev in eventos:
        if fecha(ev["commence_time"]) <= ahora + timedelta(minutes=5):
            continue  # solo prepartido: en vivo las cuotas cambian demasiado rápido
        mejores = {}  # (mercado, línea) -> {resultado: (neta, cuota, key, casa, edad_min)}
        for bk in ev.get("bookmakers", []):
            for m in bk.get("markets", []):
                if m["key"] not in ("h2h", "totals"):
                    continue
                upd = m.get("last_update") or bk.get("last_update")
                edad = (ahora - fecha(upd)).total_seconds() / 60 if upd else 0
                if C["edad_max"] and edad > C["edad_max"]:
                    continue  # cuota vieja o mercado suspendido
                for o in m["outcomes"]:
                    punto = o.get("point")
                    if o["price"] <= 1:
                        continue
                    if m["key"] == "totals" and (punto is None or (punto * 2) % 1):
                        continue  # se descartan líneas de cuarto (2.25, 2.75...)
                    neta = cuota_neta(bk["key"], o["price"], C["comision"])
                    slot = mejores.setdefault((m["key"], punto), {})
                    if o["name"] not in slot or neta > slot[o["name"]][0]:
                        slot[o["name"]] = (neta, o["price"], bk["key"], bk["title"], edad)
        for (mk, punto), outs in mejores.items():
            con_empate = mk == "h2h" and (ev["sport_key"].startswith("soccer") or "Draw" in outs)
            if len(outs) != (3 if con_empate else 2):
                continue  # faltan resultados: no se puede cubrir todo
            nombres = list(outs)
            netas = [outs[n][0] for n in nombres]
            inv = sum(1 / c for c in netas)
            if inv >= 1 or len({outs[n][2] for n in nombres}) < 2:
                continue
            if (1 / inv - 1) * 100 > C["gan_max"]:
                continue  # demasiado bueno para ser verdad: casi siempre es dato viejo o error
            rep = repartir(netas, C["capital"], C["redondeo"])
            if not rep or rep["pct"] < C["gan_min"]:
                continue
            encontradas.append({
                "ev": ev, "mk": mk, "punto": punto, "rep": rep,
                "legs": [(n, *outs[n][1:], s) for n, s in zip(nombres, rep["stakes"])],
            })
    return sorted(encontradas, key=lambda s: -s["rep"]["pct"])


def mensaje(s, C):
    ev, rep, esc = s["ev"], s["rep"], html.escape
    hora = fecha(ev["commence_time"]).astimezone(C["tz"]).strftime("%d/%m %H:%M")
    icono = next((v for k, v in ICONOS.items() if ev["sport_key"].startswith(k)), "🏅")
    if s["mk"] == "totals":
        mercado = f"Más/Menos {s['punto']:g}"
    else:
        mercado = "1X2 (con empate)" if len(s["legs"]) == 3 else "Ganador del partido"
    lineas = [f"🟢 <b>SUREBET +{rep['pct']:.2f}%</b>",
              f"{icono} {esc(ev['home_team'])} vs {esc(ev['away_team'])}",
              f"🏆 {esc(ev.get('sport_title', ev['sport_key']))} · {mercado}",
              f"🕒 {hora} ({C['tz_nombre']})", ""]
    for nombre, cuota, casa_key, casa, edad, stake in s["legs"]:
        etiqueta = ETIQUETAS.get(nombre, nombre)
        if s["mk"] == "totals":
            etiqueta += f" {s['punto']:g}"
        extra = " <i>(exchange, comisión incluida)</i>" if casa_key in EXCHANGES else ""
        if edad >= 3:
            extra += f" ⏱ cuota de hace {edad:.0f} min"
        lineas.append(f"• <b>{esc(etiqueta)}</b> @ {cuota:.2f} en {esc(casa)}: <b>${stake:,.2f}</b>{extra}")
    lineas += ["", f"💵 Inviertes ${rep['total']:,.2f} → recibes mínimo ${rep['retorno']:,.2f}",
               f"✅ Ganancia asegurada: ${rep['retorno'] - rep['total']:,.2f}",
               "", "⚠️ Verifica las cuotas en cada casa antes de apostar."]
    return "\n".join(lineas)


# ───────────────────────── datos ─────────────────────────
def eventos_en_vivo(C):
    """Descarga las cuotas de cada deporte configurado. Devuelve (eventos, créditos_restantes)."""
    clave = urllib.parse.quote(C["api_key"])
    sports, _ = http(f"{API}/sports/?apiKey={clave}")  # no consume créditos
    activos = {s["key"] for s in sports if s.get("active")}
    eventos, restantes = [], None
    for dep in C["deportes"]:
        if dep not in activos:
            log(f"{dep}: fuera de temporada o clave inválida, se omite")
            continue
        q = urllib.parse.urlencode({"apiKey": C["api_key"], "bookmakers": ",".join(C["casas"]),
                                    "markets": ",".join(C["mercados"]), "oddsFormat": "decimal"})
        datos, headers = http(f"{API}/sports/{dep}/odds/?{q}")
        eventos += datos
        restantes = headers.get("x-requests-remaining")
        if restantes is not None and float(restantes) < C["reserva"]:
            log(f"Quedan {restantes} créditos (reserva {C['reserva']:g}): dejo de consultar este ciclo.")
            break
    return eventos, restantes


def eventos_demo():
    ahora = datetime.now(timezone.utc)

    def iso(dt):
        return dt.strftime("%Y-%m-%dT%H:%M:%SZ")

    def casa(key, titulo, mercado, resultados):
        return {"key": key, "title": titulo,
                "markets": [{"key": mercado, "last_update": iso(ahora), "outcomes": resultados}]}

    def h2h(a, e, b):
        return [{"name": "Tigres", "price": a}, {"name": "Draw", "price": e}, {"name": "Leones", "price": b}]

    def totales(o, u):
        return [{"name": "Over", "price": o, "point": 220.5}, {"name": "Under", "price": u, "point": 220.5}]

    def evento(i, deporte, horas, local, visita, casas):
        return {"id": f"demo-{i}", "sport_key": deporte, "sport_title": "Demo",
                "commence_time": iso(ahora + timedelta(hours=horas)),
                "home_team": local, "away_team": visita, "bookmakers": casas}

    return [
        evento(1, "soccer_demo", 5, "Tigres", "Leones", [
            casa("pinnacle", "Pinnacle", "h2h", h2h(2.18, 3.30, 3.60)),
            casa("onexbet", "1xBet", "h2h", h2h(2.05, 3.75, 3.50)),
            casa("betsson", "Betsson", "h2h", h2h(2.00, 3.40, 4.15))]),
        evento(2, "basketball_demo", 8, "Cardenales", "Magallanes", [
            casa("pinnacle", "Pinnacle", "totals", totales(2.00, 1.80)),
            casa("onexbet", "1xBet", "totals", totales(1.80, 2.15))]),
        evento(3, "soccer_demo", 12, "Tigres", "Leones", [  # este NO tiene arbitraje
            casa("pinnacle", "Pinnacle", "h2h", h2h(2.00, 3.40, 3.70)),
            casa("onexbet", "1xBet", "h2h", h2h(1.95, 3.50, 3.60)),
            casa("betsson", "Betsson", "h2h", h2h(2.02, 3.30, 3.65))]),
    ]


# ───────────────────────── programa principal ─────────────────────────
def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")  # emojis en consolas de Windows
    except AttributeError:
        pass
    ap = argparse.ArgumentParser(description="Detector de surebets con alertas por Telegram")
    ap.add_argument("--demo", action="store_true", help="prueba con datos de ejemplo (sin API key)")
    ap.add_argument("--once", action="store_true", help="un solo escaneo y salir")
    ap.add_argument("--list-sports", action="store_true", help="lista los deportes activos (gratis)")
    ap.add_argument("--telegram-id", action="store_true", help="muestra tu TELEGRAM_CHAT_ID")
    args = ap.parse_args()
    cargar_env(os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.env"))

    offset = numero("UTC_OFFSET", -4)  # Venezuela: UTC-4 todo el año
    C = {
        "api_key": texto("ODDS_API_KEY"),
        "tg_token": texto("TELEGRAM_TOKEN"), "tg_chat": texto("TELEGRAM_CHAT_ID"),
        "casas": lista("CASAS"),
        "deportes": lista("DEPORTES", "soccer_epl,soccer_spain_la_liga,baseball_mlb"),
        "mercados": lista("MERCADOS", "h2h"),
        "capital": numero("CAPITAL", 100), "redondeo": max(numero("REDONDEO", 1), 0.01),
        "gan_min": numero("GANANCIA_MIN", 1.5), "gan_max": numero("GANANCIA_MAX", 8),
        "comision": numero("COMISION_EXCHANGE", 5) / 100, "edad_max": numero("EDAD_MAX_MIN", 10),
        "cada": max(numero("MINUTOS_ENTRE_ESCANEOS", 15), 1), "cooldown": numero("MINUTOS_SIN_REPETIR", 30),
        "reserva": numero("CREDITOS_RESERVA", 20),
        "tz": timezone(timedelta(hours=offset)), "tz_nombre": f"UTC{offset:+g}",
    }

    if args.telegram_id:
        exigir(C["tg_token"], "TELEGRAM_TOKEN")
        ids = telegram_chat_ids(C["tg_token"])
        if not ids:
            sys.exit("No hay mensajes todavía: escríbele algo a tu bot en Telegram y repite este comando.")
        for cid, nombre in ids.items():
            print(f"TELEGRAM_CHAT_ID={cid}    ({nombre})")
        return

    if args.list_sports:
        exigir(C["api_key"], "ODDS_API_KEY")
        sports, _ = http(f"{API}/sports/?apiKey={urllib.parse.quote(C['api_key'])}")
        for s in sports:
            if s.get("active"):
                print(f"{s['key']:48} {s['group']} - {s['title']}")
        return

    vistos = {}

    def enviar(msg):
        print(re.sub(r"<[^>]+>", "", html.unescape(msg)) + "\n" + "-" * 48)
        if C["tg_token"] and C["tg_chat"]:
            try:
                telegram(C["tg_token"], C["tg_chat"], msg)
            except Exception as e:
                log(f"No pude enviar a Telegram: {describir(e)}")

    def procesar(eventos):
        nuevas = 0
        for s in buscar_surebets(eventos, datetime.now(timezone.utc), C):
            clave = (s["ev"]["id"], s["mk"], s["punto"])
            if time.time() - vistos.get(clave, 0) < C["cooldown"] * 60:
                continue  # ya avisé de esta hace poco
            vistos[clave] = time.time()
            enviar(mensaje(s, C))
            nuevas += 1
        return nuevas

    if args.demo:
        n = procesar(eventos_demo())
        log(f"Demo lista: {n} surebets detectadas en 3 eventos de ejemplo (el evento 3 no tiene arbitraje).")
        return

    exigir(C["api_key"], "ODDS_API_KEY")
    exigir(C["casas"], "CASAS")
    exigir(C["deportes"], "DEPORTES")
    por_escaneo = len(C["deportes"]) * len(C["mercados"]) * math.ceil(len(C["casas"]) / 10)
    al_mes = por_escaneo * (1440 / C["cada"]) * 30
    log(f"Casas: {', '.join(C['casas'])}")
    log(f"Costo estimado: {por_escaneo} créditos por escaneo, unos {al_mes:,.0f} al mes (plan gratis: 500/mes).")
    if not (C["tg_token"] and C["tg_chat"]):
        log("Telegram sin configurar: las alertas solo se mostrarán en esta pantalla.")

    while True:
        try:
            eventos, restantes = eventos_en_vivo(C)
            n = procesar(eventos)
            log(f"Escaneo listo: {len(eventos)} eventos, {n} surebets nuevas, créditos restantes: {restantes}")
        except urllib.error.HTTPError as e:
            if e.code == 401:
                sys.exit(f"API key inválida o créditos agotados ({describir(e)}).")
            log(f"Error de la API: {describir(e)}")
        except Exception as e:
            log(f"Sin conexión o error temporal: {describir(e)}. Reintento en el próximo ciclo.")
        if args.once:
            break
        time.sleep(C["cada"] * 60)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\nBot detenido.")
    except Exception as e:
        sys.exit(f"Error: {describir(e)}")
