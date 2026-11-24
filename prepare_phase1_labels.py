"""prepare_phase1_labels.py — Phase 1a: 19 T1 + 15 random = 34 pairs.

Usage:
  python prepare_phase1_labels.py \
      --all-jsonl explain_out_v2_C5/_all.jsonl \
      --out explain_out_v2_C5/phase1_label_sheet.csv \
      --n-random 15 --seed 42
"""
import argparse
import asyncio
import csv
import json
import random
from pathlib import Path

import httpx

from generate_matches import (
    fetch_backdrops_from_api, fetch_models_from_api,
    download_model_image,
)
from monochrome_v2 import prepare_model_pixels, evaluate_pair_v2


def load_c5(path):
    """C5 — 1 t на pair_key (n_positions=1)."""
    result = {}
    for line in open(path, encoding="utf-8"):
        r = json.loads(line)
        result[r["pair_key"]] = r
    return result


def accept_at(row, theta_fill, theta_edge):
    return (
        (row.get("fill_p90") or 0) <= theta_fill
        and (row.get("edge_p90") or 0) <= theta_edge
    )


async def main():
    p = argparse.ArgumentParser()
    p.add_argument("--all-jsonl", required=True)
    p.add_argument("--out", required=True)
    p.add_argument("--n-random", type=int, default=15)
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    rows = load_c5(args.all_jsonl)
    print(f"Loaded {len(rows)} pairs from {args.all_jsonl}")

    t1_keys = sorted([k for k, r in rows.items() if accept_at(r, 0.25, 0.30)])
    print(f"T1 (0.25/0.30, t=0): {len(t1_keys)}")
    if len(t1_keys) != 19:
        print(f"⚠️  Expected 19 T1, got {len(t1_keys)}")

    csv_rows = []
    for pk in t1_keys:
        r = rows[pk]
        fill = r.get("fill_p90") or 0
        edge = r.get("edge_p90") or 0
        ssim = r.get("ssim")
        csv_rows.append({
            "stratum": "T1",
            "pair_key": pk,
            "fill_p90": round(fill, 4),
            "edge_p90": round(edge, 4),
            "ssim": round(ssim, 3) if ssim is not None else "",
            "t1_at_020_passes": fill <= 0.20 and edge <= 0.30,
            "v2_accepted": True,
            "human_label": "",
            "human_confidence": "",
            "human_notes": "",
        })

    print(f"\nFetching {args.n_random} random pairs (seed={args.seed})...")
    async with httpx.AsyncClient() as client:
        backdrops = await fetch_backdrops_from_api(client)
        models_dict = await fetch_models_from_api(client)

    all_items = list(models_dict.items())
    rng = random.Random(args.seed)
    random_models = rng.sample(all_items, args.n_random)
    backdrop_names = list(backdrops.keys())

    async with httpx.AsyncClient() as client:
        for i, (_, info) in enumerate(random_models, 1):
            bd_name = rng.choice(backdrop_names)
            bd = backdrops[bd_name]
            image = await download_model_image(client, info["image_url"])
            if image is None:
                print(f"  [{i}/{args.n_random}] {info['model']}|{bd_name}: download failed")
                continue
            pixels = prepare_model_pixels(image)
            pair = evaluate_pair_v2(
                pixels, bd,
                profile=None,
                n_positions=1, pos_q=1.0,
                use_prefilter=False,
            )
            if pair.positions:
                w = pair.positions[0]
                fill, edge, ssim = w.fill_p90, w.edge_p90, w.ssim
            else:
                fill = edge = ssim = 0
            csv_rows.append({
                "stratum": "random",
                "pair_key": f"{info['collection']}|{info['model']}|{bd_name}",
                "fill_p90": round(fill, 4),
                "edge_p90": round(edge, 4),
                "ssim": round(ssim, 3) if ssim else "",
                "t1_at_020_passes": "",
                "v2_accepted": pair.accepted,
                "human_label": "",
                "human_confidence": "",
                "human_notes": "",
            })
            print(f"  [{i}/{args.n_random}] {info['model']}|{bd_name}: "
                  f"fill={fill:.3f} edge={edge:.3f} accept={pair.accepted}")

    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w", encoding="utf-8", newline="") as f:
        w = csv.DictWriter(f, fieldnames=[
            "stratum", "pair_key", "fill_p90", "edge_p90", "ssim",
            "t1_at_020_passes", "v2_accepted",
            "human_label", "human_confidence", "human_notes",
        ])
        w.writeheader()
        w.writerows(csv_rows)

    n_t1 = sum(1 for r in csv_rows if r["stratum"] == "T1")
    n_rand = sum(1 for r in csv_rows if r["stratum"] == "random")
    n_rand_acc = sum(
        1 for r in csv_rows
        if r["stratum"] == "random" and r["v2_accepted"]
    )
    print(f"\n✅ {out_path}")
    print(f"   T1: {n_t1}, random: {n_rand} (v2 accepted: {n_rand_acc})")
    print(f"   Total: {len(csv_rows)}")
    print(f"\nNext:")
    print(f"  1. Generate PNGs (см. generate_pngs.py)")
    print(f"  2. Open CSV, fill human_label + human_confidence для всех 34")
    print(f"  3. Send back CSV + PNGs при необходимости")


if __name__ == "__main__":
    asyncio.run(main())