"""Funcao da Vercel: recebe ?url=..., roda o yt-dlp so com o ID do video e
devolve em streaming as linhas de log (L:) e depois o arquivo em base64 (B:)."""
import asyncio
import base64
import os
import queue
import re
import shutil
import tempfile
import threading
import urllib.parse

import yt_dlp

try:
    import deno  # binario do deno via pip; o yt-dlp precisa dele pro YouTube
    DENO = deno.find_deno_bin()
except Exception:
    DENO = None


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


def baixar(vid, pasta, q):
    opts = {
        "outtmpl": os.path.join(pasta, "%(title)s [%(id)s].%(ext)s"),
        # sem ffmpeg na Vercel: pega um formato que ja vem com video + audio juntos
        "format": "best[ext=mp4][vcodec!=none][acodec!=none]/best[vcodec!=none][acodec!=none]",
        "noplaylist": True,
        "logger": Log(q),
        "progress_with_newline": True,
        "cachedir": os.path.join(tempfile.gettempdir(), "yt-dlp-cache"),
        "color": {"stdout": "no_color", "stderr": "no_color"},
    }
    if DENO:
        opts["js_runtimes"] = {"deno": {"path": DENO}}
    try:
        q.put(("L", "> yt-dlp https://www.youtube.com/watch?v=" + vid))
        with yt_dlp.YoutubeDL(opts) as ydl:
            codigo = ydl.download(["https://www.youtube.com/watch?v=" + vid])
    except Exception as e:  # erro do yt-dlp ja foi logado; garante que termina
        q.put(("L", "ERRO: %s" % e))
        codigo = 1
    q.put(("FIM", codigo))


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    qs = urllib.parse.parse_qs(scope.get("query_string", b"").decode())
    vid = extrair_id(qs.get("url", [""])[0])

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
    threading.Thread(target=baixar, args=(vid, pasta, q), daemon=True).start()
    loop = asyncio.get_running_loop()
    try:
        while True:
            tipo, valor = await loop.run_in_executor(None, q.get)
            if tipo == "FIM":
                break
            for parte in str(valor).splitlines():
                await linha("L:" + parte)

        arquivos = [f for f in os.listdir(pasta) if not f.endswith((".part", ".ytdl"))]
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
