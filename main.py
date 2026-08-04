"""
MIRA — Backend API (FastAPI)
-----------------------------
Reaproveita toda a lógica de negócio do app Streamlit original,
exposta agora como endpoints REST para o frontend Next.js consumir.
"""

import os
import re
import requests
from datetime import date
from io import BytesIO
from typing import Optional, List

from fastapi import FastAPI, HTTPException, Header, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from bs4 import BeautifulSoup
from supabase import create_client

# --- CONFIGURAÇÃO ---
app = FastAPI(title="Mira API")

FRONTEND_URL = os.getenv("FRONTEND_URL", "*")  # em produção: URL exata da Vercel
app.add_middleware(
    CORSMiddleware,
    allow_origins=[FRONTEND_URL] if FRONTEND_URL != "*" else ["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_SERVICE_KEY = os.getenv("SUPABASE_SERVICE_KEY", "")
RAPIDAPI_KEY = os.getenv("RAPIDAPI_KEY", "")
GOOGLE_PLACES_API_KEY = os.getenv("GOOGLE_PLACES_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENROUTER_API_URL = "https://openrouter.ai/api/v1/chat/completions"
SEARCH_URL = "https://local-business-data.p.rapidapi.com/search"

LIMITE_DIARIO_PADRAO = 3
MAX_LEADS_FREE = 6
EMAILS_ADMIN = ["mazinguireshelton@gmail.com"]


def get_supabase():
    if not SUPABASE_URL or not SUPABASE_SERVICE_KEY:
        return None
    return create_client(SUPABASE_URL, SUPABASE_SERVICE_KEY)


# --- AUTENTICAÇÃO: verifica o token do utilizador (enviado pelo frontend) ---
def get_current_user(authorization: str = Header(None)):
    """O frontend manda o token do Supabase no header Authorization: Bearer <token>."""
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(status_code=401, detail="Token em falta.")
    token = authorization.replace("Bearer ", "")
    sb = get_supabase()
    if not sb:
        raise HTTPException(status_code=500, detail="Supabase não configurado no servidor.")
    try:
        user_resp = sb.auth.get_user(token)
        return user_resp.user
    except Exception:
        raise HTTPException(status_code=401, detail="Token inválido ou expirado.")


# --- MODELOS DE DADOS (o "formato" que a API espera receber) ---
class BuscaRequest(BaseModel):
    nicho: str
    regiao: str
    max_leads: int = 10


class PropostaRequest(BaseModel):
    nome: str
    nicho: str
    site: str = ""
    avaliacao: str = ""
    objetivo: str = ""
    remetente_nome: str = "Um consultor"
    telefone: str = ""
    email: str = ""
    cidade: str = ""
    pais: str = ""
    reviews: str = ""
    descricao: str = ""


class EmailRequest(BaseModel):
    remetente: str
    senha_app: str
    destinatario: str
    assunto: str
    corpo: str


class ExportRequest(BaseModel):
    leads: List[dict]
    nicho: str
    regiao: str


class DicaRequest(BaseModel):
    nome_empresa: str
    status: str
    dias: Optional[int] = None
    perfil_oferta: str = ""


# --- FUNÇÕES DE NEGÓCIO (extraídas do app Streamlit) ---
def calcular_score_oportunidade(site, avaliacao, num_avaliacoes, telefone):
    score = 0
    if not site: score += 30
    try:
        nota = float(avaliacao) if avaliacao else 0.0
        if 0 < nota < 4.0: score += 20
    except Exception:
        pass
    if not telefone: score -= 10
    return score


def extrair_email_do_site(url):
    if not url or url == "N/A":
        return ""
    try:
        headers = {'User-Agent': 'Mozilla/5.0'}
        resposta = requests.get(url, headers=headers, timeout=4)
        emails = set(re.findall(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}", resposta.text))
        validos = [e for e in emails if not e.endswith(('.png', '.jpg', '.jpeg', '.gif', '.webp'))]
        return ", ".join(validos[:2])
    except Exception:
        return ""


def buscar_lugares_osm(nicho, regiao, limit):
    try:
        geo = requests.get("https://nominatim.openstreetmap.org/search",
                            params={"q": regiao, "format": "json", "limit": 1},
                            headers={"User-Agent": "mira-app/1.0"}, timeout=15).json()
        if not geo:
            return []
        lat, lon = float(geo[0]["lat"]), float(geo[0]["lon"])
        query = f"""
        [out:json][timeout:25];
        (
          node["name"~"{nicho}",i](around:20000,{lat},{lon});
          node["shop"](around:20000,{lat},{lon})["name"~"{nicho}",i];
        );
        out body {limit * 2};
        """
        resp = requests.post("https://overpass-api.de/api/interpreter", data={"data": query}, timeout=30)
        if resp.status_code != 200:
            return []
        resultados, vistos = [], set()
        for el in resp.json().get("elements", []):
            if len(resultados) >= limit:
                break
            tags = el.get("tags", {})
            nome = tags.get("name")
            if not nome or nome in vistos:
                continue
            vistos.add(nome)
            tel = tags.get("phone", tags.get("contact:phone", ""))
            site = tags.get("website", tags.get("contact:website", ""))
            score = calcular_score_oportunidade(site, "", 0, tel)
            resultados.append({
                "score": score, "nome": nome, "telefone": tel,
                "email": extrair_email_do_site(site) if site else "",
                "site": site, "avaliacao": "N/A", "fonte": "OpenStreetMap"
            })
        return resultados
    except Exception:
        return []


def buscar_lugares_rapidapi(query, limit, nicho):
    if not RAPIDAPI_KEY:
        return []
    headers = {"x-rapidapi-key": RAPIDAPI_KEY, "x-rapidapi-host": "local-business-data.p.rapidapi.com"}
    params = {"query": query, "limit": str(limit), "language": "pt"}
    try:
        res = requests.get(SEARCH_URL, headers=headers, params=params, timeout=20)
        if res.status_code != 200:
            return []
        resultados = []
        for lugar in res.json().get("data", []):
            if len(resultados) >= limit:
                break
            nome = lugar.get("name", "N/A")
            tel = lugar.get("phone_number", "")
            site = lugar.get("website", "")
            aval = lugar.get("rating", "")
            score = calcular_score_oportunidade(site, aval, lugar.get("review_count", 0), tel)
            resultados.append({
                "score": score, "nome": nome, "telefone": tel,
                "email": extrair_email_do_site(site), "site": site,
                "avaliacao": str(aval), "fonte": "RapidAPI"
            })
        return resultados
    except Exception:
        return []


def buscar_lugares_google(nicho, regiao, limit):
    if not GOOGLE_PLACES_API_KEY:
        return []
    try:
        url = "https://places.googleapis.com/v1/places:searchText"
        headers = {
            "Content-Type": "application/json",
            "X-Goog-Api-Key": GOOGLE_PLACES_API_KEY,
            "X-Goog-FieldMask": "places.displayName,places.nationalPhoneNumber,places.websiteUri,places.rating"
        }
        body = {"textQuery": f"{nicho} em {regiao}", "maxResultCount": min(limit, 20)}
        res = requests.post(url, headers=headers, json=body, timeout=15)
        if res.status_code != 200:
            return []
        resultados = []
        for lugar in res.json().get("places", [])[:limit]:
            nome = lugar.get("displayName", {}).get("text", "N/A")
            tel = lugar.get("nationalPhoneNumber", "")
            site = lugar.get("websiteUri", "")
            aval = lugar.get("rating", "")
            score = calcular_score_oportunidade(site, aval, 0, tel)
            resultados.append({
                "score": score, "nome": nome, "telefone": tel,
                "email": extrair_email_do_site(site) if site else "",
                "site": site, "avaliacao": str(aval), "fonte": "Google Places"
            })
        return resultados
    except Exception:
        return []


def buscar_leads_cascata(nicho, regiao, limit):
    resultados = buscar_lugares_osm(nicho, regiao, limit)
    vistos = {r["nome"].lower() for r in resultados}

    faltam = limit - len(resultados)
    if faltam > 0:
        extras = buscar_lugares_rapidapi(f"{nicho} em {regiao}", faltam, nicho)
        for r in extras:
            if r["nome"].lower() not in vistos:
                resultados.append(r)
                vistos.add(r["nome"].lower())

    faltam = limit - len(resultados)
    if faltam > 0:
        extras = buscar_lugares_google(nicho, regiao, faltam)
        for r in extras:
            if r["nome"].lower() not in vistos:
                resultados.append(r)
                vistos.add(r["nome"].lower())

    return sorted(resultados, key=lambda x: x["score"], reverse=True)


def _resposta_valida(texto):
    if not texto or len(texto) < 40:
        return False
    bandeiras = ["user safety", "i cannot", "i can't assist", "as an ai"]
    return not any(b in texto.lower() for b in bandeiras)


SYSTEM_PROMPT = """
Tu és a IA oficial da plataforma MIRA.

És especialista em prospeção comercial B2B, marketing digital, websites, SEO e geração de leads.

REGRAS:

- Responde SEMPRE em português.
- Nunca respondas noutra língua.
- Nunca inventes informações.
- Usa apenas os dados fornecidos.
- Se faltar alguma informação, ignora-a.
- Nunca inventes website, email, telefone, cidade ou país.
- Nunca menciones Brasil, Portugal ou outro país sem estar nos dados.
- Não uses emojis.
- Não uses frases genéricas.
- Cada empresa deve receber uma análise diferente.
- Escreve como um consultor comercial experiente.

Classifica o lead em:
- Quente
- Morno
- Frio

Baseia a classificação na presença digital, reputação, website e oportunidade comercial.

A resposta deve seguir EXATAMENTE este formato:

LEAD:
Quente | Morno | Frio

DIAGNÓSTICO:
(Texto curto.)

MENSAGEM:
(Mensagem pronta para enviar no WhatsApp, personalizada e assinada apenas com o nome do vendedor.)

Não escrevas mais nada além deste formato.
"""


def gerar_proposta_ia(nome, nicho, site, avaliacao, objetivo, remetente_nome,
                       telefone="", email="", cidade="", pais="", reviews="", descricao=""):
    if not OPENROUTER_API_KEY:
        return None, "Chave OpenRouter em falta no servidor."
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}

    user_prompt = f"""
Empresa: {nome}

Categoria: {nicho}

Cidade: {cidade}

País: {pais}

Telefone: {telefone}

Email: {email}

Website: {site}

Avaliação: {avaliacao}

Número de avaliações: {reviews}

Descrição: {descricao}

Objetivo do vendedor:
{objetivo}

Nome do vendedor:
{remetente_nome}

Analisa esta empresa utilizando APENAS os dados acima.

Se algum dado estiver vazio, ignora-o.

Nunca inventes informações.
"""

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": user_prompt},
    ]

    erros = []
    for modelo in ["deepseek/deepseek-chat-v3-0324:free", "qwen/qwen3-30b-a3b:free",
                   "openrouter/free", "google/gemma-4-31b-it:free"]:
        try:
            payload = {"model": modelo, "messages": messages, "temperature": 0.4, "max_tokens": 700}
            res = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if _resposta_valida(texto):
                    return texto, None
                erros.append(f"{modelo}: resposta inválida")
            else:
                erros.append(f"{modelo}: HTTP {res.status_code}")
        except Exception as e:
            erros.append(f"{modelo}: {e}")
    return None, " | ".join(erros)


# --- CONTROLO DE USO (limites free/premium) ---
def verificar_e_registrar_uso(user_id, email):
    if email in EMAILS_ADMIN:
        return True, "Administrador."
    sb = get_supabase()
    if not sb:
        return True, "Sem verificação (Supabase não configurado)."

    cliente = sb.table("clientes").select("limite_diario, limite_total, plano").eq("id", user_id).execute()
    plano = cliente.data[0].get("plano", "free") if cliente.data else "free"
    if plano == "premium":
        return True, "Premium — sem limites."

    limite_diario = cliente.data[0]["limite_diario"] if cliente.data else LIMITE_DIARIO_PADRAO
    hoje = str(date.today())
    registro = sb.table("uso_diario").select("*").eq("user_id", user_id).eq("data", hoje).execute()

    if registro.data:
        contagem = registro.data[0]["contagem"]
        if contagem >= limite_diario:
            return False, f"Limite diário de {limite_diario} buscas atingido."
        sb.table("uso_diario").update({"contagem": contagem + 1}).eq("user_id", user_id).eq("data", hoje).execute()
    else:
        sb.table("uso_diario").insert({"user_id": user_id, "data": hoje, "contagem": 1}).execute()

    return True, "OK"


def eh_premium(user_id, email):
    if email in EMAILS_ADMIN:
        return True
    sb = get_supabase()
    if not sb:
        return False
    resp = sb.table("clientes").select("plano").eq("id", user_id).execute()
    return resp.data[0].get("plano") == "premium" if resp.data else False


# --- ENDPOINTS ---
@app.get("/")
def raiz():
    return {"status": "Mira API no ar"}


@app.post("/api/search")
def buscar(req: BuscaRequest, user=Depends(get_current_user)):
    permitido, msg = verificar_e_registrar_uso(user.id, user.email)
    if not permitido:
        raise HTTPException(status_code=429, detail=msg)

    premium = eh_premium(user.id, user.email)
    limite_real = req.max_leads if premium else min(req.max_leads, MAX_LEADS_FREE)

    resultados = buscar_leads_cascata(req.nicho, req.regiao, limite_real)
    return {"leads": resultados, "mensagem_uso": msg}


@app.post("/api/gerar-proposta")
def gerar_proposta(req: PropostaRequest, user=Depends(get_current_user)):
    sb = get_supabase()
    if sb and user.email not in EMAILS_ADMIN:
        resp = sb.table("clientes").select("plano, ia_usado").eq("id", user.id).execute()
        if resp.data:
            dados = resp.data[0]
            if dados.get("plano") != "premium" and dados.get("ia_usado"):
                raise HTTPException(status_code=403, detail="Já usaste a tua análise gratuita com IA. Faz upgrade para Premium.")

    texto, erro = gerar_proposta_ia(
        req.nome, req.nicho, req.site, req.avaliacao, req.objetivo, req.remetente_nome,
        telefone=req.telefone, email=req.email, cidade=req.cidade, pais=req.pais,
        reviews=req.reviews, descricao=req.descricao
    )
    if texto and sb and user.email not in EMAILS_ADMIN:
        resp = sb.table("clientes").select("plano").eq("id", user.id).execute()
        if resp.data and resp.data[0].get("plano") != "premium":
            sb.table("clientes").update({"ia_usado": True}).eq("id", user.id).execute()

    if not texto:
        raise HTTPException(status_code=502, detail=f"Não foi possível gerar a proposta: {erro}")
    return {"texto": texto}


@app.post("/api/enviar-email")
def enviar_email(req: EmailRequest, user=Depends(get_current_user)):
    import smtplib
    from email.mime.text import MIMEText

    msg = MIMEText(req.corpo)
    msg["Subject"] = req.assunto
    msg["From"] = req.remetente
    msg["To"] = req.destinatario

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as servidor:
            servidor.login(req.remetente, req.senha_app)
            servidor.sendmail(req.remetente, [req.destinatario], msg.as_string())
        return {"ok": True, "mensagem": "E-mail enviado!"}
    except smtplib.SMTPAuthenticationError:
        raise HTTPException(status_code=401, detail="Falha na autenticação — confirma a Senha de App do Gmail.")
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


@app.post("/api/dica-lead")
def dica_lead(req: DicaRequest, user=Depends(get_current_user)):
    if not OPENROUTER_API_KEY:
        raise HTTPException(status_code=500, detail="Chave OpenRouter em falta no servidor.")
    headers = {"Authorization": f"Bearer {OPENROUTER_API_KEY}", "Content-Type": "application/json"}
    prompt = f"""
    Estou a prospectar a empresa '{req.nome_empresa}'. Status atual: {req.status}.
    Já se passaram {req.dias if req.dias is not None else '?'} dias desde a última atualização.
    Eu ofereço: {req.perfil_oferta or 'serviços diversos'}.
    Dá uma sugestão curta e prática (máximo 3 frases) do que fazer a seguir com este lead,
    considerando o status e o tempo parado. Responde em Português, direto ao ponto.
    """
    for modelo in ["deepseek/deepseek-chat-v3-0324:free", "qwen/qwen3-30b-a3b:free", "openrouter/free"]:
        try:
            payload = {"model": modelo, "messages": [{"role": "user", "content": prompt}]}
            res = requests.post(OPENROUTER_API_URL, headers=headers, json=payload, timeout=25)
            if res.status_code == 200:
                texto = res.json()['choices'][0]['message']['content'].strip()
                if _resposta_valida(texto):
                    return {"texto": texto}
        except Exception:
            continue
    raise HTTPException(status_code=502, detail="Não foi possível gerar a dica agora.")
