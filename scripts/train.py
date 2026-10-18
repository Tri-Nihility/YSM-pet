"""阶段 4：两阶段课程训练入口。

    python scripts/train.py --stage stand --iters 200
    python scripts/train.py --stage walk  --iters 300 --warmstart outputs/checkpoints/stand_final.pt

产出
    outputs/checkpoints/<stage>_final.pt     最终策略
    outputs/checkpoints/<stage>_best.pt      平均回报最高
    outputs/logs/<stage>_train.csv           逐迭代日志
    outputs/logs/<stage>_curve.png           奖励曲线
"""
import argparse
import csv
import json
import os
import sys
import time

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
from PIL import Image, ImageDraw  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from env.stand_walk import StandWalkEnv  # noqa: E402
from train.ppo import PPO  # noqa: E402


def plot_curve(rows, path, title):
    """无 matplotlib 依赖，用 PIL 画训练曲线。"""
    W, H = 720, 320
    img = Image.new("RGB", (W, H), "white")
    d = ImageDraw.Draw(img)
    if len(rows) < 2:
        d.text((20, 20), "no data", fill="black")
        img.save(path)
        return
    pad = 40
    xs = [r["iter"] for r in rows]
    series = {
        "mean_ep_ret": ([r["mean_ep_ret"] for r in rows], (0, 0, 200)),
        "mean_ep_len/max": ([r["mean_ep_len"] / r["max_len"] for r in rows], (0, 140, 0)),
    }
    ymax = max(max(v[0]) for v in series.values()) * 1.1 or 1.0
    xmin, xmax = min(xs), max(xs)
    for key, (ys, col) in series.items():
        pts = [(pad + (x - xmin) / max(xmax - xmin, 1) * (W - 2 * pad),
                H - pad - (y / ymax) * (H - 2 * pad)) for x, y in zip(xs, ys)]
        d.line(pts, fill=col, width=2)
    d.line([(pad, pad), (pad, H - pad), (W - pad, H - pad)], fill="black")
    d.text((pad, 8), title, fill="black")
    d.text((pad + 8, 24), f"blue=mean_ep_ret  green=ep_len/max_len  ymax={ymax:.1f}",
           fill=(90, 90, 90))
    img.save(path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["stand", "walk"], default="stand")
    ap.add_argument("--iters", type=int, default=200)
    ap.add_argument("--steps-per-update", type=int, default=4096)
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--lr", type=float, default=3e-4)
    ap.add_argument("--ent-coef", type=float, default=0.0)
    ap.add_argument("--warmstart", default="")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    out_ck = os.path.join(ROOT, "outputs", "checkpoints")
    out_log = os.path.join(ROOT, "outputs", "logs")
    os.makedirs(out_ck, exist_ok=True)
    os.makedirs(out_log, exist_ok=True)
    tag = args.stage
    ck_final = os.path.join(out_ck, f"{tag}_final.pt")
    ck_best = os.path.join(out_ck, f"{tag}_best.pt")
    log_csv = os.path.join(out_log, f"{tag}_train.csv")
    log_png = os.path.join(out_log, f"{tag}_curve.png")

    env = StandWalkEnv(args.stage, seed=args.seed)
    agent = PPO(env.obs_dim, env.act_dim, lr=args.lr, ent_coef=args.ent_coef,
                seed=args.seed)
    if args.warmstart and os.path.exists(args.warmstart):
        agent.load(args.warmstart)
        print("warmstart <-", args.warmstart)

    rows = []
    best_ret = -1e18
    t0 = time.time()
    print(f"== 训练 {args.stage} | obs={env.obs_dim} act={env.act_dim} "
          f"max_len={env.max_steps} iters={args.iters} ==")
    with open(log_csv, "w", newline="", encoding="utf-8") as fp:
        wr = csv.DictWriter(fp, fieldnames=[
            "iter", "steps", "mean_ep_ret", "mean_ep_len", "max_len",
            "fallen_rate", "pi_loss", "v_loss", "entropy", "sec"])
        wr.writeheader()
        for it in range(1, args.iters + 1):
            buf, eps = agent.collect(env, args.steps_per_update)
            stats = agent.update(buf)
            ret = float(np.mean([e["ret"] for e in eps])) if eps else 0.0
            elen = float(np.mean([e["len"] for e in eps])) if eps else 0.0
            fal = float(np.mean([bool(e.get("fallen")) for e in eps])) if eps else 0.0
            row = {"iter": it, "steps": it * args.steps_per_update,
                   "mean_ep_ret": round(ret, 3), "mean_ep_len": round(elen, 1),
                   "max_len": env.max_steps, "fallen_rate": round(fal, 3),
                   "pi_loss": round(stats["pi_loss"], 5),
                   "v_loss": round(stats["v_loss"], 5),
                   "entropy": round(stats["entropy"], 5),
                   "sec": round(time.time() - t0, 1)}
            rows.append(row)
            wr.writerow(row)
            fp.flush()                                    # 逐迭代落盘，可实时 tail
            if it % 10 == 0:
                agent.save(ck_final, {"stage": args.stage, "iter": it, "ret": ret})
            if it % 5 == 0 or it == 1 or it == args.iters:
                print(f"  [{it:4d}/{args.iters}] ret={ret:9.2f} len={elen:6.1f}/"
                      f"{env.max_steps} fallen={fal:.2f} ent={stats['entropy']:.3f} "
                      f"({row['sec']}s)")
            if ret > best_ret:
                best_ret = ret
                agent.save(ck_best, {"stage": args.stage, "iter": it, "ret": ret})
    agent.save(ck_final, {"stage": args.stage, "iter": args.iters, "ret": ret})
    plot_curve(rows, log_png, f"PPO {args.stage} (best_ret={best_ret:.1f})")
    print("保存:", ck_final, ck_best)
    print("曲线:", log_png)
    env.close()


if __name__ == "__main__":
    main()
