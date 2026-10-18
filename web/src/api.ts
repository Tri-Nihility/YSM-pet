// 后端 REST 封装

async function j<T>(res: Response): Promise<T> {
  if (!res.ok) {
    const txt = await res.text().catch(() => "");
    throw new Error(`${res.status} ${txt}`);
  }
  return res.json() as Promise<T>;
}

export const api = {
  config: () => fetch("/api/config").then((r) => j<import("./types").ConfigResp>(r)),
  rig: () => fetch("/rig.json").then((r) => j<import("./types").Rig>(r)),
  pose: () =>
    fetch("/api/pose").then((r) =>
      j<{ mode: string; angles: number[]; expressions: Record<string, string> }>(r)),
  setPose: (angles: number[]) =>
    fetch("/api/pose", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ angles }),
    }).then((r) => j<{ angles: number[] }>(r)),
  expressions: () =>
    fetch("/api/expressions").then((r) =>
      j<{ enabled: boolean; slots: import("./types").ExprSlot[]; state: Record<string, string> }>(r)),
  setExpression: (slot: string, option: string) =>
    fetch("/api/expressions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ slot, option }),
    }).then((r) => j<{ state: Record<string, string> }>(r)),
  trainStart: (stage: string, iters: number, spu: number, mode: string, warmstart = "") =>
    fetch("/api/train/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ stage, iters, stepsPerUpdate: spu, mode, warmstart }),
    }).then((r) => j<{ ok: boolean }>(r)),
  trainStop: () =>
    fetch("/api/train/stop", { method: "POST" }).then((r) => j<{ ok: boolean }>(r)),
  trainPause: () =>
    fetch("/api/train/pause", { method: "POST" }).then((r) => j<{ paused: boolean }>(r)),
  checkpoints: () =>
    fetch("/api/checkpoints").then((r) =>
      j<{ name: string; path: string; size: number }[]>(r)),
};
