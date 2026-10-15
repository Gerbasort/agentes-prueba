import os
import time
from openai import OpenAI, RateLimitError

client = OpenAI(
    base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
    api_key=os.environ["GEMINI_API_KEY"],
)
MODEL = "gemini-2.5-flash-lite"  # confirma el nombre vigente en AI Studio


def agente(rol: str, tarea: str) -> str:
    """Un agente = una llamada al modelo con su propio rol."""
    for intento in range(5):
        try:
            r = client.chat.completions.create(
                model=MODEL,
                max_tokens=1500,
                messages=[
                    {"role": "system", "content": rol},
                    {"role": "user", "content": tarea},
                ],
            )
            time.sleep(7)  # respeta el límite gratuito de peticiones por minuto
            return r.choices[0].message.content
        except RateLimitError:
            time.sleep(20 * (intento + 1))
    raise RuntimeError("Se superó el límite de peticiones")


INVESTIGADOR = "Eres un investigador. Entrega datos clave en viñetas, sin relleno."
REDACTOR = "Eres un redactor claro y directo. Escribes en español."
REVISOR = (
    "Eres un revisor estricto. Señala mejoras concretas. "
    "Si todo está bien, responde solo: APROBADO."
)

tema = os.environ.get("TEMA", "Cómo funciona la energía solar fotovoltaica")

print("1) Investigando...")
notas = agente(INVESTIGADOR, f"Investiga sobre: {tema}")

print("2) Redactando...")
borrador = agente(REDACTOR, f"Con estas notas escribe un artículo corto:\n{notas}")

for ronda in range(1, 4):  # máximo 3 rondas de revisión
    print(f"3) Revisando (ronda {ronda})...")
    critica = agente(REVISOR, f"Revisa este texto:\n{borrador}")
    if "APROBADO" in critica:
        break
    borrador = agente(
        REDACTOR,
        f"Mejora el texto según la crítica.\n\nTEXTO:\n{borrador}\n\nCRÍTICA:\n{critica}",
    )

with open("resultado.md", "w", encoding="utf-8") as f:
    f.write(borrador)

print("\n===== RESULTADO FINAL =====\n")
print(borrador)
