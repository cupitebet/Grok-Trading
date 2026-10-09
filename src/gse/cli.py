import argparse
import logging
import math
import sys
from pathlib import Path

import httpx

from gse import golden, status
from gse.config import load_settings, require
from gse.db import connect, migrate
from gse.ingest_x import ingest_asset, snapshot_t60
from gse.market import sync_bars, sync_fear_greed
from gse.score_job import score_pending
from gse.x_client import RateLimited, XApiError, XClient

log = logging.getLogger("gse")


def _x(settings) -> XClient:
    return XClient(require(settings.x_bearer_token, "X_BEARER_TOKEN"))


def cmd_migrate(settings, conn, args) -> int:
    applied = migrate(conn)
    print("migrasi diterapkan:", ", ".join(applied) if applied else "tidak ada (sudah terbaru)")
    return 0


def cmd_ingest_x(settings, conn, args) -> int:
    x = _x(settings)
    code = 0
    for asset in settings.assets:
        if args.asset and asset.symbol not in args.asset:
            continue
        try:
            r = ingest_asset(conn, x, asset, settings.x_max_pages)
            print(f"{r.asset}: {r.tweets} tweet, {r.pages} halaman" + (" (CELAH tercatat)" if r.gap_recorded else ""))
        except RateLimited as e:
            log.error("%s: %s", asset.symbol, e)
            return 2
        except (XApiError, httpx.HTTPError) as e:
            log.error("%s: gagal mengambil data X: %s", asset.symbol, e)
            code = 1
    return code


def cmd_snapshot_metrics(settings, conn, args) -> int:
    try:
        stored, gone = snapshot_t60(conn, _x(settings), settings.metric_delay_minutes, args.limit)
    except RateLimited as e:
        log.error("%s", e)
        return 2
    except (XApiError, httpx.HTTPError) as e:
        log.error("gagal mengambil metrik dari X: %s", e)
        return 1
    print(f"metrik t60: {stored} tersimpan, {gone} tweet tidak tersedia lagi")
    return 0


def cmd_score(settings, conn, args) -> int:
    from openai import OpenAI

    client = OpenAI(api_key=require(settings.xai_api_key, "XAI_API_KEY"), base_url="https://api.x.ai/v1")
    try:
        scored, failed = score_pending(conn, client, settings.grok_model, batch_size=args.batch_size,
                                       limit=args.limit, only_golden=args.golden)
    except Exception:
        log.exception("penilaian Grok berhenti; batch yang sudah tersimpan tetap ada")
        return 1
    print(f"skor: {scored} tersimpan, {failed} tanpa skor valid (akan dicoba lagi, maks 3x)")
    return 0


def cmd_market(settings, conn, args) -> int:
    code = 0
    with httpx.Client(timeout=30) as http:
        for asset in settings.assets:
            for interval in args.interval:
                try:
                    n = sync_bars(conn, http, settings.binance_base_url, asset.pair, interval)
                    print(f"{asset.pair} {interval}: {n} bar baru")
                except httpx.HTTPStatusError as e:
                    hint = (" (lokasi server diblokir Binance; coba BINANCE_BASE_URL=https://data-api.binance.vision)"
                            if e.response.status_code in (403, 451) else "")
                    log.error("%s %s: HTTP %s%s", asset.pair, interval, e.response.status_code, hint)
                    code = 1
                except httpx.HTTPError as e:
                    log.error("%s %s: %s", asset.pair, interval, e)
                    code = 1
    return code


def cmd_fear_greed(settings, conn, args) -> int:
    with httpx.Client(timeout=30) as http:
        try:
            print(f"fear & greed: {sync_fear_greed(conn, http)} hari tersimpan/diperbarui")
        except httpx.HTTPError as e:
            log.error("fear & greed gagal diambil: %s", e)
            return 1
    return 0


def cmd_golden_export(settings, conn, args) -> int:
    n = golden.export_sample(conn, Path(args.out), args.n, args.labeler, args.seed)
    print(f"{n} tweet diekspor ke {args.out}. Isi kolom 'label' dengan angka -1..1, lalu jalankan golden-import.")
    return 0


def cmd_golden_import(settings, conn, args) -> int:
    r = golden.import_labels(conn, Path(args.file), args.labeler)
    print(f"{r.imported} label diimpor, {r.skipped_empty} baris kosong dilewati")
    for e in r.errors:
        print("  error:", e)
    return 1 if r.errors else 0


def cmd_golden_eval(settings, conn, args) -> int:
    ev = golden.evaluate(conn, settings.grok_model)
    print(f"label: {ev.labeled} tweet, punya skor Grok saat ini: {ev.scored}")
    if ev.scored < ev.labeled:
        print("  sebagian belum dinilai, jalankan: gse score --golden")
    print(f"Spearman: {ev.spearman:.3f}   MAE: {ev.mae:.3f}")
    for lang, (n, rho) in ev.by_lang.items():
        print(f"  {lang}: n={n} Spearman={rho:.3f}")
    ok = not math.isnan(ev.spearman) and ev.spearman >= 0.6 and ev.labeled >= 500
    print("Gate 1 (golden set >= 500, Spearman >= 0.6):", "LOLOS" if ok else "BELUM")
    return 0


def cmd_status(settings, conn, args) -> int:
    rows = status.coverage(conn, settings.grok_model, args.days)
    if not rows:
        print("belum ada data tweet")
    for c in rows:
        print(f"{c.asset}: {c.tweets_window} tweet/{args.days} hari, {c.tweets_24h} dalam 24 jam, "
              f"skor {c.scored_window}/{c.tweets_window}")
        print(f"  cakupan {c.days_covered:.1f} hari, jeda terpanjang {c.max_gap}, "
              f"sejak tweet terakhir {c.since_latest}, celah tercatat {c.recorded_gaps}")
        print("  Gate 1 (pengumpulan):", "LOLOS" if c.gate1_ok else "BELUM")
    print("\npemakaian API (hari ini / 30 hari):")
    for service, metric, today, total in status.usage(conn):
        print(f"  {service}.{metric}: {today or 0} / {total}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="gse", description="Grok Sentiment Engine - Fase 1")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("migrate", help="buat/perbarui skema database").set_defaults(fn=cmd_migrate)

    s = sub.add_parser("ingest-x", help="ambil tweet baru dari X API")
    s.add_argument("--asset", nargs="*", help="batasi ke simbol tertentu, mis. BTC")
    s.set_defaults(fn=cmd_ingest_x)

    s = sub.add_parser("snapshot-metrics", help="ambil metrik tweet pada umur t+60 menit")
    s.add_argument("--limit", type=int, default=1000)
    s.set_defaults(fn=cmd_snapshot_metrics)

    s = sub.add_parser("score", help="nilai tweet yang belum punya skor dengan Grok")
    s.add_argument("--limit", type=int, default=200, help="maks tweet per aset per run")
    s.add_argument("--batch-size", type=int, default=20)
    s.add_argument("--golden", action="store_true", help="hanya tweet yang ada di golden set")
    s.set_defaults(fn=cmd_score)

    s = sub.add_parser("market", help="sinkronkan OHLCV dari Binance")
    s.add_argument("--interval", nargs="+", default=["1d", "1h"], choices=["1d", "1h"])
    s.set_defaults(fn=cmd_market)

    sub.add_parser("fear-greed", help="sinkronkan Crypto Fear & Greed Index").set_defaults(fn=cmd_fear_greed)

    s = sub.add_parser("golden-export", help="ekspor sampel tweet ke CSV untuk dilabeli")
    s.add_argument("--out", required=True)
    s.add_argument("--n", type=int, default=500)
    s.add_argument("--labeler", required=True)
    s.add_argument("--seed", default="gse")
    s.set_defaults(fn=cmd_golden_export)

    s = sub.add_parser("golden-import", help="impor label dari CSV")
    s.add_argument("file")
    s.add_argument("--labeler", required=True)
    s.set_defaults(fn=cmd_golden_import)

    sub.add_parser("golden-eval", help="bandingkan skor Grok dengan label manusia").set_defaults(fn=cmd_golden_eval)

    s = sub.add_parser("status", help="cakupan data, celah, dan pemakaian API")
    s.add_argument("--days", type=int, default=14)
    s.set_defaults(fn=cmd_status)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    settings = load_settings()
    with connect(settings.database_url) as conn:
        return args.fn(settings, conn, args)


if __name__ == "__main__":
    sys.exit(main())
