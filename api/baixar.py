"""Funcao da Vercel: recebe ?url=..., roda o yt-dlp so com o ID do video e
devolve em streaming as linhas de log (L:) e depois o arquivo em base64 (B:)."""
import asyncio
import base64
import os
import queue
import re
import shutil
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
        log("Baixando o deno (so na primeira vez neste servidor)...")
        zip_path = destino + ".zip"
        urllib.request.urlretrieve(DENO_ZIP, zip_path)
        with zipfile.ZipFile(zip_path) as z:
            z.extract("deno", TMP)
        os.remove(zip_path)
        os.chmod(destino, 0o755)
        return destino
    except Exception as e:
        log("AVISO: nao consegui baixar o deno: %s" % e)
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

# qualidade escolhida na pagina -> formato do yt-dlp
VIDEO = "bv*[height<={h}]+ba/b[height<={h}]"
QUALIDADES = {
    "melhor": "bv*+ba/b",
    "2160": VIDEO.format(h=2160),
    "1440": VIDEO.format(h=1440),
    "1080": VIDEO.format(h=1080),
    "720": VIDEO.format(h=720),
    "480": VIDEO.format(h=480),
    "360": VIDEO.format(h=360),
    "mp3": "ba/b",
    "m4a": "ba[ext=m4a]/ba/b",
}
# sem ffmpeg so da pra baixar formatos que ja vem com video + audio juntos
SEM_FFMPEG = "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]"


def extrair_id(texto):
    texto = texto.strip()
    m = re.search(r"(?:v=|youtu\.be/|shorts/|embed/|live/)([A-Za-z0-9_-]{11})", texto)
    if m:
        return m.group(1)
    if re.fullmatch(r"[A-Za-z0-9_-]{11}", texto):
        return texto
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


def baixar(vid, qualidade, pasta, q):
    opts = {
        "outtmpl": "%(title)s [%(id)s].%(ext)s",
        # tudo (inclusive arquivos de teste) na pasta temporaria; na Vercel so /tmp aceita escrita
        "paths": {"home": pasta, "temp": pasta},
        "format": QUALIDADES[qualidade],
        # na mesma resolucao, pega o maior bitrate (menos pixelado); o YouTube
        # costuma ter versoes 1080p de 768 kbps ate 3500 kbps ("Premium")
        "format_sort": ["res", "br", "ext:mp4:m4a"],
        "merge_output_format": "mp4",
        "concurrent_fragment_downloads": 8,  # formatos melhores vem em varios pedacos
        "noplaylist": True,
        "logger": Log(q),
        "progress_with_newline": True,
        "cachedir": os.path.join(tempfile.gettempdir(), "yt-dlp-cache"),
        "color": {"stdout": "no_color", "stderr": "no_color"},
    }
    deno_bin = achar_deno(lambda m: q.put(("L", m)))
    ffmpeg_bin = achar_ffmpeg()
    if deno_bin:
        opts["js_runtimes"] = {"deno": {"path": deno_bin}}
    cookies = arquivo_cookies()
    if cookies:
        opts["cookiefile"] = cookies
        q.put(("L", "Usando cookies do YouTube (variavel YOUTUBE_COOKIES)."))
    if ffmpeg_bin:
        opts["ffmpeg_location"] = ffmpeg_bin
        if qualidade == "mp3":
            opts["postprocessors"] = [{"key": "FFmpegExtractAudio",
                                       "preferredcodec": "mp3", "preferredquality": "0"}]
    else:
        q.put(("L", "AVISO: ffmpeg nao encontrado, baixando a melhor qualidade sem precisar juntar (geralmente 360p)."))
        opts["format"] = SEM_FFMPEG
    try:
        q.put(("L", "> yt-dlp -f \"%s\" https://www.youtube.com/watch?v=%s" % (opts["format"], vid)))
        with yt_dlp.YoutubeDL(opts) as ydl:
            codigo = ydl.download(["https://www.youtube.com/watch?v=" + vid])
    except Exception as e:  # erro do yt-dlp ja foi logado; garante que termina
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
    qs =urllib.parse.parse_qs(scope.get("query_string", b"").decode())
    vid = extrair_id(qs.get("url", [""])[0])
    qualidade = qs.get("q", ["melhor"])[0]
    if qualidade not in QUALIDADES:
        qualidade = "melhor"

    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/plain; charset=utf-8"),
                            (b"cache-control", b"no-store")]})

    async def linha(txt):
        await send({"type": "http.response.body", "body": (txt + "\n").encode(), "more_body": True})

    if not vid:
        await linha("L:Link invalido. Cole um link do YouTube.")
        await send({"type": "http.response.body", "body": b""})
        return

    pasta = tempfile.mkdtemp()
    q = queue.Queue()
    threading.Thread(target=baixar, args=(vid, qualidade, pasta, q), daemon=True).start()
    loop = asyncio.get_running_loop()
    try:
        while True:
            tipo, valor = await loop.run_in_executor(None, q.get)
            if tipo == "FIM":
                break
            for parte in str(valor).splitlines():
                await linha("L:" + parte)

        # ignora pedacos que sobram do download (.part, .f137.mp4 etc.)
        arquivos = [f for f in os.listdir(pasta)
                    if not f.endswith((".part", ".ytdl")) and not re.search(r"\.f\d+\.\w+$", f)]
        if valor != 0 or not arquivos:
            await linha("L:Deu erro (codigo %s)." % valor)
        else:
            nome = arquivos[0]
            await linha("L:Pronto! Enviando o arquivo pro seu navegador...")
            await linha("F:" + nome)
            with open(os.path.join(pasta, nome), "rb") as f:
                while True:
                    bloco = f.read(3 * 256 * 1024)
                    if not bloco:
                        break
                    await linha("B:" + base64.b64encode(bloco).decode())
            await linha("E:")
    finally:
        shutil.rmtree(pasta, ignore_errors=True)
        await send({"type": "http.response.body", "body": b""})
