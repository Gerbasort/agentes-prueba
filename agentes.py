import os
import sys
import time
import yaml
from graphlib import TopologicalSorter
from openai import OpenAI, RateLimitError, InternalServerError, APIConnectionError

ruta = sys.argv[1] if len(sys.argv) > 1 else "config.yml"
with open(ruta, encoding="utf-8") as f:
    cfg = yaml.safe_load(f)

MODEL = cfg.get("modelo", "gemini-2.5-flash-lite")
PAUSA = cfg.get("pausa_segundos", 7)
MAX_TOKENS = cfg.get("max_tokens", 4000)
tarea = os.environ.get("TEMA") or cfg.get("tarea", "")
roles = cfg["roles"]
agentes = cfg["agentes"]
ciclos = cfg.get("ciclos", [])

MIN_CHARS = cfg.get("min_caracteres", 200)
ETIQUETAS = cfg.get("etiquetas", {})


def etiqueta_de(r):
    return ETIQUETAS.get(r, r)

client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=os.environ["GEMINI_API_KEY"],
)

os.makedirs("outputs", exist_ok=True)
traza = open("outputs/traza.md", "w", encoding="utf-8")


# ---------- utilidades de configuración ----------
def lista(x):
    if x is None:
        return []
    return x if isinstance(x, list) else [x]


def entradas_de(a):
    return lista(a.get("entradas")) + lista(a.get("recibe"))


def historial_de(a):
    return lista(a.get("historial"))


def informe_de(id_):
    return agentes[id_].get("produce", id_)


def modelo_de(id_):
    return agentes[id_].get("modelo", MODEL)


# ---------- llamada al modelo ----------
ultima_llamada = {}  # momento de la última llamada, por modelo


def llamar(etiqueta, modelo, system, user):
    espera = PAUSA - (time.time() - ultima_llamada.get(modelo, 0))
    if espera > 0:
        time.sleep(espera)

    cortas = 0       # respuestas vacías o demasiado cortas
    motivo = None    # por qué terminó la última respuesta
    for intento in range(6):
        try:
            r = client.chat.completions.create(
                model=modelo,
                max_tokens=MAX_TOKENS,
                messages=[
                    {"role": "system", "content": system},
                    {"role": "user", "content": user},
                ],
            )
        except RateLimitError:
            print(f"⚠ {etiqueta}: límite de peticiones, esperando...", flush=True)
            time.sleep(20 * (intento + 1))
            continue
        except (InternalServerError, APIConnectionError) as e:
            print(f"⚠ {etiqueta}: error temporal del servidor ({type(e).__name__}), reintentando...", flush=True)
            time.sleep(15 * (intento + 1))
            continue

        ultima_llamada[modelo] = time.time()
        eleccion = r.choices[0]
        texto = (eleccion.message.content or "").strip()
        motivo = eleccion.finish_reason

        if len(texto) < MIN_CHARS and "APROBADO" not in texto:
            cortas += 1
            print(
                f"⚠ {etiqueta}: respuesta vacía o muy corta ({len(texto)} caracteres, "
                f"motivo: {motivo}), intento {cortas}/3",
                flush=True,
            )
            traza.write(
                f"## {etiqueta} [{modelo}] — RESPUESTA RECHAZADA ({len(texto)} caracteres, motivo: {motivo})\n\n"
                f"{texto}\n\n---\n\n"
            )
            traza.flush()
            if cortas >= 3:
                break
            time.sleep(PAUSA)
            continue

        if motivo == "length":
            print(f"⚠ {etiqueta}: la respuesta se cortó por max_tokens; sube max_tokens en el config", flush=True)

        traza.write(
            f"## {etiqueta} [{modelo}]\n\n**Prompt:**\n{user}\n\n**Respuesta:**\n{texto}\n\n---\n\n"
        )
        traza.flush()
        print(f"::group::Respuesta de {etiqueta}", flush=True)
        print(texto, flush=True)
        print("::endgroup::", flush=True)
        return texto

    raise RuntimeError(
        f"{etiqueta} no produjo una respuesta válida tras varios intentos (último motivo: {motivo})"
    )


# ---------- validación ----------
produce_de = {informe_de(i): i for i in agentes}
revisores = {a["revisado_por"] for a in agentes.values() if a.get("revisado_por")}
en_ciclo = {}
for c in ciclos:
    for i in c["agentes"]:
        if i not in agentes:
            sys.exit(f"Error: el ciclo '{c['nombre']}' usa un agente inexistente: {i}")
        en_ciclo[i] = c["nombre"]

for id_, a in agentes.items():
    if a["rol"] not in roles:
        sys.exit(f"Error: el agente '{id_}' usa un rol inexistente: {a['rol']}")
    for r in entradas_de(a) + historial_de(a):
        if r not in produce_de:
            sys.exit(f"Error: '{id_}' recibe el informe '{r}', pero ningún agente lo produce")
    if a.get("revisado_por") and a["revisado_por"] not in agentes:
        sys.exit(f"Error: '{id_}' tiene un revisor inexistente: {a['revisado_por']}")
if cfg["salida_final"] not in produce_de:
    sys.exit(f"Error: salida_final '{cfg['salida_final']}' no la produce ningún agente")


# ---------- orden de ejecución ----------
def nodo(id_):
    return "ciclo:" + en_ciclo[id_] if id_ in en_ciclo else id_


grafo = {}
for id_, a in agentes.items():
    if id_ in revisores:
        continue
    deps = grafo.setdefault(nodo(id_), set())
    for r in entradas_de(a) + historial_de(a):
        d = nodo(produce_de[r])
        if d != nodo(id_):
            deps.add(d)
try:
    orden = list(TopologicalSorter(grafo).static_order())
except Exception as e:
    sys.exit(f"Error: hay un ciclo de dependencias que no está declarado en 'ciclos': {e}")

# ---------- ejecución ----------
informes = {}   # última versión de cada informe
historia = {}   # todas las versiones de cada informe


def construir_prompt(id_, ronda=None, total=None):
    a = agentes[id_]
    partes = [a["instruccion"].replace("{tarea}", tarea)]
    if CONTINUA:
        partes.append(
            "(Esto continúa ejecuciones anteriores: los informes que recibes incluyen trabajo previo. "
            "No repitas lo ya establecido; parte de donde quedó.)"
        )
    if ronda:
        partes.append(f"(Ronda {ronda} de {total} del ciclo.)")

    pedidas = entradas_de(a)
    presentes = [r for r in pedidas if r in informes]   # lo que no existe, no se muestra
    for r in presentes:
        partes.append(f"### {etiqueta_de(r)}:\n{informes[r]}")
    if pedidas and not presentes:
        partes.append(
            "(No hay trabajo previo: es la primera vez que trabajas en esto. "
            "Produce tu informe completo desde cero.)"
        )

    for r in historial_de(a):
        versiones = historia.get(r, [])
        inicio = max(0, len(versiones) - HMAX)
        for n, texto in enumerate(versiones[inicio:], inicio + 1):
            partes.append(f"### {etiqueta_de(r)} (versión {n}):\n{texto}")
    return "\n\n".join(partes)


def guardar(id_, texto):
    nombre = informe_de(id_)
    informes[nombre] = texto
    historia.setdefault(nombre, []).append(texto)
    with open(f"outputs/{nombre}_v{len(historia[nombre])}.md", "w", encoding="utf-8") as f:
        f.write(texto)


def ejecutar(id_, ronda=None, total=None):
    a = agentes[id_]
    etiqueta = f"{id_} (ronda {ronda})" if ronda else id_
    print(f"▶ {etiqueta} [{modelo_de(id_)}]", flush=True)
    prompt = construir_prompt(id_, ronda, total)
    texto = llamar(etiqueta, modelo_de(id_), roles[a["rol"]], prompt)

    rv = a.get("revisado_por")
    if rv:
        r = agentes[rv]
        for n in range(1, a.get("max_rondas", 2) + 1):
            print(f"  ↻ {rv} revisa (revisión {n})", flush=True)
            critica = llamar(
                f"{rv} (revisión {n})",
                modelo_de(rv),
                roles[r["rol"]],
                r["instruccion"].replace("{tarea}", tarea) + f"\n\n### Texto a revisar:\n{texto}",
            )
            if "APROBADO" in critica:
                break
            texto = llamar(
                f"{id_} (corrección {n})",
                modelo_de(id_),
                roles[a["rol"]],
                f"{prompt}\n\n### Tu versión anterior:\n{texto}\n\n### Crítica a aplicar:\n{critica}",
            )
    guardar(id_, texto)


for n in orden:
    if n in revisores:
        continue
    if n.startswith("ciclo:"):
        c = next(c for c in ciclos if "ciclo:" + c["nombre"] == n)
        for ronda in range(1, c["rondas"] + 1):
            for id_ in c["agentes"]:
                ejecutar(id_, ronda, c["rondas"])
    else:
        ejecutar(n)

final = informes[cfg["salida_final"]]
with open("outputs/resultado.md", "w", encoding="utf-8") as f:
    f.write(final)
print("\n===== RESULTADO FINAL =====\n")
print(final)
