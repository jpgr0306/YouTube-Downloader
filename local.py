"""Roda o mesmo site no seu PC: http://localhost:8000
(serve o index.html e a mesma funcao api/baixar.py que roda na Vercel)."""
import importlib
import os
import sys
import threading
import webbrowser

import uvicorn

RAIZ = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(RAIZ, "api"))
import baixar  # noqa: E402

PORTA = 8000


async def app(scope, receive, send):
    if scope["type"] != "http":
        return
    if scope["path"].startswith("/api/baixar"):
        # recarrega a cada pedido: se o codigo mudar, nao precisa reabrir a janela
        return await importlib.reload(baixar).app(scope, receive, send)
    with open(os.path.join(RAIZ, "index.html"), "rb") as f:
        corpo = f.read()
    await send({"type": "http.response.start", "status": 200,
                "headers": [(b"content-type", b"text/html; charset=utf-8")]})
    await send({"type": "http.response.body", "body": corpo})


if __name__ == "__main__":
    print("Site rodando em http://localhost:%d  (feche esta janela para parar)" % PORTA)
    threading.Timer(1.5, lambda: webbrowser.open("http://localhost:%d" % PORTA)).start()
    uvicorn.run(app, host="127.0.0.1", port=PORTA, log_level="warning")
