import csv
import math
from dataclasses import dataclass, field
from pathlib import Path

import psycopg

from gse.grok_scorer import prompt_hash

EXPORT_COLUMNS = ["tweet_id", "asset", "lang", "created_at", "context", "text", "label", "note"]


def _ranks(values: list[float]) -> list[float]:
    order = sorted(range(len(values)), key=lambda i: values[i])
    ranks = [0.0] * len(values)
    i = 0
    while i < len(order):
        j = i
        while j + 1 < len(order) and values[order[j + 1]] == values[order[i]]:
            j += 1
        avg = (i + j) / 2 + 1
        for k in range(i, j + 1):
            ranks[order[k]] = avg
        i = j + 1
    return ranks


def spearman(xs: list[float], ys: list[float]) -> float:
    """Korelasi Pearson dari rank (tie diberi rank rata-rata). NaN jika salah satu sisi konstan."""
    if len(xs) != len(ys) or len(xs) < 2:
        return math.nan
    rx, ry = _ranks(xs), _ranks(ys)
    mx, my = sum(rx) / len(rx), sum(ry) / len(ry)
    cov = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    vx = sum((a - mx) ** 2 for a in rx)
    vy = sum((b - my) ** 2 for b in ry)
    return cov / math.sqrt(vx * vy) if vx and vy else math.nan


def export_sample(conn: psycopg.Connection, path: Path, n: int, labeler: str, seed: str = "gse") -> int:
    """Ekspor sampel acak (deterministik per seed) yang belum dilabeli labeler ini.

    Skor Grok sengaja tidak ikut diekspor agar pelabel tidak terpengaruh.
    """
    rows = conn.execute(
        """
        SELECT ta.tweet_id, ta.asset, t.lang, t.created_at, ctx.text, t.text
        FROM tweet_assets ta
        JOIN tweets_raw t ON t.tweet_id = ta.tweet_id
        LEFT JOIN LATERAL (
            SELECT p.text FROM tweets_raw p
            WHERE p.tweet_id = (
                SELECT r->>'id' FROM jsonb_array_elements(t.referenced) r
                WHERE r->>'type' IN ('replied_to', 'quoted') LIMIT 1
            )
        ) ctx ON true
        WHERE t.deleted_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM golden_labels g
              WHERE g.tweet_id = ta.tweet_id AND g.asset = ta.asset AND g.labeler = %(labeler)s
          )
        ORDER BY md5(ta.tweet_id || ta.asset || %(seed)s)
        LIMIT %(n)s
        """,
        {"labeler": labeler, "seed": seed, "n": n},
    ).fetchall()
    # utf-8-sig agar Excel/Google Sheets membaca emoji dan huruf non-ASCII dengan benar
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(EXPORT_COLUMNS)
        for tid, asset, lang, created, ctx, text in rows:
            w.writerow([tid, asset, lang or "", created.isoformat(), ctx or "", text, "", ""])
    return len(rows)


@dataclass
class ImportResult:
    imported: int = 0
    skipped_empty: int = 0
    errors: list[str] = field(default_factory=list)


def import_labels(conn: psycopg.Connection, path: Path, labeler: str) -> ImportResult:
    result = ImportResult()
    rows = []
    with path.open(newline="", encoding="utf-8-sig") as f:
        for line_no, row in enumerate(csv.DictReader(f), start=2):
            raw = (row.get("label") or "").strip()
            if not raw:
                result.skipped_empty += 1
                continue
            try:
                label = float(raw.replace(",", ".").replace("−", "-"))
            except ValueError:
                result.errors.append(f"baris {line_no}: label '{raw}' bukan angka")
                continue
            if not -1.0 <= label <= 1.0:
                result.errors.append(f"baris {line_no}: label {label} di luar -1..1")
                continue
            rows.append((row["tweet_id"], row["asset"], labeler, label, (row.get("note") or "").strip() or None))

    known = {r[0] for r in conn.execute(
        "SELECT tweet_id FROM tweets_raw WHERE tweet_id = ANY(%s)", ([r[0] for r in rows],))}
    valid = [r for r in rows if r[0] in known]
    result.errors += [f"tweet_id {r[0]} tidak ada di database" for r in rows if r[0] not in known]

    with conn.transaction():
        with conn.cursor() as cur:
            cur.executemany(
                "INSERT INTO golden_labels (tweet_id, asset, labeler, label, note) VALUES (%s, %s, %s, %s, %s)"
                " ON CONFLICT (tweet_id, asset, labeler)"
                " DO UPDATE SET label = EXCLUDED.label, note = EXCLUDED.note, labeled_at = now()",
                valid,
            )
    result.imported = len(valid)
    return result


@dataclass
class Evaluation:
    labeled: int
    scored: int
    spearman: float
    mae: float
    by_lang: dict[str, tuple[int, float]]


def evaluate(conn: psycopg.Connection, model: str) -> Evaluation:
    """Bandingkan label manusia (rata-rata antar pelabel) dengan skor Grok untuk prompt saat ini."""
    labeled = conn.execute(
        "SELECT count(*) FROM (SELECT DISTINCT tweet_id, asset FROM golden_labels) g").fetchone()[0]
    rows = conn.execute(
        """
        SELECT avg(g.label), s.sentiment, coalesce(t.lang, 'und')
        FROM golden_labels g
        JOIN tweet_scores s ON s.tweet_id = g.tweet_id AND s.asset = g.asset
                           AND s.model = %s AND s.prompt_hash = %s
        JOIN tweets_raw t ON t.tweet_id = g.tweet_id
        GROUP BY g.tweet_id, g.asset, s.sentiment, t.lang
        """,
        (model, prompt_hash(model)),
    ).fetchall()
    human = [float(r[0]) for r in rows]
    grok = [float(r[1]) for r in rows]
    by_lang: dict[str, tuple[int, float]] = {}
    for lang in sorted({r[2] for r in rows}):
        h = [float(r[0]) for r in rows if r[2] == lang]
        g = [float(r[1]) for r in rows if r[2] == lang]
        by_lang[lang] = (len(h), spearman(h, g))
    mae = sum(abs(a - b) for a, b in zip(human, grok)) / len(rows) if rows else math.nan
    return Evaluation(labeled, len(rows), spearman(human, grok), mae, by_lang)
