"""NEON AIR - PWA de calidad del aire del Valle de Aburra sobre datos SIATA.

Servidor Flask: sirve la PWA y la API REST del pipeline ETL.
Arranque local:      python app.py
Arranque en EC2:     gunicorn -w 2 -b 0.0.0.0:8000 app:app
"""
from __future__ import annotations

import logging

from flask import Flask, jsonify, render_template, send_from_directory

import config
from api import api
from etl import load, pipeline

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
log = logging.getLogger("neon-air")


def create_app() -> Flask:
    app = Flask(__name__, static_folder="static", template_folder="templates")
    app.json.sort_keys = False  # el orden de las claves cuenta para leer la API
    app.register_blueprint(api)

    @app.get("/")
    def index():
        return render_template("index.html", app_name=config.APP_NAME,
                               version=config.APP_VERSION)

    # El service worker debe servirse desde la raiz para controlar todo el scope.
    @app.get("/sw.js")
    def service_worker():
        response = send_from_directory("static", "sw.js", mimetype="application/javascript")
        response.headers["Cache-Control"] = "no-cache"
        response.headers["Service-Worker-Allowed"] = "/"
        return response

    @app.get("/manifest.webmanifest")
    def manifest():
        return send_from_directory(
            "static", "manifest.webmanifest", mimetype="application/manifest+json"
        )

    @app.errorhandler(404)
    def not_found(_):
        return jsonify({"error": "Recurso no encontrado."}), 404

    @app.errorhandler(500)
    def server_error(exc):
        log.exception("Error interno", exc_info=exc)
        return jsonify({"error": "Error interno del servidor."}), 500

    load.init_db()
    # El ETL se precalienta en segundo plano: el primer usuario no espera el
    # procesamiento completo del historico.
    pipeline.ensure_started("auto")
    return app


app = create_app()


if __name__ == "__main__":
    log.info("NEON AIR escuchando en http://0.0.0.0:%s", config.PORT)
    app.run(host="0.0.0.0", port=config.PORT, debug=False)
