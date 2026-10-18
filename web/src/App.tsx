import { useEffect, useRef, useState } from "react";
import Viewport from "./components/Viewport";
import JointPanel from "./components/JointPanel";
import ExpressionPanel from "./components/ExpressionPanel";
import TrainPanel from "./components/TrainPanel";
import { api } from "./api";
import type { ConfigResp, ExprSlot, Rig, WsFrame } from "./types";

export default function App() {
  const [cfg, setCfg] = useState<ConfigResp | null>(null);
  const [rig, setRig] = useState<Rig | null>(null);
  const [angles, setAngles] = useState<number[]>([]);
  const [exprState, setExprState] = useState<Record<string, string>>({});
  const [stage, setStage] = useState("stand");
  const [status, setStatus] = useState({
    running: false, paused: false, iter: 0, iters: 0,
    episode: 0, step: 0, reward: 0, epRet: 0, mode: "manual",
    history: [] as { iter: number; ret: number; len: number; fallen: number }[],
  });
  const [wsState, setWsState] = useState<" connecting" | " ok" | " down">(" connecting");

  const frameRef = useRef<WsFrame | null>(null);
  const exprHiddenRef = useRef<Set<string>>(new Set());
  const followPhysicsRef = useRef(false);
  const anglesRef = useRef<number[]>([]);
  anglesRef.current = angles;

  // ------------------------------------------------ 初始化
  useEffect(() => {
    Promise.all([api.config(), api.rig(), api.expressions(), api.pose()])
      .then(([c, r, e, p]) => {
        setCfg(c);
        setRig(r);
        setExprState(e.state);
        setAngles(p.angles);
      })
      .catch((err) => console.error("init failed", err));
  }, []);

  // 表情 -> 隐藏骨骼集合
  useEffect(() => {
    if (!rig) return;
    const hidden = new Set<string>();
    for (const slot of rig.expressions.slots) {
      const cur = exprState[slot.slot] ?? slot.default ?? slot.options[0]?.name;
      for (const opt of slot.options) {
        if (opt.name !== cur) opt.bones.forEach((b) => hidden.add(b));
      }
    }
    exprHiddenRef.current = hidden;
  }, [rig, exprState]);

  // ------------------------------------------------ WebSocket
  useEffect(() => {
    let ws: WebSocket | null = null;
    let closed = false;
    let retry = 0;
    const connect = () => {
      const proto = location.protocol === "https:" ? "wss" : "ws";
      ws = new WebSocket(`${proto}://${location.host}/ws`);
      ws.onopen = () => setWsState(" ok");
      ws.onmessage = (ev) => {
        const f = JSON.parse(ev.data) as WsFrame;
        frameRef.current = f;
        followPhysicsRef.current = f.mode !== "manual";
        if (f.mode !== "manual") {
          setAngles(f.angles); // 训练/回放：滑块跟随策略
        }
        setStatus({
          running: f.running, paused: f.paused, iter: f.iter, iters: f.iters,
          episode: f.episode, step: f.step, reward: f.reward, epRet: f.epRet,
          mode: f.mode,
          history: statusHist.current,
        });
      };
      ws.onclose = () => {
        setWsState(" down");
        if (!closed && retry < 50) {
          retry += 1;
          setTimeout(connect, 1000);
        }
      };
    };
    connect();
    return () => {
      closed = true;
      ws?.close();
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // 训练曲线（低频轮询 /api/train/status）
  const statusHist = useRef(status.history);
  useEffect(() => {
    const t = setInterval(() => {
      api
        .checkpoints()
        .catch(() => []);
      fetch("/api/train/status")
        .then((r) => r.json())
        .then((s) => {
          statusHist.current = s.history ?? [];
          setStatus((prev) => ({ ...prev, ...s, history: s.history ?? [] }));
        })
        .catch(() => undefined);
    }, 2000);
    return () => clearInterval(t);
  }, []);

  // ------------------------------------------------ 交互
  const manualLocked = status.running;

  const onJoint = (idx: number, v: number) => {
    const next = angles.slice();
    next[idx] = v;
    setAngles(next);
    api.setPose(next).catch(() => undefined);
  };

  const onReset = () => {
    if (!cfg) return;
    const d = cfg.joints.map((j) => j.default);
    setAngles(d);
    api.setPose(d).catch(() => undefined);
  };

  const onPick = (slot: string, option: string) => {
    setExprState((s) => ({ ...s, [slot]: option }));
    api.setExpression(slot, option).catch(() => undefined);
  };

  const maxLen = stage === "stand" ? 180 : 360;

  return (
    <div className="layout">
      <div className="left">
        <Viewport
          joints={cfg?.joints ?? []}
          frameRef={frameRef}
          exprHiddenRef={exprHiddenRef}
          followPhysicsRef={followPhysicsRef}
        />
        <div className="hud">
          <span className={"dot" + wsState} />ws{wsState}
          {" · "}
          {status.mode === "manual" ? "手动摆 pose" : status.mode === "train" ? `训练·${status.stage}` : "策略回放"}
          {status.running && ` · 迭代 ${status.iter}/${status.iters || "∞"} · 回合 ${status.episode}`}
          {status.running && ` · r=${status.reward.toFixed(2)}`}
        </div>
      </div>
      <div className="right">
        <TrainPanel
          stage={stage}
          running={status.running}
          paused={status.paused}
          iter={status.iter}
          iters={status.iters}
          history={status.history}
          maxLen={maxLen}
          onStage={setStage}
          onStart={() => api.trainStart(stage, 60, 2048, "train").catch(alert)}
          onStartWalk={() =>
            api.trainStart("walk", 60, 2048, "train",
              "outputs/checkpoints/stand_final.pt").catch(alert)
          }
          onPause={() => api.trainPause().catch(alert)}
          onStop={() => api.trainStop().catch(alert)}
        />
        <JointPanel
          joints={cfg?.joints ?? []}
          angles={angles}
          disabled={manualLocked}
          onChange={onJoint}
          onReset={onReset}
        />
        {rig && (
          <ExpressionPanel
            slots={rig.expressions.slots}
            state={exprState}
            onPick={onPick}
          />
        )}
      </div>
    </div>
  );
}
