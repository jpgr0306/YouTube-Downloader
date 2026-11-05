"""Funcao da Vercel: recebe ?url=..., o yt-dlp descobre os links do video e do
audio (so pelo ID), o ffmpeg puxa os dois do YouTube, junta na hora e o resultado
vai saindo em pedacos pro navegador. Nada e salvo no disco do servidor.
Resposta em linhas: L: log, F: nome do arquivo, B: pedaco em base64, E: fim."""
import asyncio
import base64
import json
import os
import queue
import re
import shutil
import subprocess
import tempfile
import stat
import sys
import threading
import urllib.parse
import urllib.request
import zipfile

import yt_dlp

TMP = tempfile.gettempdir()
DENO_ZIP = "https://github.com/denoland/deno/releases/latest/download/deno-x86_64-unknown-linux-gnu.zip"


def executavel(caminho):
    """Garante permissao de execucao; se a pasta for so leitura, copia pra /tmp."""
    if not caminho or not os.path.isfile(caminho):
        return None
    if os.access(caminho, os.X_OK):
        return caminho
    copia = os.path.join(TMP, os.path.basename(caminho))
    if not os.path.isfile(copia):
        shutil.copy(caminho, copia)
    os.chmod(copia, os.stat(copia).st_mode | stat.S_IEXEC)
    return copia


def achar_deno(log):
    """O yt-dlp precisa do deno pro YouTube. No PC vem do pip; na Vercel a pasta
    bin/ do pip nao e enviada, entao baixa o binario oficial pra /tmp (1x por servidor)."""
    try:
        import deno
        achado = executavel(deno.find_deno_bin())
        if achado:
            return achado
    except Exception:
        pass
    destino = os.path.join(TMP, "deno")
    if os.path.isfile(destino):
        return destino
    if not sys.platform.startswith("linux"):
        return None
    try:
        log("Baixando o deno (só na primeira vez neste servidor)...")
        zip_path = destino + ".zip"
        urllib.request.urlretrieve(DENO_ZIP, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extract("deno", TMP)
        os.remove(zip_path)
        os.chmod(destino, 0o755)
        return destino
    except Exception as e:
        log("AVISO: não consegui baixar o deno: %s" % e)
        return None


def achar_ffmpeg():
    try:
        import imageio_ffmpeg  # binario do ffmpeg via pip; junta video + audio
        return executavel(imageio_ffmpeg.get_ffmpeg_exe())
    except Exception:
        return None


def cabecalho_para_netscape(cabecalho):
    """Converte o cabecalho 'cookie' copiado do Inspecionar (nome=valor; nome2=valor2)
    pro formato de arquivo que o yt-dlp entende."""
    linhas = ["# Netscape HTTP Cookie File"]
    for parte in cabecalho.split(";"):
        nome, igual, valor = parte.strip().partition("=")
        if not igual or not nome:
            continue
        linhas.append("\t".join([".youtube.com", "TRUE", "/", "TRUE", "2147483647", nome, valor]))
    return "\n".join(linhas)


def arquivo_cookies():
    """Cookies do YouTube vindos da variavel YOUTUBE_COOKIES (painel da Vercel).
    Aceita o arquivo cookies.txt (Netscape) ou o cabecalho 'cookie' do Inspecionar.
    Evita o 'Sign in to confirm you're not a bot'."""
    conteudo = os.environ.get("YOUTUBE_COOKIES", "").strip()
    if not conteudo:
        return None
    # aceita o arquivo colado normal ou com \n e \t escritos como texto
    conteudo = conteudo.replace("\\n", "\n").replace("\\t", "\t")
    if conteudo.lower().startswith("cookie:"):
        conteudo = conteudo[7:].strip()
    if "\t" not in conteudo:  # nao e arquivo Netscape: e o cabecalho do Inspecionar
        conteudo = cabecalho_para_netscape(conteudo)
    caminho = os.path.join(TMP, "youtube_cookies.txt")
    with open(caminho, "w", encoding="utf-8") as f:
        f.write(conteudo + "\n")
    return caminho

# qualidades da pagina (altura maxima do video, ou so audio)
QUALIDADES = ["melhor", "2160", "1440", "1080", "720", "480", "360", "mp3"]
# so audio: melhor audio separado; se o YouTube so oferecer video+audio juntos
# (acontece com cookies), pega o mais leve ate 360p, que ja tem audio de 128 kbps,
# em vez de baixar um video 1080p inteiro so pra tirar o som
# audio "DRC" = versao com volume comprimido pelo YouTube: so usa se nao tiver a normal
SEM_DRC = "[format_id!*=drc]"
AUDIO = {"mp3": "ba%s/ba/b[height<=360][acodec!=none]/w[acodec!=none]/b" % SEM_DRC}
MB = 1024 * 1024
BLOCO = 3 * 256 * 1024  # tamanho de cada pedaco mandado pro navegador


def montar_formato(qualidade):
    """Formato do yt-dlp: melhor video + melhor audio (o limite de qualidade fica na ordenacao)."""
    if qualidade in AUDIO:
        return AUDIO[qualidade]
    return "bv*+ba{d}/bv*+ba/b/b*".format(d=SEM_DRC)


def ordenacao(qualidade):
    """Ordem de preferencia dos formatos:
    1. audio no idioma ORIGINAL (o YouTube tem dublagens automaticas em ingles, espanhol...;
       nos EUA, onde fica a Vercel, o padrao costuma ser o ingles);
    2. resolucao: "res" e o lado MENOR do video, igual o YouTube chama "720p" -- assim
       Shorts (em pe) tambem funcionam. "res:720" = o maior que nao passa de 720
       (se nao tiver nenhum, o menor acima);
    3. na mesma resolucao, o maior bitrate (menos pixelado)."""
    res = "res" if qualidade in ("melhor",) or qualidade in AUDIO else "res:" + qualidade
    return ["lang", res, "br", "ext:mp4:m4a"]


def normalizar_url(texto):
    """Devolve (url, e_youtube) ou None.
    YouTube: so o ID do video (ignora playlist, &si=, &t= etc.).
    Outros sites (Instagram, TikTok, Facebook...): o link como veio."""
    texto = texto.strip()
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", texto):
        return "https://www.youtube.com/watch?v=" + texto, True
    if re.search(r"(?:^|[/.])(?:youtube\.com|youtu\.be|youtube-nocookie\.com)", texto, re.I):
        m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", texto)
        return ("https://www.youtube.com/watch?v=" + m.group(1), True) if m else None
    if not re.match(r"https?://", texto, re.I) and re.match(r"[\w-]+(\.[\w-]+)+/", texto):
        texto = "https://" + texto  # colou sem o https://
    if re.match(r"https?://[\w-]+(\.[\w-]+)+(/|$)", texto, re.I) and " " not in texto:
        return texto, False
    return None


class Log:
    def __init__(self, q):
        self.q = q

    def debug(self, msg):
        if not msg.startswith("[debug] "):
            self.q.put(("L", msg))

    info = debug

    def warning(self, msg):
        self.q.put(("L", "AVISO: " + msg))

    def error(self, msg):
        self.q.put(("L", msg))


def repassar(fluxo, q, erros=None):
    """Le um stderr linha a linha: progresso/log vai pra tela, erros ficam guardados."""
    for bruto in fluxo:
        txt = bruto.decode("utf-8", "replace").strip()
        if not txt:
            continue
        if erros is not None:
            erros.append(txt)
        else:
            q.put(("L", txt))


def baixar(url, e_youtube, qualidade, pasta, q):
    """Roda em outra thread: manda ("L", log), ("F", nome), ("B", bytes) e ("FIM", codigo).

    1. yt-dlp (aqui mesmo) so descobre os formatos e salva as infos num .json pequeno.
    2. O audio (pequeno) e baixado pro /tmp.
    3. O video e baixado por um yt-dlp separado escrevendo na saida padrao, que vai
       direto (pipe) pro ffmpeg; o ffmpeg junta com o audio e escreve MP4 fragmentado
       na saida dele, que vai saindo em pedacos pro navegador. O video nunca vai pro disco.
    """
    opts = {
        # so sites que o yt-dlp conhece; o extrator "generico" (qualquer pagina) fica
        # desligado pra ninguem usar o servidor como proxy pra endereco qualquer
        "allowed_extractors": ["default", "-generic"],
        "playlistend": 1,  # link com varios videos (carrossel do Instagram etc.): so o 1o
        "format_sort": ordenacao(qualidade),
        "format": montar_formato(qualidade),
        "noplaylist": True,
        "logger": Log(q),
        "cachedir": os.path.join(TMP, "yt-dlp-cache"),
        "color": {"stdout": "no_color", "stderr": "no_color"},
    }
    # deno e cookies so fazem sentido pro YouTube
    deno_bin = achar_deno(lambda m: q.put(("L", m))) if e_youtube else None
    if deno_bin:
        opts["js_runtimes"] = {"deno": {"path": deno_bin}}
    cookies = arquivo_cookies() if e_youtube else None
    if cookies:
        opts["cookiefile"] = cookies
        q.put(("L", "Usando cookies do YouTube (variável YOUTUBE_COOKIES)."))
    elif e_youtube and os.environ.get("VERCEL"):
        q.put(("L", "AVISO: variável YOUTUBE_COOKIES não encontrada neste deploy "
                    "(confira o nome, o ambiente Production e faça Redeploy)."))
    ffmpeg = achar_ffmpeg()
    if not ffmpeg:
        q.put(("L", "ERRO: ffmpeg não encontrado."))
        q.put(("FIM", 1))
        return

    try:
        q.put(("L", "> yt-dlp -f \"%s\" %s" % (opts["format"], url)))
        with yt_dlp.YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)  # so descobre os formatos
            if info.get("_type") in ("playlist", "multi_video"):
                # carrossel / post com varios videos: pega o primeiro que tem formato
                videos = [e for e in (info.get("entries") or []) if e and e.get("format_id")]
                if not videos:
                    raise RuntimeError("esse link não tem vídeo pra baixar")
                q.put(("L", "Esse link tem mais de um vídeo; baixando o primeiro."))
                info = videos[0]
            info_json = os.path.join(pasta, "info.json")
            with open(info_json, "w", encoding="utf-8") as f:
                json.dump(ydl.sanitize_info(info), f)
        formatos = info.get("requested_formats") or [info]
        q.put(("L", "Formato: " + " + ".join(
            "%s (%s)" % (f.get("format_id"), f.get("format_note") or f.get("resolution") or "?")
            for f in formatos)))

        # yt-dlp separado que baixa um formato usando as infos ja descobertas
        env = dict(os.environ, PYTHONPATH=os.pathsep.join(p for p in sys.path if p))
        # -N 8 baixa 8 pedacos por vez; cada pedaco fica um instante num arquivo
        # temporario na pasta atual -> por isso os processos rodam com cwd=pasta (/tmp),
        # ja que a pasta do codigo na Vercel e so leitura
        base = [sys.executable, "-m", "yt_dlp", "--load-info-json", info_json, "--newline",
                "--no-part", "--no-color", "-N", "8",
                "--abort-on-unavailable-fragments", "--cache-dir", os.path.join(TMP, "yt-dlp-cache")]
        if cookies:
            base += ["--cookies", cookies]
        if deno_bin:
            base += ["--js-runtimes", "deno:" + deno_bin]

        so_audio = qualidade in AUDIO
        principal = formatos[0]
        audio_arquivo = None
        if len(formatos) == 2:
            # baixa o audio primeiro (e pequeno)
            q.put(("L", "Baixando o áudio..."))
            r = subprocess.run(base + ["-f", formatos[1]["format_id"], "-o", os.path.join(pasta, "audio.%(ext)s")],
                               stdin=subprocess.DEVNULL, capture_output=True, env=env, cwd=pasta)
            achados = [a for a in os.listdir(pasta) if a.startswith("audio.")]
            if r.returncode != 0 or not achados:
                for e in r.stderr.decode("utf-8", "replace").splitlines()[-5:]:
                    q.put(("L", e))
                raise RuntimeError("falha ao baixar o áudio")
            audio_arquivo = os.path.join(pasta, achados[0])

        frag = ["-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof"]
        # formatos HLS com video+audio juntos vem em MPEG-TS, com AAC no formato ADTS:
        # pra caber no MP4 precisa desse filtro (so quando o audio e AAC)
        aac = ["-bsf:a", "aac_adtstoasc"] if (principal.get("acodec") or "").startswith("mp4a") else []
        cmd = [ffmpeg, "-hide_banner", "-loglevel", "error", "-i", "pipe:0"]
        if qualidade == "mp3":
            cmd += ["-vn", "-c:a", "libmp3lame", "-b:a", "320k", "-f", "mp3"]
            ext = "mp3"
        elif audio_arquivo:
            cmd += ["-i", audio_arquivo, "-map", "0:v:0", "-map", "1:a:0", "-c", "copy"] + frag
            ext = "mp4"
        else:
            cmd += ["-c", "copy"] + aac + frag
            # sites so de audio (SoundCloud, Bandcamp...): .m4a em vez de .mp4
            # (vcodec None = desconhecido, pode ter video; so "none" e certeza de que nao tem)
            ext = "m4a" if principal.get("vcodec") == "none" else "mp4"
        cmd.append("pipe:1")

        ident = info.get("id") or "video"
        nome = "%s [%s].%s" % (yt_dlp.utils.sanitize_filename(info.get("title") or ident)[:150], ident, ext)
        acao = ("Baixando o áudio e convertendo pra MP3" if so_audio else
                "Baixando o vídeo e juntando com o áudio" if audio_arquivo else "Baixando o vídeo")
        q.put(("L", acao + " - vai direto pro navegador, sem salvar no servidor..."))
        q.put(("F", nome))

        baixador = subprocess.Popen(base + ["-f", principal["format_id"], "-o", "-"],
                                    stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, env=env, cwd=pasta)
        juntador = subprocess.Popen(cmd, stdin=baixador.stdout, stdout=subprocess.PIPE,
                                    stderr=subprocess.PIPE, cwd=pasta)
        baixador.stdout.close()  # agora so o ffmpeg le esse pipe
        erros = []
        t1 = threading.Thread(target=repassar, args=(baixador.stderr, q), daemon=True)
        t2 = threading.Thread(target=repassar, args=(juntador.stderr, q, erros), daemon=True)
        t1.start()
        t2.start()
        while True:
            pedaco = juntador.stdout.read(BLOCO)
            if not pedaco:
                break
            q.put(("B", pedaco))
        codigo = juntador.wait() or baixador.wait()
        t1.join(timeout=5)
        t2.join(timeout=5)
        for e in erros[-10:]:
            q.put(("L", "ffmpeg: " + e))
    except Exception as e:
        q.put(("L", "ERRO: %s" % e))
        codigo = 1
    q.put(("FIM", codigo))


INDEX = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "index.html")


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    if not scope["path"].startswith("/api/baixar"):
        # qualquer outro endereco: devolve a pagina
        with open(INDEX, "rb") as f:
            corpo = f.read()
        await send({"type": "http.response.start", "status": 200,
                    "headers": [(b"content-type", b"text/html; charset=utf-8")]})
        await send({"type": "http.response.body", "body": corpo})
        return
    qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
    link = normalizar_url(qs.get("url", [""])[0])
    qualidade = qs.get("q", ["melhor"])[0]
    if qualidade not in QUALIDADES:
        qualidade = "melhor"

    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"cache-control", b"no-store")]})

    async def linha(txt):
        await send({"type": "http.response.body", "body": (txt + "\n").encode(), "more_body": True})

    if not link:
        await linha("L:Link inválido. Cole o link de um vídeo (YouTube, Instagram, TikTok...).")
        await send({"type": "http.response.body", "body": b""})
        return

    pasta = tempfile.mkdtemp()  # so pro audio e o .json de infos
    q = queue.Queue(maxsize=32)  # limita a memoria se o navegador for mais lento que o download
    threading.Thread(target=baixar, args=(link[0], link[1], qualidade, pasta, q), daemon=True).start()
    loop = asyncio.get_running_loop()
    enviado = 0
    try:
        while True:
            tipo, valor = await loop.run_in_executor(None, q.get)
            if tipo == "FIM":
                break
            if tipo == "B":
                enviado += len(valor)
                await linha("B:" + base64.b64encode(valor).decode())
            elif tipo == "F":
                await linha("F:" + valor)
            else:
                for parte in str(valor).splitlines():
                    await linha("L:" + parte)
        if valor == 0 and enviado:
            await linha("L:Pronto! %.1f MB enviados." % (enviado / MB))
            await linha("E:")
        else:
            await linha("L:Deu erro (código %s)." % valor)
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
        await send({"type": "http.response.body", "body": b""})
