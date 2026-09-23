"""Ejecuta el pipeline ETL desde la terminal (util para cron en la EC2).

    python scripts/run_etl.py                 # auto: vivo y si falla, archivo
    python scripts/run_etl.py --source file   # solo el historico de clase
    python scripts/run_etl.py --benchmark     # ademas compara los 4 metodos
    python scripts/run_etl.py --json          # salida en JSON para pipes

Cron cada 30 min en la instancia:
    */30 * * * * cd /opt/neon-air && .venv/bin/python scripts/run_etl.py --source live >> /var/log/neon-air-etl.log 2>&1
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from etl import load, pipeline  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description="Pipeline ETL de calidad del aire SIATA")
    parser.add_argument("--source", choices=("auto", "live", "file"), default="auto")
    parser.add_argument("--benchmark", action="store_true",
                        help="compara Linear/Cubic/Nearest/Ensamble ocultando datos reales")
    parser.add_argument("--json", action="store_true", help="imprime el reporte como JSON")
    parser.add_argument("--quiet", action="store_true")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.WARNING if args.quiet else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    load.init_db()
    try:
        state = pipeline.run_pipeline(args.source, with_benchmark=args.benchmark)
    except Exception as exc:
        print(f"ETL fallido: {exc}", file=sys.stderr)
        return 1

    report = state["report"]
    if args.json:
        print(json.dumps(
            {
                "run_id": state["run_id"],
                "source": state["ingest"]["source"],
                "realtime": state["ingest"].get("realtime"),
                "duration_s": state["duration_s"],
                "report": report,
                "benchmark": state["benchmark"]["summary"] if state["benchmark"] else None,
            },
            default=str,
        ))
        return 0

    print(f"\n  RUN #{state['run_id']}  ·  {state['ingest']['source']}  ·  {state['duration_s']} s")
    print(f"  ventana        {report['window']['start']} -> {report['window']['end']}")
    print(f"  estaciones     {report['stations']}")
    print(f"  registros      {report['rows']:,}")
    print("  --- calidad del dato ---")
    print(f"  observados     {report['observed']:,}  ({report['availability_raw_pct']}% crudo)")
    print(f"  nulos origen   {report['missing_source']:,}")
    print(f"  centinelas     {report['sentinels']:,}")
    print(f"  fuera de rango {report['out_of_range']:,}")
    print(f"  bandera mala   {report['bad_quality']:,}")
    print(f"  outliers       {report['outliers']:,}")
    print(f"  huecos         {report['gaps']:,}")
    print("  --- imputacion ---")
    print(f"  imputados      {report['imputed']:,}  (ensamble linear+cubic+nearest)")
    print(f"  sin recuperar  {report['unresolved']:,}")
    print(f"  disponibilidad {report['availability_raw_pct']}% -> {report['availability_final_pct']}%")
    print(f"  indicador      {report['indicator']}")

    if state["benchmark"]:
        print("\n  --- comparacion de metodos (MAE menor es mejor) ---")
        print(f"  {'metodo':<10} {'MAE':>8} {'RMSE':>8} {'MAPE%':>8} {'R2':>8} {'imposibles%':>12}")
        for row in state["benchmark"]["summary"]:
            print(f"  {row['method']:<10} {row['mae']:>8.3f} {row['rmse']:>8.3f} "
                  f"{(row['mape'] or 0):>8.2f} {(row['r2'] or 0):>8.3f} {row['impossible_pct']:>12.2f}")
        print(f"  ganador: {state['benchmark']['winner']}")

    print(f"\n  almacen: {load.db_stats()}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
