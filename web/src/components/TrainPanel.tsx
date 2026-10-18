import { useEffect, useRef } from "react";
import type { HistPoint } from "../types";

interface Props {
  stage: string;
  running: boolean;
  paused: boolean;
  iter: number;
  iters: number;
  history: HistPoint[];
  maxLen: number;
  onStage: (s: string) => void;
  onStart: () => void;
  onStartWalk: () => void;
  onPause: () => void;
  onStop: () => void;
}

function Curve({ history, maxLen }: { history: HistPoint[]; maxLen: number }) {
  const ref = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const cv = ref.current!;
    const W = (cv.width = cv.clientWidth * 2);
    const H = (cv.height = cv.clientHeight * 2);
    const g = cv.getContext("2d")!;
    g.clearRect(0, 0, W, H);
    if (history.length < 2) return;
    const pad = 12;
    const xs = history.map((h) => h.iter);
    const xmin = Math.min(...xs);
    const xmax = Math.max(...xs, xmin + 1);
    const rets = history.map((h) => h.ret);
    const lens = history.map((h) => h.len / maxLen);
    const rmin = Math.min(...rets, 0);
    const rmax = Math.max(...rets, 1) * 1.05;
    const px = (x: number) => pad + ((x - xmin) / (xmax - xmin)) * (W - 2 * pad);
    const py = (y: number) => H - pad - ((y - rmin) / (rmax - rmin || 1)) * (H - 2 * pad);
    // 轴
    g.strokeStyle = "#39414d";
    g.beginPath();
    g.moveTo(pad, pad);
    g.lineTo(pad, H - pad);
    g.lineTo(W - pad, H - pad);
    g.stroke();
    // 回报（蓝）
    g.strokeStyle = "#5b9dff";
    g.lineWidth = 3;
    g.beginPath();
    history.forEach((h, i) => (i ? g.lineTo(px(h.iter), py(h.ret)) : g.moveTo(px(h.iter), py(h.ret))));
    g.stroke();
    // 存活率（绿）
    g.strokeStyle = "#4ade80";
    g.lineWidth = 2;
    g.beginPath();
    history.forEach((h, i) => (i ? g.lineTo(px(h.iter), py(h.len)) : g.moveTo(px(h.iter), py(h.len))));
    g.stroke();
  }, [history, maxLen]);
  return (
    <div className="curve-box">
      <canvas ref={ref} />
      <div className="hint">
        <span style={{ color: "#5b9dff" }}>━ 平均回报</span>{" "}
        <span style={{ color: "#4ade80" }}>━ 存活步数/上限</span>
      </div>
    </div>
  );
}

export default function TrainPanel(p: Props) {
  return (
    <div className="panel">
      <div className="panel-title">
        训练控制
        <span className={"badge " + (p.running ? "run" : "")}>
          {p.running ? (p.paused ? "已暂停" : "运行中") : "空闲"}
        </span>
      </div>
      <div className="train-row">
        <button
          className={"chip" + (p.stage === "stand" ? " on" : "")}
          disabled={p.running}
          onClick={() => p.onStage("stand")}
        >
          阶段1 · 站立
        </button>
        <button
          className={"chip" + (p.stage === "walk" ? " on" : "")}
          disabled={p.running}
          onClick={() => p.onStage("walk")}
        >
          阶段2 · 行走
        </button>
      </div>
      <div className="train-row">
        <button disabled={p.running} onClick={p.onStart}>开始训练</button>
        <button
          disabled={p.running}
          onClick={p.onStartWalk}
          title="加载站立 checkpoint，作为行走阶段的热启动"
        >
          站立→行走（热启动）
        </button>
        <button disabled={!p.running} onClick={p.onPause}>{p.paused ? "继续" : "暂停"}</button>
        <button className="danger" disabled={!p.running} onClick={p.onStop}>停止</button>
      </div>
      <div className="train-row stat">
        迭代 {p.iter}/{p.iters || "∞"} · 历史 {p.history.length} 点
      </div>
      <Curve history={p.history} maxLen={p.maxLen} />
    </div>
  );
}
