"""阶段 4：轻量 PPO（torch CPU）。

足够训练 9 维连续控制的站立/行走任务，不引入任何 RL 框架依赖。
结构：ActorCritic(MLP 2x64 tanh) + 状态无关 log_std + GAE + clip surrogate。
"""
import os

os.environ.setdefault("OPENBLAS_NUM_THREADS", "1")
os.environ.setdefault("OMP_NUM_THREADS", "1")

import numpy as np  # noqa: E402
import torch  # noqa: E402
import torch.nn as nn  # noqa: E402

torch.set_num_threads(max(1, min(4, os.cpu_count() or 2)))


def mlp(sizes, act=nn.Tanh):
    layers = []
    for i in range(len(sizes) - 1):
        layers.append(nn.Linear(sizes[i], sizes[i + 1]))
        if i < len(sizes) - 2:
            layers.append(act())
    return nn.Sequential(*layers)


class ActorCritic(nn.Module):
    def __init__(self, obs_dim, act_dim, hidden=64, log_std_init=-0.5):
        super().__init__()
        self.pi = mlp([obs_dim, hidden, hidden, act_dim])
        self.v = mlp([obs_dim, hidden, hidden, 1])
        self.log_std = nn.Parameter(torch.full((act_dim,), float(log_std_init)))

    def value(self, obs):
        return self.v(obs).squeeze(-1)

    def dist(self, obs):
        mu = self.pi(obs)
        return torch.distributions.Normal(mu, self.log_std.exp())

    @torch.no_grad()
    def act(self, obs, deterministic=False):
        obs = torch.as_tensor(obs, dtype=torch.float32)
        d = self.dist(obs)
        a = d.mean if deterministic else d.sample()
        return a.numpy(), d.log_prob(a).sum(-1).numpy(), float(self.value(obs).item())


class RunningNorm:
    """观测标准化（Welford），保存到 checkpoint 以便评估/继续训练。"""

    def __init__(self, dim, clip=8.0):
        self.mean = np.zeros(dim)
        self.var = np.ones(dim)
        self.count = 1e-4
        self.clip = clip

    def update(self, x):
        x = np.asarray(x, float)
        bm, bv, bc = x.mean(0), x.var(0), x.shape[0]
        delta = bm - self.mean
        tot = self.count + bc
        self.mean += delta * bc / tot
        m_a = self.var * self.count
        m_b = bv * bc
        self.var = (m_a + m_b + delta ** 2 * self.count * bc / tot) / tot
        self.count = tot

    def normalize(self, x):
        return np.clip((x - self.mean) / np.sqrt(self.var + 1e-8),
                       -self.clip, self.clip)

    def state_dict(self):
        return {"mean": self.mean, "var": self.var, "count": self.count}

    def load_state_dict(self, d):
        self.mean, self.var, self.count = d["mean"], d["var"], d["count"]


def discount_cumsum(x, gamma):
    out = np.zeros_like(x, dtype=np.float64)
    acc = 0.0
    for i in reversed(range(len(x))):
        acc = x[i] + gamma * acc
        out[i] = acc
    return out


class PPO:
    def __init__(self, obs_dim, act_dim, lr=3e-4, gamma=0.99, lam=0.95,
                 clip=0.2, ent_coef=0.0, vf_coef=0.5, epochs=10,
                 minibatch=256, max_grad_norm=0.5, seed=0):
        self.net = ActorCritic(obs_dim, act_dim)
        self.opt = torch.optim.Adam(self.net.parameters(), lr=lr)
        self.norm = RunningNorm(obs_dim)
        self.gamma, self.lam, self.clip = gamma, lam, clip
        self.ent_coef, self.vf_coef = ent_coef, vf_coef
        self.epochs, self.minibatch = epochs, minibatch
        self.max_grad_norm = max_grad_norm
        self.rng = np.random.default_rng(seed)

    # ------------------------------------------------------------ rollout
    def collect(self, env, steps, deterministic=False, on_episode=None, on_step=None):
        ob = env.reset()
        buf = {k: [] for k in ("obs", "act", "logp", "rew", "done", "val")}
        eps_stats = []
        ep_ret, ep_len = 0.0, 0
        for _ in range(steps):
            o = self.norm.normalize(ob)
            a, logp, v = self.net.act(o, deterministic)
            ob2, r, d, info = env.step(a)
            if on_step:
                on_step(r)
            for k, val in zip(("obs", "act", "logp", "rew", "done", "val"),
                              (ob, a, logp, r, float(d), v)):
                buf[k].append(val)
            ep_ret += r
            ep_len += 1
            ob = ob2
            if d:
                self.norm.update(np.stack(buf["obs"][-ep_len:]))
                eps_stats.append({"ret": ep_ret, "len": ep_len, **{
                    k: v for k, v in info.items() if isinstance(v, (int, float, bool))}})
                if on_episode:
                    on_episode(eps_stats[-1])
                ob = env.reset()
                ep_ret, ep_len = 0.0, 0
        if ep_len:
            self.norm.update(np.stack(buf["obs"][-ep_len:]))
        return {k: np.asarray(v) for k, v in buf.items()}, eps_stats

    # ------------------------------------------------------------ update
    def update(self, buf):
        obs = torch.as_tensor(self.norm.normalize(buf["obs"]), dtype=torch.float32)
        act = torch.as_tensor(buf["act"], dtype=torch.float32)
        old_logp = torch.as_tensor(buf["logp"], dtype=torch.float32)
        rew, done, val = buf["rew"], buf["done"], buf["val"]

        # GAE
        n = len(rew)
        adv = np.zeros(n)
        lastgaelam = 0.0
        for i in reversed(range(n)):
            next_v = 0.0 if done[i] else val[i + 1] if i + 1 < n else 0.0
            nonterm = 1.0 - done[i]
            delta = rew[i] + self.gamma * next_v * nonterm - val[i]
            lastgaelam = delta + self.gamma * self.lam * nonterm * lastgaelam
            adv[i] = lastgaelam
        ret = adv + val

        adv_t = torch.as_tensor(adv, dtype=torch.float32)
        adv_t = (adv_t - adv_t.mean()) / (adv_t.std() + 1e-8)
        ret_t = torch.as_tensor(ret, dtype=torch.float32)

        idx = np.arange(n)
        stats = {"pi_loss": [], "v_loss": [], "entropy": [], "kl": []}
        for _ in range(self.epochs):
            self.rng.shuffle(idx)
            for lo in range(0, n, self.minibatch):
                mb = idx[lo:lo + self.minibatch]
                if len(mb) < 8:
                    continue
                mb = torch.as_tensor(mb)
                d = self.net.dist(obs[mb])
                logp = d.log_prob(act[mb]).sum(-1)
                ratio = (logp - old_logp[mb]).exp()
                pg1 = -adv_t[mb] * ratio
                pg2 = -adv_t[mb] * torch.clamp(ratio, 1 - self.clip, 1 + self.clip)
                pi_loss = torch.max(pg1, pg2).mean()
                v_loss = ((self.net.value(obs[mb]) - ret_t[mb]) ** 2).mean()
                ent = d.entropy().sum(-1).mean()
                loss = pi_loss + self.vf_coef * v_loss - self.ent_coef * ent
                self.opt.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(self.net.parameters(), self.max_grad_norm)
                self.opt.step()
                with torch.no_grad():
                    stats["pi_loss"].append(float(pi_loss))
                    stats["v_loss"].append(float(v_loss))
                    stats["entropy"].append(float(ent))
                    stats["kl"].append(float((old_logp[mb] - logp).mean()))
        return {k: float(np.mean(v)) for k, v in stats.items()}

    # ------------------------------------------------------------ io
    def save(self, path, extra=None):
        torch.save({
            "net": self.net.state_dict(),
            "norm": self.norm.state_dict(),
            "extra": extra or {},
        }, path)

    def load(self, path):
        ck = torch.load(path, weights_only=False)
        self.net.load_state_dict(ck["net"])
        self.norm.load_state_dict(ck["norm"])
        return ck.get("extra", {})
