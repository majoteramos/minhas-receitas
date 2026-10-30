
import json
import re
import os
from urllib.parse import urlparse
from io import BytesIO
from xml.sax.saxutils import escape

import requests
import streamlit as st
from PIL import Image as PILImage
from bs4 import BeautifulSoup
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.enums import TA_CENTER
from reportlab.lib.units import cm
from reportlab.pdfbase import pdfmetrics
from reportlab.pdfbase.ttfonts import TTFont
from reportlab.platypus import (
    SimpleDocTemplate, Paragraph, Spacer, Image as PDFImage,
    PageBreak, Table, TableStyle
)

st.set_page_config(page_title="As minhas receitas", page_icon="🍲")
st.title("🍲 As minhas receitas")
st.write("Extrai receitas de páginas da Internet, adiciona fotografias e cria PDFs.")

CABECALHOS = ["h1", "h2", "h3", "h4", "h5", "h6"]

def procurar_receita(dados):
    fila = [dados]
    while fila:
        item = fila.pop(0)
        if isinstance(item, list):
            fila.extend(item)
        elif isinstance(item, dict):
            tipo = item.get("@type", [])
            if isinstance(tipo, str):
                tipo = [tipo]
            if any(str(t).split("/")[-1] == "Recipe" for t in tipo):
                return item
            for valor in item.values():
                if isinstance(valor, (dict, list)):
                    fila.append(valor)
    return None


def texto_limpo(elemento):
    return elemento.get_text(" ", strip=True).strip()


def quantidade_no_texto(texto):
    padrao = (
        r"\d|"
        r"\b(g|kg|mg|ml|cl|l|"
        r"colheres?|colherinhas?|chávenas?|xícaras?|"
        r"chá|sopa|unidades?|dentes?|fatias?|"
        r"gramas?|litros?|embalagens?|pacotes?|"
        r"pitadas?|ramos?|latas?)\b"
    )
    return bool(re.search(padrao, texto, re.IGNORECASE))


def listas_depois_do_titulo(titulo):
    listas = []
    for elemento in titulo.find_all_next():
        if elemento.name in CABECALHOS:
            break
        if elemento.name in ["ul", "ol"]:
            itens = []
            for li in elemento.find_all("li", recursive=True):
                texto = texto_limpo(li)
                if texto and texto not in itens:
                    itens.append(texto)
            if itens:
                listas.append(itens)
    return listas


def extrair_ingredientes(sopa, ingredientes_json):
    candidatos = []

    if isinstance(ingredientes_json, list):
        itens = [str(x).strip() for x in ingredientes_json if str(x).strip()]
        if itens:
            candidatos.append(itens)

    for titulo in sopa.find_all(CABECALHOS + ["strong"]):
        nome = texto_limpo(titulo).lower().strip(" :")
        if any(p in nome for p in ["ingrediente", "ingredient"]):
            for lista in listas_depois_do_titulo(titulo):
                candidatos.append(lista)

    if not candidatos:
        return []

    def pontuacao(lista):
        com_quantidades = sum(1 for item in lista if quantidade_no_texto(item))
        # Valoriza listas completas e, sobretudo, ingredientes com quantidades.
        return com_quantidades * 10 + len(lista)

    return max(candidatos, key=pontuacao)


def limpar_passos(passos):
    resultado = []

    def percorrer(elementos):
        if isinstance(elementos, str):
            texto = elementos.strip()
            if texto:
                resultado.append(texto)
        elif isinstance(elementos, list):
            for elemento in elementos:
                percorrer(elemento)
        elif isinstance(elementos, dict):
            if elementos.get("text"):
                percorrer(elementos["text"])
            else:
                for chave in ("itemListElement", "steps"):
                    if elementos.get(chave):
                        percorrer(elementos[chave])

    percorrer(passos)

    # Retira repetições consecutivas.
    finais = []
    for passo in resultado:
        if not finais or passo != finais[-1]:
            finais.append(passo)
    return finais



def extrair_preparacao(sopa, passos_json):
    # Frases típicas de lixo no final das páginas (comentários, captcha, etc.)
    lixo = [
        "gostou desta receita",
        "conte-nos o que achou",
        "avalie esta receita",
        "min. 30 caracteres",
        "this site is protected by recaptcha",
        "google privacy policy",
        "terms of service",
        "deixe o seu comentário",
        "comentários",
        "partilhe a receita",
    ]

    def filtrar_lixo(lista):
        resultado = []
        for item in lista:
            texto_lower = item.lower()
            if any(frase in texto_lower for frase in lixo):
                continue
            # Também para se a linha for muito curta e genérica
            if len(item.strip()) < 8 and not re.search(r"\d", item):
                continue
            resultado.append(item)
        return resultado

    # Primeiro tenta usar os passos estruturados da receita.
    if passos_json:
        passos = limpar_passos(passos_json)
        if len(passos) >= 2:
            passos = [re.sub(r"^\s*\d+[\.\)\-:]\s+", "", p).strip() for p in passos]
            return filtrar_lixo(passos)

    # Procura especificamente a secção de preparação.
    palavras = [
        "confecção", "confecao", "preparação", "preparacao",
        "modo de preparo", "modo de preparação",
        "instruções", "instrucoes", "preparo"
    ]

    for titulo in sopa.find_all(CABECALHOS):
        nome = texto_limpo(titulo).lower().strip(" :")
        if any(p in nome for p in palavras):
            resultado = []
            for elemento in titulo.find_all_next():
                if elemento.name in CABECALHOS:
                    break
                # Para de apanhar conteúdo se encontrar um título de comentários ou similar
                if elemento.name in CABECALHOS:
                    nome_seguinte = texto_limpo(elemento).lower()
                    if any(p in nome_seguinte for p in ["comentário", "comentario", "avaliação", "avaliacao", "feedback"]):
                        break
                if elemento.name in ["ol", "ul"]:
                    for li in elemento.find_all("li", recursive=True):
                        texto = texto_limpo(li)
                        if texto and texto not in resultado:
                            resultado.append(texto)
                elif elemento.name == "li":
                    texto = texto_limpo(elemento)
                    if texto and texto not in resultado:
                        resultado.append(texto)
                elif elemento.name == "p":
                    texto = texto_limpo(elemento)
                    if texto and texto not in resultado:
                        resultado.append(texto)

            if len(resultado) >= 2:
                resultado = [re.sub(r"^\s*\d+[\.\)\-:]\s+", "", p).strip() for p in resultado]
                return filtrar_lixo(resultado)

    return []



def extrair_introducao(sopa):
    # Procura primeiro uma descrição da receita.
    descricao = sopa.find("meta", attrs={"property": "og:description"})
    if descricao and descricao.get("content"):
        texto = descricao["content"].strip()
    else:
        descricao = sopa.find("meta", attrs={"name": "description"})
        texto = descricao.get("content", "").strip() if descricao else ""

    if not texto:
        principal = sopa.find("article") or sopa.find("main") or sopa.body or sopa
        paragrafos = []

        for p in principal.find_all("p"):
            frase = texto_limpo(p)
            if len(frase) >= 45:
                paragrafos.append(frase)

        texto = " ".join(paragrafos[:3])

    texto = re.sub(r"\s+", " ", texto).strip()
    if not texto:
        return "Escreve aqui um breve resumo da receita."

    frases = re.split(r"(?<=[.!?])\s+", texto)
    resumo = ""

    for frase in frases:
        frase = frase.strip()
        if not frase:
            continue
        if len(resumo) + len(frase) > 450:
            break
        resumo += (" " if resumo else "") + frase
        if len(resumo) >= 250:
            break

    return resumo or texto[:450]


def extrair_notas(sopa, notas_json=None):
    if notas_json:
        if isinstance(notas_json, list):
            return "\n".join(str(x) for x in notas_json)
        return str(notas_json)

    palavras = [
        "notas", "sugestões", "sugestoes", "dicas",
        "conselhos", "observações", "observacoes", "notes", "tips"
    ]

    for titulo in sopa.find_all(CABECALHOS + ["strong"]):
        nome = texto_limpo(titulo).lower().strip(" :")
        if any(p in nome for p in palavras):
            resultado = []
            for elemento in titulo.find_all_next():
                if elemento.name in CABECALHOS:
                    break
                if elemento.name in ["p", "li"]:
                    texto = texto_limpo(elemento)
                    if texto and texto not in resultado:
                        resultado.append(texto)
            if resultado:
                return "\n".join(resultado)
    return ""


def markdown_para_reportlab(texto):
    """Converte Markdown simples para HTML aceite pelo ReportLab."""
    if not texto:
        return ""
    texto = str(texto)

    # Escapa caracteres especiais primeiro
    texto = escape(texto)

    # Negrito **texto** ou __texto__
    texto = re.sub(r"\*\*(.+?)\*\*", r"<b>\1</b>", texto)
    texto = re.sub(r"__(.+?)__", r"<b>\1</b>", texto)

    # Itálico *texto* ou _texto_ (depois do negrito para não interferir)
    texto = re.sub(r"(?<!\*)\*(?!\*)(.+?)(?<!\*)\*(?!\*)", r"<i>\1</i>", texto)
    texto = re.sub(r"(?<!_)_(?!_)(.+?)(?<!_)_(?!_)", r"<i>\1</i>", texto)

    # Quebras de linha
    texto = texto.replace("\n", "<br/>")
    return texto


def adicionar_paragrafo(historia, texto, estilo):
    texto = str(texto or "").strip()
    if texto:
        historia.append(Paragraph(markdown_para_reportlab(texto), estilo))
        historia.append(Spacer(1, 0.08 * cm))


def criar_pdf(titulo, resumo, ingredientes, preparacao, notas, fotos, url_fonte="", legendas=None):
    memoria = BytesIO()
    documento = SimpleDocTemplate(
        memoria,
        pagesize=A4,
        rightMargin=1.5 * cm,
        leftMargin=1.5 * cm,
        topMargin=1.4 * cm,
        bottomMargin=1.4 * cm
    )

    estilos = getSampleStyleSheet()
    fonte_titulo = "Times-Italic"
    caminho_segoe_print = r"C:\\Windows\\Fonts\\segoepr.ttf"
    try:
        if os.path.exists(caminho_segoe_print):
            pdfmetrics.registerFont(TTFont("SegoePrintReceita", caminho_segoe_print))
            fonte_titulo = "SegoePrintReceita"
    except Exception:
        pass
    estilos.add(ParagraphStyle(
        name="TituloReceita", parent=estilos["Title"],
        fontName=fonte_titulo, fontSize=23, leading=28,
        textColor="#58745A", alignment=TA_CENTER, spaceAfter=9
    ))
    estilos.add(ParagraphStyle(
        name="SecaoReceita", parent=estilos["Heading2"],
        fontName=fonte_titulo, textColor="#B5654D",
        fontSize=14, leading=17, spaceBefore=10, spaceAfter=10,
        keepWithNext=True
    ))
    estilos.add(ParagraphStyle(
        name="TextoReceita", parent=estilos["BodyText"],
        fontSize=10, leading=13, spaceAfter=1
    ))
    estilos.add(ParagraphStyle(
        name="IngredienteReceita", parent=estilos["TextoReceita"],
        leading=12, spaceAfter=0.5
    ))
    estilos.add(ParagraphStyle(
        name="LegendaFotoReceita", parent=estilos["TextoReceita"],
        fontSize=8, leading=10, alignment=TA_CENTER,
        textColor="#666666", spaceBefore=3, spaceAfter=0
    ))

    largura_util = A4[0] - documento.leftMargin - documento.rightMargin

    def criar_imagem_ajustada(foto, largura_max, altura_max):
        """Mantém as proporções originais da fotografia."""
        imagem_pil = PILImage.open(BytesIO(foto.getvalue()))
        largura_original, altura_original = imagem_pil.size
        if not largura_original or not altura_original:
            return None
        escala = min(
            largura_max / largura_original,
            altura_max / altura_original,
            1.0
        )
        imagem = PDFImage(
            BytesIO(foto.getvalue()),
            width=largura_original * escala,
            height=altura_original * escala
        )
        imagem.hAlign = "CENTER"
        return imagem

    historia = []
    legendas = legendas or []
    historia.append(Paragraph(escape(titulo or "Receita"), estilos["TituloReceita"]))

    # Primeira fotografia: em destaque, mas sem ocupar demasiado espaço.
    if fotos:
        try:
            imagem = criar_imagem_ajustada(fotos[0], min(12.5 * cm, largura_util), 7 * cm)
            if imagem:
                historia.append(imagem)
                legenda = (legendas[0].strip() if len(legendas) > 0 else "")
                if legenda:
                    historia.append(Paragraph(escape(legenda), estilos["LegendaFotoReceita"]))
                historia.append(Spacer(1, 0.2 * cm))
        except Exception:
            pass

    if resumo.strip():
        historia.append(Paragraph("Resumo", estilos["SecaoReceita"]))
        adicionar_paragrafo(historia, resumo, estilos["TextoReceita"])

    historia.append(Paragraph("Ingredientes", estilos["SecaoReceita"]))
    lista_ingredientes = [item.strip() for item in ingredientes.splitlines() if item.strip()]
    metade = (len(lista_ingredientes) + 1) // 2
    colunas_ingredientes = [lista_ingredientes[:metade], lista_ingredientes[metade:]]
    celulas_ingredientes = []
    for coluna in colunas_ingredientes:
        conteudo_coluna = []
        for item in coluna:
            item_original = item.strip()
            # Remove apenas marcadores de lista simples no início
            item_limpo = re.sub(r"^[-•*]\s+", "", item_original).strip()
            if not item_limpo:
                item_limpo = item_original

            # Não adiciona • se:
            # - começa com numeração própria (1), 2., etc.)
            # - contém negrito (**)
            # - está todo em maiúsculas (cabeçalho de secção)
            parece_cabecalho = (
                re.match(r"^\d+[\.\)\-:]", item_limpo)
                or "**" in item_limpo
                or (item_limpo.isupper() and len(item_limpo) > 1)
            )

            if parece_cabecalho:
                texto_final = markdown_para_reportlab(item_limpo)
            else:
                texto_final = "• " + markdown_para_reportlab(item_limpo)

            conteudo_coluna.append(
                Paragraph(texto_final, estilos["IngredienteReceita"])
            )
        celulas_ingredientes.append(conteudo_coluna)
    tabela_ingredientes = Table(
        [[celulas_ingredientes[0], celulas_ingredientes[1]]],
        colWidths=[largura_util / 2, largura_util / 2],
        hAlign="LEFT"
    )
    tabela_ingredientes.setStyle(TableStyle([
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 0),
        ("RIGHTPADDING", (0, 0), (-1, -1), 7),
        ("TOPPADDING", (0, 0), (-1, -1), 0),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
    ]))
    historia.append(tabela_ingredientes)
    historia.append(Spacer(1, 0.08 * cm))

    historia.append(Paragraph("Preparação passo a passo", estilos["SecaoReceita"]))
    passos = [p.strip() for p in preparacao.splitlines() if p.strip()]
    for passo in passos:
        # Respeita totalmente o que o utilizador escreve (numeração, negrito, maiúsculas)
        adicionar_paragrafo(historia, markdown_para_reportlab(passo), estilos["TextoReceita"])

    # Fotografias adicionais: duas por linha, com uma coluna vazia como separador.
    if len(fotos) > 1:
        historia.append(Paragraph("Fotografias", estilos["SecaoReceita"]))
        largura_intervalo = 0.45 * cm
        largura_celula = (largura_util - largura_intervalo) / 2
        imagens = []
        for indice_foto, foto in enumerate(fotos[1:], start=1):
            try:
                imagem = criar_imagem_ajustada(foto, largura_celula, 6 * cm)
                if imagem:
                    legenda = legendas[indice_foto].strip() if len(legendas) > indice_foto else ""
                    imagens.append((imagem, legenda))
            except Exception:
                continue

        for indice in range(0, len(imagens), 2):
            par = imagens[indice:indice + 2]
            # Célula esquerda
            legenda_esq = Paragraph(escape(par[0][1]), estilos["LegendaFotoReceita"]) if par[0][1] else Paragraph("", estilos["LegendaFotoReceita"])
            celula_esquerda = [par[0][0], legenda_esq]

            # Célula direita (ou vazia)
            if len(par) > 1:
                legenda_dir = Paragraph(escape(par[1][1]), estilos["LegendaFotoReceita"]) if par[1][1] else Paragraph("", estilos["LegendaFotoReceita"])
                celula_direita = [par[1][0], legenda_dir]
            else:
                celula_direita = [Paragraph("", estilos["LegendaFotoReceita"]), Paragraph("", estilos["LegendaFotoReceita"])]

            tabela = Table(
                [[celula_esquerda, Paragraph("", estilos["LegendaFotoReceita"]), celula_direita]],
                colWidths=[largura_celula, largura_intervalo, largura_celula],
                hAlign="CENTER"
            )
            tabela.setStyle(TableStyle([
                ("VALIGN", (0, 0), (-1, -1), "TOP"),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 3),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
            ]))
            historia.append(tabela)
            historia.append(Spacer(1, 0.15 * cm))

    # As notas aparecem a seguir, sem começar obrigatoriamente outra página.
    if notas.strip():
        historia.append(Paragraph("Sugestões e notas", estilos["SecaoReceita"]))
        for nota in notas.splitlines():
            nota = nota.strip().lstrip("•-* \t")
            if nota:
                nota = nota[0].upper() + nota[1:]
                adicionar_paragrafo(historia, "• " + nota, estilos["TextoReceita"])

    # Espaço para feedback pessoal.
    historia.append(Paragraph("Feedback", estilos["SecaoReceita"]))
    caixa_feedback = Table([[""]], colWidths=[largura_util], rowHeights=[3.5 * cm])
    caixa_feedback.setStyle(TableStyle([
        ("BOX", (0, 0), (-1, -1), 1, "#9A9A9A"),
        ("BACKGROUND", (0, 0), (-1, -1), "#FFFFFF"),
        ("VALIGN", (0, 0), (-1, -1), "TOP"),
        ("LEFTPADDING", (0, 0), (-1, -1), 6),
        ("RIGHTPADDING", (0, 0), (-1, -1), 6),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    historia.append(caixa_feedback)

    # A fonte fica no fim do documento, depois do espaço de feedback.
    if url_fonte.strip():
        historia.append(Spacer(1, 0.25 * cm))
        historia.append(Paragraph("Fonte da receita", estilos["SecaoReceita"]))
        try:
            site = urlparse(url_fonte).netloc
            if site.startswith("www."):
                site = site[4:]
        except Exception:
            site = url_fonte
        url_seguro = escape(url_fonte, {'"': "&quot;"})
        texto_site = escape(site or url_fonte)
        historia.append(Paragraph(
            f'{texto_site} — <link href="{url_seguro}" color="#3568A8">Abrir receita original</link>',
            estilos["TextoReceita"]
        ))

    documento.build(historia)
    memoria.seek(0)
    return memoria.getvalue()



url = st.text_input("Link da receita", key="url_receita")

if st.button("Extrair receita"):
    if not url.startswith(("http://", "https://")):
        st.error("Introduz um link válido, começando por https://")
    else:
        try:
            resposta = requests.get(
                url,
                headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/130.0 Safari/537.36"},
                timeout=25
            )
            resposta.raise_for_status()
            sopa = BeautifulSoup(resposta.text, "html.parser")

            receita = None
            for bloco in sopa.find_all("script", type="application/ld+json"):
                try:
                    dados = json.loads(bloco.string or bloco.get_text())
                    receita = procurar_receita(dados)
                    if receita:
                        break
                except (json.JSONDecodeError, TypeError):
                    continue

            nome = ""
            ingredientes_json = []
            passos_json = []
            notas_json = None

            if receita:
                nome = receita.get("name", "")
                ingredientes_json = receita.get("recipeIngredient", [])
                passos_json = receita.get("recipeInstructions", [])
                if receita.get("description"):
                    st.session_state["resumo"] = str(receita["description"])
                notas_json = receita.get("recipeNotes")

            if not nome:
                h1 = sopa.find("h1")
                if h1:
                    nome = texto_limpo(h1)

            ingredientes = extrair_ingredientes(sopa, ingredientes_json)
            passos = extrair_preparacao(sopa, passos_json)
            resumo = extrair_introducao(sopa)
            notas = extrair_notas(sopa, notas_json)

            if nome:
                st.session_state["titulo"] = nome
            if ingredientes:
                st.session_state["ingredientes"] = "\n".join(ingredientes)
            if passos:
                # Limpa numeração, remove lixo e evita duplicados
                lixo = [
                    "gostou desta receita", "conte-nos o que achou", "avalie esta receita",
                    "min. 30 caracteres", "this site is protected by recaptcha",
                    "google privacy policy", "terms of service", "deixe o seu comentário"
                ]
                passos_limpos = []
                for p in passos:
                    p_limpo = re.sub(r"^\s*\d+[\.\)\-:]\s+", "", p).strip()
                    if not p_limpo:
                        continue
                    if any(frase in p_limpo.lower() for frase in lixo):
                        continue
                    if p_limpo not in passos_limpos:
                        passos_limpos.append(p_limpo)
                st.session_state["preparacao"] = "\n".join(passos_limpos)
            if resumo:
                st.session_state["resumo"] = resumo
            if notas:
                st.session_state["notas"] = notas

            if nome and (ingredientes or passos):
                st.success("Receita extraída. Revê os campos antes de criar o PDF.")
            else:
                st.warning("Não consegui identificar todos os dados. Preenche ou corrige os campos manualmente.")

        except Exception as erro:
            st.error(f"Não foi possível ler esta página: {erro}")


st.subheader("Dados da receita")
st.caption(
    "Podes editar livremente todos os campos. "
    "Formatação suportada: **negrito**, *itálico*. "
    "Maiúsculas e numeração são respeitadas. "
    "Nos ingredientes, linhas em MAIÚSCULAS ou a **negrito** não levam marca •."
)

titulo = st.text_input("Nome da receita", key="titulo")

resumo = st.text_area(
    "Resumo da introdução",
    key="resumo",
    height=110,
    help="Podes usar **negrito** e *itálico*."
)

ingredientes = st.text_area(
    "Ingredientes e quantidades (um por linha)",
    key="ingredientes",
    height=240,
    help="Um por linha. Linhas em MAIÚSCULAS, a **negrito** ou com numeração (1) ...) "
         "não levam marca •. As outras levam. Podes usar **negrito** e *itálico*."
)

preparacao = st.text_area(
    "Preparação passo a passo (um passo por linha)",
    key="preparacao",
    height=280,
    help="Um passo por linha. A numeração não é automática — escreve tu os números se quiseres. "
         "Podes usar **negrito**, *itálico* e MAIÚSCULAS livremente."
)

notas = st.text_area(
    "Sugestões e notas",
    key="notas",
    height=140,
    help="Um ponto por linha. Podes usar **negrito** e *itálico*."
)

fotos = st.file_uploader(
    "Adicionar 1 a 6 fotografias",
    type=["jpg", "jpeg", "png", "webp", "gif", "bmp", "tiff"],
    accept_multiple_files=True
)

legendas_fotos = []
if fotos:
    st.markdown("**Legendas das fotografias**")
    st.caption("Escreve uma descrição curta para cada fotografia. Por exemplo: «Molho de natas».")
    for indice_foto, foto in enumerate(fotos):
        legenda = st.text_input(
            f"Legenda da fotografia {indice_foto + 1}",
            key=f"legenda_foto_{indice_foto}",
            placeholder="Ex.: Molho de natas"
        )
        legendas_fotos.append(legenda)

if fotos and len(fotos) > 6:
    st.warning("Seleciona no máximo 6 fotografias.")

if st.button("Criar PDF"):
    if not titulo.strip():
        st.error("Indica o nome da receita.")
    elif not ingredientes.strip() or not preparacao.strip():
        st.error("Preenche os ingredientes e a preparação antes de criar o PDF.")
    elif fotos and len(fotos) > 6:
        st.error("Seleciona no máximo 6 fotografias.")
    else:
        try:
            pdf = criar_pdf(
                titulo, resumo, ingredientes, preparacao, notas,
                fotos if fotos else [], url, legendas_fotos
            )
            nome_ficheiro = re.sub(
                r'[\\/*?:"<>|]', "", titulo
            ).strip().replace(" ", "_")
            st.download_button(
                "Descarregar PDF",
                data=pdf,
                file_name=f"{nome_ficheiro or 'receita'}.pdf",
                mime="application/pdf"
            )
            st.success("PDF criado. Clica em «Descarregar PDF».")
        except Exception as erro:
            st.error(f"Não foi possível criar o PDF: {erro}")
