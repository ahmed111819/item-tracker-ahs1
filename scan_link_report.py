"""Collect per-link scanner results and render the GitHub Actions report."""

import csv
import json
import os
import sys
import time
from pathlib import Path
from urllib.parse import urlsplit

OFFER_KINDS = ("coupon", "promotion", "price_drop")
ALERT_KINDS = (*OFFER_KINDS, "block_release")
LABELS = {
    "not_started": "لم يبدأ", "in_progress": "غير مكتمل", "failed": "توقف عند خطأ",
    "AMAZON_NEXT_DISABLED": "اكتمل", "CONFIRMED_EMPTY_END_PAGE": "اكتمل",
    "MAX_PAGES_SAFETY_CAP": "بلغ حد الصفحات",
    "REPEATED_RESULT_PAGES": "توقّف بسبب تكرار النتائج",
}


def record_event(bot, index, url, **details):
    folder = os.environ.get("SCAN_REPORT_DIR")
    if folder:
        try:
            directory = Path(folder)
            directory.mkdir(parents=True, exist_ok=True)
            with (directory / f"{bot}.jsonl").open("a", encoding="utf-8") as stream:
                event = dict(bot=bot, index=index, url=url, at=time.time(), **details)
                stream.write(json.dumps(event, ensure_ascii=False) + "\n")
        except OSError as error:
            print(f"Scan report write failed: {type(error).__name__}")


def classify_card_for_report(metrics, asin, deals, coupon_min, promotion_min, price_min):
    for offer in deals.get("offers", []):
        kind = "coupon" if "coupon" in offer["type"] else "promotion"
        if offer["discount_percent"] >= (coupon_min if kind == "coupon" else promotion_min):
            metrics["found"][kind].add(asin)
    price_drop = deals["price_drop"]
    if price_drop and price_drop["discount_percent"] >= price_min:
        metrics["found"]["price_drop"].add(asin)
    if deals.get("unrecognized"):
        metrics["unknown"].add(asin)


def new_page_metrics():
    return {
        "found": {kind: set() for kind in OFFER_KINDS},
        "sent": dict.fromkeys(ALERT_KINDS, 0),
        "unknown": set(),
    }


def summarize(directory):
    links = {}
    for path in sorted(directory.glob("bot*.jsonl")):
        for line in path.read_text(encoding="utf-8").splitlines():
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue  # A stopped process may leave a final partial line.
            key = (event["bot"], event["index"], event["url"])
            row = links.setdefault(key, dict(
                pages={}, found={kind: set() for kind in OFFER_KINDS},
                products=set(), unknown=set(), sent=dict.fromkeys(ALERT_KINDS, 0),
                status="not_started", elapsed_seconds=0, start=None,
            ))
            kind = event["kind"]
            if kind == "start":
                row.update(status="in_progress", start=event["at"])
            elif kind == "finish":
                row["status"] = event["status"]
            elif kind == "page":
                page = event["page"]
                row["pages"][page] = row["pages"].get(page, False) or event["success"]
                row["products"].update(event["products"])
                row["unknown"].update(event["unknown"])
                for offer_kind in OFFER_KINDS:
                    row["found"][offer_kind].update(event["found"][offer_kind])
                for alert_kind in ALERT_KINDS:
                    row["sent"][alert_kind] += event["sent"][alert_kind]
            if kind in {"page", "finish"} and row["start"] is not None:
                row["elapsed_seconds"] = max(0, round(event["at"] - row["start"]))

    for (bot, index, url), row in sorted(links.items()):
        pages_ok = sum(row["pages"].values())
        yield dict(bot=bot, link_number=index, url=url, status=row["status"],
                   pages_opened=pages_ok, pages_failed=len(row["pages"]) - pages_ok,
                   unique_products=len(row["products"]),
                   **{f"found_{kind}": len(row["found"][kind]) for kind in OFFER_KINDS},
                   **{f"sent_{kind}": row["sent"][kind] for kind in ALERT_KINDS},
                   unrecognized_products=len(row["unknown"]),
                   last_attempt_seconds=row["elapsed_seconds"])


def render_report(directory, output):
    rows = list(summarize(directory))
    output.mkdir(parents=True, exist_ok=True)
    fields = ["bot", "link_number", "url", "status", "pages_opened", "pages_failed",
              "unique_products", *(f"found_{k}" for k in OFFER_KINDS),
              *(f"sent_{k}" for k in ALERT_KINDS), "unrecognized_products", "last_attempt_seconds"]
    with (output / "scan-links.csv").open("w", encoding="utf-8-sig", newline="") as stream:
        writer = csv.DictWriter(stream, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)
    (output / "scan-links.json").write_text(json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = [
        "# نتائج الروابط في هذا التشغيل", "",
        "المنتجات والصفحات تُحسب مرة لكل رابط، حتى إذا أعاد البوت المحاولة. "
        "عدادات «وجد» تحسب المنتجات المؤهلة لكل نوع قبل الحجب ومنع التكرار، "
        "لذلك قد يظهر المنتج في أكثر من نوع. عدادات «أرسل» تحسب تنبيهات تيليجرام المرسلة فعليًا. "
        "المدة لآخر محاولة لكل رابط وقد تتداخل مع روابط تعمل بالتوازي.", "",
    ]
    if not rows:
        lines.append("لا توجد نتائج مسجلة لهذا التشغيل. راجع سجل مهمة الفحص لمعرفة سبب التوقف.")
    for bot in sorted({row["bot"] for row in rows}):
        lines.extend([
            f"## {bot}", "",
            "| الرابط | الحالة | صفحات فُتحت | صفحات تعذرت | منتجات فريدة | وجد تخفيض / كوبون / عرض | أرسل تخفيض / كوبون / عرض / فك حجب | منتجات بصيغ للمراجعة | المدة بالدقائق |",
            "|---|---|---:|---:|---:|---|---|---:|---:|",
        ])
        for row in rows:
            if row["bot"] != bot:
                continue
            url = row["url"].replace(" ", "%20").replace("|", "%7C").replace("<", "%3C").replace(">", "%3E")
            label = f"رابط {row['link_number']}"
            if urlsplit(url).scheme in {"https", "http"}:
                label = f"[{label}](<{url}>)"
            found = " / ".join(str(row[f"found_{k}"]) for k in ("price_drop", "coupon", "promotion"))
            sent = " / ".join(str(row[f"sent_{k}"]) for k in ("price_drop", "coupon", "promotion", "block_release"))
            lines.append(
                f"| {label} | {LABELS.get(row['status'], row['status'])} | {row['pages_opened']} | "
                f"{row['pages_failed']} | {row['unique_products']} | {found} | {sent} | "
                f"{row['unrecognized_products']} | {row['last_attempt_seconds'] / 60:.1f} |"
            )
        lines.append("")
    summary = "\n".join(lines) + "\n"
    (output / "scan-links.md").write_text(summary, encoding="utf-8")
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with Path(os.environ["GITHUB_STEP_SUMMARY"]).open("a", encoding="utf-8") as stream:
            stream.write(summary if len(summary.encode("utf-8")) < 900_000 else
                         "# نتائج الروابط\n\nحمّل التقرير الكامل من Artifacts.\n")


if __name__ == "__main__":
    render_report(Path(sys.argv[1]), Path(sys.argv[2]))
