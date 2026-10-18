"""策略评估 / 回放。

    python scripts/eval_policy.py --stage stand --ckpt outputs/checkpoints/stand_final.pt
    python scripts/eval_policy.py --stage walk  --ckpt outputs/checkpoints/walk_final.pt --episodes 20
    python scripts/eval_policy.py --stage stand --ckpt ... --render          # 弹 pybullet 窗口
    python scripts/eval_policy.py --stage stand --ckpt ... --dump-traj out.json

判定标准（写死在这里，避免口头约定漂移）
------------------------------------------
stand「学会站立」：survival_rate >= 0.8 且 mean_tilt_deg <= 20 且 mean_height_ratio >= 0.9
walk 「学会走路」：survival_rate >= 0.6 且 mean_speed >= 0.5 * target_speed 且步态交替数 > 0
"""
import argparse
import json
import os
import sys

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "src"))

from env.stand_walk import StandWalkEnv  # noqa: E402
from train.ppo import PPO  # noqa: E402


def run_one(env, agent, deterministic=True, max_steps=None):
    """跑一回合，返回统计 + 轨迹。"""
    ob = env.reset()
    done = False
    ret, n = 0.0, 0
    traj = []
    limit = max_steps or env.max_steps
    while not done and n < limit:
        a, _, _ = agent.net.act(agent.norm.normalize(ob), deterministic)
        ob, r, done, info = env.step(a)
        ret += r
        n += 1
        traj.append({"q": env.current_q().round(3).tolist(),
                     "h": round(float(ob[0]), 4),
                     "r": round(float(r), 4)})
    st = env._tilt_deg()
    vel = 0.0
    try:
        import pybullet as p
        vel = p.getBaseVelocity(env.robot, physicsClientId=env.client)[0][0]
    except Exception:
        pass
    return {
        "ret": ret, "len": n, "max_len": env.max_steps,
        "height_ratio": float(ob[0]),
        "tilt_deg": float(st),
        "speed": float(vel),
        "x": float(env._pelvis_xz()[0]),
        **{k: v for k, v in info.items() if isinstance(v, (int, float, bool))},
    }, traj


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--stage", choices=["stand", "walk"], default="stand")
    ap.add_argument("--ckpt", default="")
    ap.add_argument("--episodes", type=int, default=10)
    ap.add_argument("--seed", type=int, default=1000)
    ap.add_argument("--render", action="store_true")
    ap.add_argument("--stochastic", action="store_true", help="用采样动作而非均值")
    ap.add_argument("--dump-traj", default="", help="把最后一回合轨迹写成 json")
    args = ap.parse_args()

    ck = args.ckpt or os.path.join(ROOT, "outputs", "checkpoints",
                                   f"{args.stage}_final.pt")
    if not os.path.exists(ck):
        sys.exit(f"找不到 checkpoint: {ck}")

    env = StandWalkEnv(args.stage, render=args.render, seed=args.seed)
    agent = PPO(env.obs_dim, env.act_dim, seed=args.seed)
    extra = agent.load(ck)
    env.seed(args.seed)
    print(f"评估 {args.stage} | ckpt={os.path.relpath(ck, ROOT)} | extra={extra}")

    stats, last_traj = [], []
    for i in range(args.episodes):
        s, tr = run_one(env, agent, deterministic=not args.stochastic)
        stats.append(s)
        last_traj = tr
        print(f"  ep{i:02d} ret={s['ret']:8.2f} len={s['len']:4d}/{s['max_len']} "
              f"h={s['height_ratio']:.2f} tilt={s['tilt_deg']:6.1f}° "
              f"{'SURVIVED' if s.get('survived') else 'FALLEN'}")

    def m(k):
        return float(np.mean([s.get(k, 0.0) for s in stats]))

    surv = float(np.mean([bool(s.get("survived")) for s in stats]))
    summary = {
        "stage": args.stage, "ckpt": os.path.relpath(ck, ROOT),
        "episodes": args.episodes,
        "mean_ret": round(m("ret"), 3),
        "mean_len": round(m("len"), 1), "max_len": env.max_steps,
        "survival_rate": round(surv, 3),
        "mean_height_ratio": round(m("height_ratio"), 3),
        "mean_tilt_deg": round(m("tilt_deg"), 2),
        "mean_speed": round(m("speed"), 3),
        "mean_distance": round(m("x"), 3),
    }
    if args.stage == "stand":
        ok = (surv >= 0.8 and summary["mean_tilt_deg"] <= 20
              and summary["mean_height_ratio"] >= 0.9)
    else:
        tgt = env.ep["target_speed"]
        ok = surv >= 0.6 and summary["mean_speed"] >= 0.5 * tgt
        summary["target_speed"] = float(tgt)
    summary["PASS"] = bool(ok)

    print("\n=== 汇总 ===")
    for k, v in summary.items():
        print(f"  {k:20s} {v}")
    print("  判定:", "✅ 通过" if ok else "❌ 未达标，需要继续训练")

    out = os.path.join(ROOT, "outputs", "logs", f"eval_{args.stage}.json")
    with open(out, "w", encoding="utf-8") as fp:
        json.dump({"summary": summary, "episodes": stats}, fp,
                  ensure_ascii=False, indent=2)
    print("保存:", out)

    if args.dump_traj:
        with open(args.dump_traj, "w", encoding="utf-8") as fp:
            json.dump(last_traj, fp, ensure_ascii=False)
        print("轨迹:", args.dump_traj)

    env.close()


if __name__ == "__main__":
    main()
