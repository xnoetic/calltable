"""单页看板：内联 CSS/JS，无任何外部依赖，每 3 秒轮询 hub。

视觉部分全部手写：配色 token（默认深色，跟随 ``prefers-color-scheme`` 提供浅色）、
内联 SVG 插画/图标、CSS 动效（尊重 ``prefers-reduced-motion``）。
页面被拆成若干字符串常量，最终拼成 ``PAGE``；对外只暴露 ``PAGE`` 与
``render_page()``，占位符替换逻辑保持不变。
"""

from __future__ import annotations

import json

# --------------------------------------------------------------------------
# 文档头
# --------------------------------------------------------------------------
_HEAD = """<!doctype html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark light">
<link rel="icon" href="data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'%3E%3Crect width='32' height='32' rx='9' fill='%23101828'/%3E%3Cpath d='M10 8h12a2 2 0 0 1 2 2v3a2.5 2.5 0 0 0 0 5v3a2 2 0 0 1-2 2H10a2 2 0 0 1-2-2v-3a2.5 2.5 0 0 0 0-5v-3a2 2 0 0 1 2-2Z' fill='none' stroke='%235ba8ff' stroke-width='2' stroke-linejoin='round'/%3E%3C/svg%3E">
<title>calltable · 验证任务队列</title>
"""

# --------------------------------------------------------------------------
# 样式：设计 token / 布局 / 组件 / 动效
# --------------------------------------------------------------------------
_STYLE = r"""<style>
/* ---------------------------------------------------------------- tokens */
:root {
  color-scheme: dark;
  --font: -apple-system, BlinkMacSystemFont, "Segoe UI", "Noto Sans SC",
    "PingFang SC", "Hiragino Sans GB", "Microsoft YaHei", system-ui, sans-serif;
  --mono: ui-monospace, SFMono-Regular, "SF Mono", "Cascadia Mono", Consolas,
    "Liberation Mono", monospace;

  --bg: #070a10;
  --bg-glow-1: rgba(91, 168, 255, .13);
  --bg-glow-2: rgba(155, 140, 255, .11);
  --bg-glow-3: rgba(61, 220, 151, .07);
  --grid-line: rgba(255, 255, 255, .028);

  --panel: rgba(19, 26, 37, .74);
  --panel-solid: #111927;
  --panel-2: rgba(32, 43, 58, .66);
  --panel-3: rgba(255, 255, 255, .045);
  --term-bg: #06090e;

  --line: rgba(255, 255, 255, .085);
  --line-2: rgba(255, 255, 255, .16);

  --text: #e9eff7;
  --text-2: #c2cfdd;
  --dim: #8ea0b5;
  --dim-2: #7b8ca1;

  --accent: #5ba8ff;
  --accent-2: #9b8cff;
  --ok: #3ddc97;
  --bad: #ff7070;
  --warn: #f5b544;
  --run: #5ba8ff;

  --accent-bg: rgba(91, 168, 255, .14);
  --ok-bg: rgba(61, 220, 151, .13);
  --bad-bg: rgba(255, 112, 112, .13);
  --warn-bg: rgba(245, 181, 68, .13);
  --run-bg: rgba(91, 168, 255, .14);

  --r-sm: 8px;
  --r: 12px;
  --r-lg: 16px;
  --r-xl: 22px;
  --shadow-sm: 0 1px 2px rgba(0, 0, 0, .28);
  --shadow: 0 16px 44px -24px rgba(0, 0, 0, .85), 0 1px 2px rgba(0, 0, 0, .3);
  --shadow-lift: 0 26px 60px -28px rgba(0, 0, 0, .9),
    0 0 0 1px rgba(91, 168, 255, .16);
  --ease: cubic-bezier(.22, .82, .24, 1);
}

@media (prefers-color-scheme: light) {
  :root {
    color-scheme: light;
    --bg: #eef2f8;
    --bg-glow-1: rgba(59, 130, 246, .16);
    --bg-glow-2: rgba(124, 92, 255, .12);
    --bg-glow-3: rgba(16, 185, 129, .09);
    --grid-line: rgba(15, 23, 42, .045);

    --panel: rgba(255, 255, 255, .82);
    --panel-solid: #ffffff;
    --panel-2: rgba(241, 245, 251, .92);
    --panel-3: rgba(15, 23, 42, .035);
    --term-bg: #0b1220;

    --line: rgba(15, 23, 42, .11);
    --line-2: rgba(15, 23, 42, .2);

    --text: #101a28;
    --text-2: #33455c;
    --dim: #5c6d82;
    --dim-2: #6a7a8e;

    --accent: #1f6feb;
    --accent-2: #7048e8;
    --ok: #0e9f6e;
    --bad: #d92c2c;
    --warn: #b7791f;
    --run: #1f6feb;

    --accent-bg: rgba(31, 111, 235, .1);
    --ok-bg: rgba(14, 159, 110, .1);
    --bad-bg: rgba(217, 44, 44, .09);
    --warn-bg: rgba(183, 121, 31, .12);
    --run-bg: rgba(31, 111, 235, .1);

    --shadow: 0 16px 40px -26px rgba(15, 23, 42, .45), 0 1px 2px rgba(15, 23, 42, .06);
    --shadow-lift: 0 24px 54px -30px rgba(15, 23, 42, .5),
      0 0 0 1px rgba(31, 111, 235, .16);
    --shadow-sm: 0 1px 2px rgba(15, 23, 42, .07);
  }
}

/* ------------------------------------------------------------------ base */
*, *::before, *::after { box-sizing: border-box; }
html { -webkit-text-size-adjust: 100%; }
body {
  margin: 0;
  min-height: 100vh;
  color: var(--text);
  background-color: var(--bg);
  background-image:
    radial-gradient(1050px 620px at 88% -12%, var(--bg-glow-1), transparent 62%),
    radial-gradient(820px 520px at 2% -4%, var(--bg-glow-2), transparent 58%),
    radial-gradient(760px 480px at 55% 108%, var(--bg-glow-3), transparent 62%);
  background-attachment: fixed;
  font-family: var(--font);
  font-size: 14px;
  line-height: 1.55;
  -webkit-font-smoothing: antialiased;
  text-rendering: optimizeLegibility;
}
body::before {
  content: "";
  position: fixed;
  inset: 0;
  z-index: 0;
  pointer-events: none;
  background-image:
    linear-gradient(var(--grid-line) 1px, transparent 1px),
    linear-gradient(90deg, var(--grid-line) 1px, transparent 1px);
  background-size: 52px 52px;
  -webkit-mask-image: radial-gradient(ellipse at 50% -10%, #000 10%, transparent 72%);
  mask-image: radial-gradient(ellipse at 50% -10%, #000 10%, transparent 72%);
}
h1, h2, h3, h4, p { margin: 0; }
[hidden] { display: none !important; }
a { color: var(--accent); text-decoration: none; transition: color .18s var(--ease); }
a:hover { color: var(--accent-2); }
code, kbd, pre, .mono { font-family: var(--mono); }
code { color: var(--accent); font-size: .94em; letter-spacing: -.01em; }
:focus-visible {
  outline: 2px solid var(--accent);
  outline-offset: 2px;
  border-radius: 6px;
}
::selection { background: var(--accent-bg); color: var(--text); }
* { scrollbar-width: thin; scrollbar-color: #2b3846 transparent; }
::-webkit-scrollbar { width: 10px; height: 10px; }
::-webkit-scrollbar-track { background: transparent; }
::-webkit-scrollbar-thumb {
  background: #263241;
  border: 2px solid transparent;
  border-radius: 99px;
  background-clip: content-box;
}
::-webkit-scrollbar-thumb:hover { background: #37495e; background-clip: content-box; }
.num { font-variant-numeric: tabular-nums; }
.nowrap { white-space: nowrap; }

/* ---------------------------------------------------------------- topbar */
.topbar {
  position: sticky;
  top: 0;
  z-index: 30;
  display: flex;
  align-items: center;
  gap: 14px;
  padding: 11px 24px;
  border-bottom: 1px solid var(--line);
  background: linear-gradient(180deg, rgba(8, 12, 19, .92), rgba(8, 12, 19, .66));
  -webkit-backdrop-filter: blur(16px) saturate(1.3);
  backdrop-filter: blur(16px) saturate(1.3);
}
@media (prefers-color-scheme: light) {
  .topbar { background: linear-gradient(180deg, rgba(255, 255, 255, .9), rgba(255, 255, 255, .62)); }
}
.brand { display: flex; align-items: center; gap: 11px; color: inherit; }
.brand:hover { color: inherit; }
.brand-mark {
  width: 36px;
  height: 36px;
  flex: 0 0 auto;
  filter: drop-shadow(0 6px 16px rgba(91, 168, 255, .35));
  transition: transform .5s var(--ease);
}
.brand:hover .brand-mark { transform: rotate(-6deg) scale(1.06); }
.brand-mark .tile { fill-opacity: .13; }
.brand-mark .edge { stroke-opacity: .5; }
.brand-mark .st1 { stop-color: #6cb6ff; }
.brand-mark .st2 { stop-color: #a99bff; }
@media (prefers-color-scheme: light) {
  .brand-mark .tile { fill-opacity: .09; }
  .brand-mark .edge { stroke-opacity: .9; }
  .brand-mark .st1 { stop-color: #2f7fe0; }
  .brand-mark .st2 { stop-color: #7350e6; }
}
.brand-text { display: flex; flex-direction: column; line-height: 1.15; }
.brand-name {
  font-size: 17px;
  font-weight: 680;
  letter-spacing: -.015em;
  background: linear-gradient(100deg, var(--text) 20%, var(--accent) 70%, var(--accent-2));
  -webkit-background-clip: text;
  background-clip: text;
  color: transparent;
}
.brand-sub { font-size: 11px; color: var(--dim-2); letter-spacing: .02em; }
.topbar-right { margin-left: auto; display: flex; align-items: center; gap: 10px; }

.live {
  display: inline-flex;
  align-items: center;
  gap: 8px;
  height: 30px;
  padding: 0 13px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel);
  color: var(--dim);
  font-size: 12px;
  white-space: nowrap;
}
.live .state-txt { color: var(--text-2); }
.live.offline { border-color: color-mix(in srgb, var(--bad) 40%, transparent); }
.pulse-dot {
  position: relative;
  display: inline-block;
  width: 8px;
  height: 8px;
  border-radius: 50%;
  background: var(--ok);
  flex: 0 0 auto;
  animation: breathe 2.6s ease-in-out infinite;
}
.pulse-dot::after {
  content: "";
  position: absolute;
  inset: -4px;
  border-radius: 50%;
  border: 1.5px solid var(--ok);
  opacity: 0;
  animation: ring 2.6s ease-out infinite;
}
.live.offline .pulse-dot { background: var(--bad); animation: none; }
.live.offline .pulse-dot::after { border-color: var(--bad); animation: none; opacity: .35; }

.spinner {
  width: 15px;
  height: 15px;
  border-radius: 50%;
  border: 2px solid var(--line-2);
  border-top-color: var(--accent);
  opacity: .35;
  transition: opacity .3s var(--ease);
  flex: 0 0 auto;
}
.spinner.active { animation: spin .7s linear infinite; opacity: 1; }

.ghost-btn {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 30px;
  padding: 0 12px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--text-2);
  font: inherit;
  font-size: 12px;
  cursor: pointer;
  transition: border-color .18s var(--ease), color .18s var(--ease),
    background .18s var(--ease), transform .18s var(--ease);
}
.ghost-btn:hover {
  border-color: var(--accent);
  color: var(--text);
  background: var(--accent-bg);
  text-decoration: none;
}
.ghost-btn:active { transform: translateY(1px); }
.ghost-btn.danger:hover {
  border-color: var(--bad);
  color: var(--bad);
  background: var(--bad-bg);
}
.ghost-btn[aria-pressed="true"] { border-color: var(--accent); color: var(--accent); }

/* ----------------------------------------------------------------- shell */
main {
  position: relative;
  z-index: 1;
  max-width: 1480px;
  margin: 0 auto;
  padding: 22px 24px 64px;
}
.view { animation: viewIn .42s var(--ease) both; }
.view-enter { animation: viewIn .42s var(--ease) both; }
.view.hidden, #detail { display: none; }
#detail.visible { display: block; }
.page-head {
  display: flex;
  align-items: flex-end;
  justify-content: space-between;
  gap: 16px;
  flex-wrap: wrap;
  margin-bottom: 16px;
}
.page-title {
  font-size: 21px;
  font-weight: 680;
  letter-spacing: -.02em;
  display: flex;
  align-items: center;
  gap: 10px;
}
.page-title .tick {
  display: inline-flex;
  align-items: center;
  height: 22px;
  padding: 0 8px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  font-size: 11px;
  font-weight: 500;
  letter-spacing: .04em;
}
.page-sub { color: var(--dim); font-size: 12.5px; margin-top: 3px; }
.chip-updated {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  height: 26px;
  padding: 0 11px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  font-size: 11.5px;
  font-variant-numeric: tabular-nums;
}

.block { margin-bottom: 26px; }
.block-head {
  display: flex;
  align-items: baseline;
  gap: 10px;
  margin: 0 2px 10px;
}
.block-head h3 {
  font-size: 13px;
  font-weight: 650;
  letter-spacing: .06em;
  text-transform: uppercase;
  color: var(--text-2);
  display: inline-flex;
  align-items: center;
  gap: 8px;
}
.block-head h3::before {
  content: "";
  width: 3px;
  height: 13px;
  border-radius: 2px;
  background: linear-gradient(180deg, var(--accent), var(--accent-2));
  box-shadow: 0 0 10px rgba(91, 168, 255, .55);
}
.block-hint { color: var(--dim-2); font-size: 11.5px; }
.only-narrow { display: none; }
.hide-narrow { display: inline; }

/* ----------------------------------------------------------------- stats */
.stats {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(158px, 1fr));
  gap: 12px;
  margin-bottom: 24px;
}
.stat {
  position: relative;
  overflow: hidden;
  padding: 13px 15px 12px;
  border-radius: var(--r);
  border: 1px solid var(--line);
  background: linear-gradient(158deg, var(--panel-3), transparent 65%),
    var(--panel);
  box-shadow: var(--shadow);
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);
  animation: rise .5s var(--ease) both;
  transition: border-color .25s var(--ease), transform .25s var(--ease);
}
.stat:hover { transform: translateY(-2px); border-color: var(--line-2); }
.stat::after {
  content: "";
  position: absolute;
  left: 15px;
  right: 15px;
  top: 0;
  height: 1px;
  background: linear-gradient(90deg, transparent, var(--accent), transparent);
  opacity: .5;
}
.stat-ico {
  position: absolute;
  top: 12px;
  right: 12px;
  width: 30px;
  height: 30px;
  display: grid;
  place-items: center;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  transition: color .3s var(--ease), background .3s var(--ease),
    border-color .3s var(--ease);
}
.stat-ico .icon { width: 15px; height: 15px; }
.stat[data-tone="ok"] .stat-ico {
  color: var(--ok);
  border-color: color-mix(in srgb, var(--ok) 30%, transparent);
  background: var(--ok-bg);
}
.stat[data-tone="bad"] .stat-ico {
  color: var(--bad);
  border-color: color-mix(in srgb, var(--bad) 30%, transparent);
  background: var(--bad-bg);
}
.stat[data-tone="warn"] .stat-ico {
  color: var(--warn);
  border-color: color-mix(in srgb, var(--warn) 30%, transparent);
  background: var(--warn-bg);
}
.stat[data-tone="run"] .stat-ico,
.stat[data-tone="accent"] .stat-ico {
  color: var(--accent);
  border-color: color-mix(in srgb, var(--accent) 30%, transparent);
  background: var(--accent-bg);
}
.stat-label {
  padding-right: 34px;
  font-size: 10.5px;
  letter-spacing: .1em;
  text-transform: uppercase;
  color: var(--dim);
  font-weight: 600;
}
.stat-value {
  font-size: 27px;
  font-weight: 660;
  letter-spacing: -.03em;
  line-height: 1.25;
  font-variant-numeric: tabular-nums;
  transition: color .3s var(--ease);
}
.stat-foot { font-size: 11px; color: var(--dim-2); }
.stat[data-tone="ok"] .stat-value { color: var(--ok); }
.stat[data-tone="bad"] .stat-value { color: var(--bad); }
.stat[data-tone="warn"] .stat-value { color: var(--warn); }
.stat[data-tone="run"] .stat-value { color: var(--run); }
.stat[data-tone="accent"] .stat-value { color: var(--accent); }
.stat.bump .stat-value { animation: bump .45s var(--ease); }

/* ----------------------------------------------------------------- cards */
.card {
  position: relative;
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--panel);
  box-shadow: var(--shadow);
  -webkit-backdrop-filter: blur(12px) saturate(1.1);
  backdrop-filter: blur(12px) saturate(1.1);
}
.card::before {
  content: "";
  position: absolute;
  left: 18%;
  right: 18%;
  top: -1px;
  height: 1px;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .26), transparent);
  opacity: .7;
  pointer-events: none;
}
.card.pad { padding: 15px 16px 16px; }
.card-in { animation: rise .5s var(--ease) both; }
.card-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}
.card-head h3 {
  font-size: 12px;
  font-weight: 650;
  letter-spacing: .07em;
  text-transform: uppercase;
  color: var(--text-2);
}
.card-hint { margin-left: auto; color: var(--dim-2); font-size: 11.5px; }

/* ----------------------------------------------------------------- hosts */
.hosts {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(min(310px, 100%), 1fr));
  gap: 14px;
}
.hosts.is-empty { display: block; }
.host {
  min-width: 0;
  padding: 14px 15px 13px;
  transition: transform .28s var(--ease), box-shadow .28s var(--ease),
    border-color .28s var(--ease);
}
.host:hover {
  transform: translateY(-3px);
  border-color: var(--line-2);
  box-shadow: var(--shadow-lift);
}
.host.offline { opacity: .82; }
.host.offline::after {
  content: "";
  position: absolute;
  inset: 0;
  border-radius: inherit;
  pointer-events: none;
  background: repeating-linear-gradient(
    135deg,
    transparent 0 9px,
    rgba(255, 255, 255, .014) 9px 18px
  );
}
.host-top { display: flex; align-items: flex-start; gap: 11px; }
.host-ico {
  flex: 0 0 auto;
  width: 40px;
  height: 40px;
  display: grid;
  place-items: center;
  border-radius: 12px;
  border: 1px solid var(--line);
  background: linear-gradient(155deg, var(--accent-bg), transparent 78%);
  color: var(--accent);
  transition: transform .3s var(--ease), box-shadow .3s var(--ease);
}
.host:hover .host-ico {
  transform: translateY(-1px) scale(1.04);
  box-shadow: 0 8px 20px -10px rgba(91, 168, 255, .6);
}
.host-ico .icon { width: 21px; height: 21px; }
.host-id { min-width: 0; flex: 1; }
.host-title {
  display: flex;
  align-items: center;
  gap: 9px;
  flex-wrap: wrap;
}
.hname {
  font-size: 14.5px;
  font-weight: 640;
  letter-spacing: -.01em;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.host-state {
  display: inline-flex;
  align-items: center;
  gap: 6px;
  padding: 1px 8px 1px 6px;
  border-radius: 999px;
  border: 1px solid var(--line);
  font-size: 10.5px;
  color: var(--dim);
  font-variant-numeric: tabular-nums;
}
.host-state.on { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 30%, transparent); background: var(--ok-bg); }
.host-state.off { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 30%, transparent); background: var(--bad-bg); }
.host-state .pulse-dot { width: 7px; height: 7px; }
.host-state.off .pulse-dot { background: var(--bad); animation: none; }
.host-state.off .pulse-dot::after { border-color: var(--bad); animation: none; }
.host-meta {
  color: var(--dim-2);
  font-size: 11.5px;
  margin-top: 3px;
  overflow: hidden;
  text-overflow: ellipsis;
}
.host-alert {
  margin-top: 10px;
  padding: 7px 10px;
  border-radius: 9px;
  border: 1px solid color-mix(in srgb, var(--warn) 35%, transparent);
  background: var(--warn-bg);
  color: var(--warn);
  font-size: 11.5px;
  display: flex;
  align-items: center;
  gap: 7px;
}
.host-alert .icon { width: 14px; height: 14px; flex: 0 0 auto; }
.slot-list { display: grid; gap: 8px; margin-top: 12px; }
.slot-head {
  display: flex;
  justify-content: space-between;
  align-items: baseline;
  gap: 8px;
  font-size: 11.5px;
  color: var(--dim);
}
.slot-name { letter-spacing: .02em; }
.slot-num { font-variant-numeric: tabular-nums; color: var(--text-2); }
.bar {
  position: relative;
  height: 6px;
  margin-top: 4px;
  border-radius: 999px;
  background: var(--panel-3);
  overflow: hidden;
}
.bar > i {
  display: block;
  height: 100%;
  width: 0;
  border-radius: inherit;
  background: linear-gradient(90deg, var(--accent), var(--accent-2));
  box-shadow: 0 0 12px -2px rgba(91, 168, 255, .7);
  transition: width .65s var(--ease), background .35s var(--ease);
}
.bar > i::after {
  content: "";
  display: block;
  height: 100%;
  width: 100%;
  background: linear-gradient(90deg, transparent, rgba(255, 255, 255, .35), transparent);
  transform: translateX(-100%);
  animation: sheen 3.6s var(--ease) infinite;
  opacity: .5;
}
.bar.idle > i { background: transparent; box-shadow: none; }
.bar.idle > i::after { animation: none; }
.bar.full > i {
  background: linear-gradient(90deg, var(--warn), var(--bad));
  box-shadow: 0 0 12px -2px rgba(255, 112, 112, .6);
}
.lock-row { display: flex; flex-wrap: wrap; gap: 6px; margin-top: 9px; }
.lock-row:empty { display: none; }
.host-foot {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  margin-top: 10px;
}
.host-running {
  display: grid;
  grid-template-columns: minmax(0, 1fr);
  gap: 5px;
  margin-top: 10px;
}
.host-running:empty { display: none; }
.run-item {
  min-width: 0;
  display: flex;
  align-items: flex-start;
  gap: 8px;
  padding: 6px 9px;
  border-radius: 8px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--text-2);
  font-size: 11.5px;
  transition: border-color .2s var(--ease), background .2s var(--ease);
}
.run-item:hover {
  border-color: var(--accent);
  background: var(--accent-bg);
  color: var(--text);
  text-decoration: none;
}
.run-dot {
  width: 6px;
  height: 6px;
  margin-top: 5px;
  border-radius: 50%;
  background: var(--run);
  box-shadow: 0 0 0 0 var(--run);
  animation: runPulse 1.8s ease-out infinite;
  flex: 0 0 auto;
}
.run-main { min-width: 0; flex: 1; display: flex; flex-direction: column; }
.run-id { font-size: 11px; line-height: 1.45; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.run-sub { display: flex; gap: 6px; min-width: 0; color: var(--dim); line-height: 1.4; }
.run-proj { white-space: nowrap; }
.run-label {
  flex: 1 1 auto;
  min-width: 0;
  color: var(--dim-2);
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}

/* ------------------------------------------------------- chips & badges */
.chip {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  font-size: 11px;
  font-variant-numeric: tabular-nums;
  white-space: nowrap;
}
.chip-accent { color: var(--accent); border-color: color-mix(in srgb, var(--accent) 32%, transparent); background: var(--accent-bg); }
.chip-warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 32%, transparent); background: var(--warn-bg); }
.chip-bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 32%, transparent); background: var(--bad-bg); }
.chip-ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 30%, transparent); background: var(--ok-bg); }
.chip .icon { width: 12px; height: 12px; }

.badge {
  display: inline-flex;
  align-items: center;
  gap: 5px;
  padding: 2px 9px 2px 7px;
  border-radius: 999px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  font-size: 11.5px;
  font-weight: 600;
  white-space: nowrap;
  letter-spacing: .01em;
}
.badge .icon { width: 13px; height: 13px; }
.badge .ic-arc { transform-origin: center; animation: spin 1.1s linear infinite; }
.badge.st-running { color: var(--run); border-color: color-mix(in srgb, var(--run) 34%, transparent); background: var(--run-bg); }
.badge.st-ok { color: var(--ok); border-color: color-mix(in srgb, var(--ok) 32%, transparent); background: var(--ok-bg); }
.badge.st-bad { color: var(--bad); border-color: color-mix(in srgb, var(--bad) 34%, transparent); background: var(--bad-bg); }
.badge.st-warn { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 34%, transparent); background: var(--warn-bg); }
.badge.st-queued { color: var(--dim); }
.badge.st-queued .icon { opacity: .8; }

/* ---------------------------------------------------------------- tables */
.table-wrap {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  background: var(--panel);
  box-shadow: var(--shadow);
  overflow-x: auto;
  -webkit-overflow-scrolling: touch;
  -webkit-backdrop-filter: blur(10px);
  backdrop-filter: blur(10px);
}
table { width: 100%; border-collapse: collapse; font-size: 12.5px; }
thead th {
  position: relative;
  text-align: left;
  padding: 9px 12px;
  background: var(--panel-2);
  color: var(--dim);
  font-size: 10.5px;
  font-weight: 650;
  letter-spacing: .08em;
  text-transform: uppercase;
  white-space: nowrap;
  border-bottom: 1px solid var(--line);
}
tbody td {
  padding: 8px 12px;
  border-bottom: 1px solid var(--line);
  color: var(--text-2);
  white-space: nowrap;
  vertical-align: middle;
}
tbody tr:last-child td { border-bottom: none; }
tbody tr.clickable { cursor: pointer; }
tbody tr.clickable td { transition: background .18s var(--ease); }
tbody tr.clickable:hover td { background: var(--panel-3); }
tbody tr.clickable:hover td:first-child code { color: var(--text); }
td.wrap { white-space: normal; min-width: 150px; max-width: 320px; }
td.dim, td .dim { color: var(--dim); }
td.td-strong { color: var(--text); font-weight: 550; }
.joblink { display: inline-flex; }
.mob-status { display: none; margin-top: 4px; }
.id-short { display: none; }
.joblink code { font-size: 12px; color: var(--text-2); }
.joblink:hover code { color: var(--accent); text-decoration: underline; }

tr.row-in { animation: rowIn .46s var(--ease) both; }
tr.row-flash td { animation: rowFlash .9s var(--ease); }

/* ------------------------------------------------------------ empty state */
.empty {
  display: flex;
  flex-direction: column;
  align-items: center;
  gap: 10px;
  padding: 30px 16px 34px;
  border: 1px dashed var(--line-2);
  border-radius: var(--r-lg);
  background: linear-gradient(180deg, var(--panel-3), transparent);
  color: var(--dim-2);
  text-align: center;
  font-size: 12.5px;
}
.empty-art {
  width: 138px;
  height: 92px;
  color: var(--dim-2);
  animation: floaty 7s ease-in-out infinite;
}
.empty-art .art-accent { color: var(--accent); }
.empty strong { color: var(--text-2); font-weight: 600; font-size: 13px; }
.empty p { max-width: 34em; }

/* ---------------------------------------------------------------- detail */
.back {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  margin-bottom: 13px;
  height: 30px;
  padding: 0 12px 0 9px;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--panel-3);
  color: var(--dim);
  font-size: 12px;
  transition: color .18s var(--ease), border-color .18s var(--ease),
    background .18s var(--ease), transform .18s var(--ease);
}
.back:hover {
  color: var(--text);
  border-color: var(--accent);
  background: var(--accent-bg);
  text-decoration: none;
  transform: translateX(-2px);
}
.hero {
  display: grid;
  grid-template-columns: minmax(0, 1fr) 232px;
  gap: 18px;
  align-items: center;
  padding: 20px 22px;
  margin-bottom: 16px;
  overflow: hidden;
}
.hero::after {
  content: "";
  position: absolute;
  right: -60px;
  top: -80px;
  width: 320px;
  height: 320px;
  border-radius: 50%;
  background: radial-gradient(circle, var(--accent-bg), transparent 62%);
  pointer-events: none;
}
.hero-body { min-width: 0; position: relative; z-index: 1; }
.hero-eyebrow {
  font-size: 10.5px;
  letter-spacing: .18em;
  text-transform: uppercase;
  color: var(--dim-2);
  font-weight: 650;
}
.hero-id {
  font-family: var(--mono);
  font-size: 24px;
  font-weight: 600;
  letter-spacing: -.02em;
  margin: 3px 0 4px;
  overflow-wrap: anywhere;
}
.hero-sub { color: var(--dim); font-size: 12.5px; }
.hero-status { margin-top: 11px; display: flex; flex-wrap: wrap; gap: 8px; align-items: center; }
.hero-art {
  position: relative;
  z-index: 1;
  color: var(--accent);
  display: grid;
  place-items: center;
}
.hero-svg { width: 100%; height: auto; max-width: 232px; display: block; }
.hero-art[data-status="succeeded"] { color: var(--ok); }
.hero-art[data-status="failed"], .hero-art[data-status="error"] { color: var(--bad); }
.hero-art[data-status="timeout"], .hero-art[data-status="cancelled"] { color: var(--warn); }
.hero-art[data-status="queued"] { color: var(--dim); }
.hero-float { animation: floaty 8s ease-in-out infinite; }

/* 取号 → 排队 → 叫号 步骤条 */
.steps { display: flex; align-items: center; gap: 0; margin-top: 16px; flex-wrap: wrap; row-gap: 10px; }
.step { display: flex; align-items: center; gap: 8px; }
.step-dot {
  position: relative;
  width: 22px;
  height: 22px;
  border-radius: 50%;
  display: grid;
  place-items: center;
  border: 1.5px solid var(--line-2);
  background: var(--panel-solid);
  color: var(--dim-2);
  font-size: 10px;
  font-variant-numeric: tabular-nums;
  transition: border-color .3s var(--ease), color .3s var(--ease),
    background .3s var(--ease), box-shadow .3s var(--ease);
  flex: 0 0 auto;
}
.step-dot .icon { width: 12px; height: 12px; }
.step-label { font-size: 11.5px; color: var(--dim-2); transition: color .3s var(--ease); }
.step-line {
  width: 34px;
  height: 2px;
  margin: 0 8px;
  border-radius: 2px;
  background: var(--line);
  position: relative;
  overflow: hidden;
}
.step.done .step-dot { border-color: var(--ok); color: var(--ok); background: var(--ok-bg); }
.step.done .step-label { color: var(--dim); }
.step.done .step-line { background: color-mix(in srgb, var(--ok) 45%, var(--line)); }
.step.active .step-dot {
  border-color: var(--accent);
  color: var(--accent);
  background: var(--accent-bg);
  box-shadow: 0 0 0 4px var(--accent-bg);
  animation: dotPulse 2.2s ease-in-out infinite;
}
.step.active .step-label { color: var(--text); font-weight: 600; }
.step.bad .step-dot { border-color: var(--bad); color: var(--bad); background: var(--bad-bg); }
.step.bad .step-label { color: var(--bad); }

.detail-grid {
  display: grid;
  grid-template-columns: minmax(0, 1.3fr) minmax(0, 1fr);
  gap: 14px;
  margin-bottom: 26px;
}
.detail-grid > .card { display: flex; flex-direction: column; }
.detail-grid > .card > .toolbar { margin-top: auto; padding-top: 14px; }
.meta {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(158px, 1fr));
  gap: 9px 14px;
}
.meta-item { min-width: 0; }
.meta-k {
  display: block;
  font-size: 10.5px;
  letter-spacing: .07em;
  text-transform: uppercase;
  color: var(--dim-2);
}
.meta-v {
  display: block;
  font-size: 12.5px;
  color: var(--text);
  overflow-wrap: anywhere;
}
.meta-v.mono { font-family: var(--mono); font-size: 12px; }
.meta-item.wide {
  grid-column: 1 / -1;
  padding-top: 6px;
  border-top: 1px dashed var(--line);
}
.meta-item.wide .meta-v { font-family: var(--mono); font-size: 12px; color: var(--text-2); }
.meta-item.cmd .meta-v {
  max-height: 12.5em;
  overflow-y: auto;
  margin-top: 4px;
  padding: 7px 10px;
  border: 1px solid var(--line);
  border-radius: 8px;
  background: var(--panel-3);
  overscroll-behavior: contain;
}
.toolbar { display: flex; gap: 9px; flex-wrap: wrap; margin-top: 16px; }

.term {
  border: 1px solid var(--line);
  border-radius: var(--r-lg);
  overflow: hidden;
  background: var(--term-bg);
  box-shadow: var(--shadow);
}
.term-bar {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 8px 13px;
  border-bottom: 1px solid rgba(255, 255, 255, .07);
  background: rgba(255, 255, 255, .032);
}
.term-dots { display: inline-flex; gap: 5px; }
.term-dots i { width: 9px; height: 9px; border-radius: 50%; display: inline-block; opacity: .8; }
.term-dots i:nth-child(1) { background: #ff5f57; }
.term-dots i:nth-child(2) { background: #febc2e; }
.term-dots i:nth-child(3) { background: #28c840; }
.term-title { font-family: var(--mono); font-size: 11px; color: #7b8b9d; }
.term-live { margin-left: auto; display: inline-flex; align-items: center; gap: 6px; font-size: 10.5px; color: #7b8b9d; }
pre.log {
  margin: 0;
  padding: 12px 15px 16px;
  max-height: 460px;
  overflow: auto;
  background: none;
  border: 0;
  color: #c6d4e3;
  font-family: var(--mono);
  font-size: 12px;
  line-height: 1.62;
  white-space: pre-wrap;
  word-break: break-word;
  tab-size: 4;
}
pre.log:empty::before { content: "（暂无日志输出）"; color: #5d6b7c; }

.chart-body {
  position: relative;
  flex: 1 1 auto;
  min-height: 190px;
  margin-top: 4px;
}
#d-chart {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  display: block;
  border-radius: var(--r-sm);
}
.legend { color: var(--dim); font-size: 11.5px; margin-top: 9px; display: flex; flex-wrap: wrap; gap: 4px 14px; }.legend .lg { display: inline-flex; align-items: center; gap: 6px; }
.legend .sw { width: 18px; height: 3px; border-radius: 2px; display: inline-block; }

.artifacts { display: grid; gap: 8px; }
.artifact {
  display: flex;
  align-items: center;
  gap: 10px;
  padding: 9px 12px;
  border-radius: var(--r);
  border: 1px solid var(--line);
  background: var(--panel);
  transition: border-color .2s var(--ease), transform .2s var(--ease),
    box-shadow .2s var(--ease);
}
.artifact:hover {
  border-color: var(--accent);
  transform: translateY(-1px);
  box-shadow: var(--shadow);
}
.artifact .file-ico {
  width: 30px;
  height: 30px;
  flex: 0 0 auto;
  display: grid;
  place-items: center;
  border-radius: 9px;
  border: 1px solid var(--line);
  background: var(--accent-bg);
  color: var(--accent);
}
.artifact .file-ico .icon { width: 15px; height: 15px; }
.artifact .file-main { min-width: 0; display: flex; flex-direction: column; }
.artifact .file-name {
  font-family: var(--mono);
  font-size: 12px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
}
.artifact .file-meta { color: var(--dim-2); font-size: 11px; font-variant-numeric: tabular-nums; }
.artifact .file-dl { margin-left: auto; color: var(--dim-2); }
.artifact:hover .file-dl { color: var(--accent); }
.artifact .file-dl .icon { width: 16px; height: 16px; }

/* ------------------------------------------------------------- token box */
.token-box {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  padding: 13px 15px;
  margin-bottom: 18px;
  border-radius: var(--r);
  border: 1px solid color-mix(in srgb, var(--warn) 34%, transparent);
  background: var(--warn-bg);
  color: var(--warn);
  font-size: 12.5px;
}
.token-box[hidden] { display: none; }
.token-box label { font-weight: 600; letter-spacing: .02em; }
.token-box input {
  flex: 1 1 240px;
  min-width: 0;
  height: 32px;
  padding: 0 11px;
  border-radius: 9px;
  border: 1px solid var(--line-2);
  background: var(--panel-solid);
  color: var(--text);
  font: inherit;
  font-family: var(--mono);
  font-size: 12.5px;
}
.token-box input:focus-visible { outline: 2px solid var(--accent); outline-offset: 1px; }
.sub { color: var(--dim); font-size: 11.5px; }
.icon { width: 14px; height: 14px; display: block; }

/* ------------------------------------------------------------- keyframes */
@keyframes spin { to { transform: rotate(360deg); } }
@keyframes rise { from { opacity: 0; transform: translateY(10px); } to { opacity: 1; transform: none; } }
@keyframes viewIn { from { opacity: 0; transform: translateY(8px); } to { opacity: 1; transform: none; } }
@keyframes rowIn {
  0% { opacity: 0; transform: translateY(-4px); background: var(--accent-bg); }
  60% { background: var(--accent-bg); }
  100% { opacity: 1; transform: none; background: transparent; }
}
@keyframes rowFlash {
  0% { background: var(--accent-bg); }
  100% { background: transparent; }
}
@keyframes bump {
  0% { transform: scale(1); }
  35% { transform: scale(1.14); }
  100% { transform: scale(1); }
}
@keyframes breathe {
  0%, 100% { box-shadow: 0 0 0 0 rgba(61, 220, 151, .5); }
  55% { box-shadow: 0 0 0 6px rgba(61, 220, 151, 0); }
}
@keyframes ring {
  0% { opacity: .55; transform: scale(.62); }
  70% { opacity: 0; transform: scale(1.5); }
  100% { opacity: 0; transform: scale(1.5); }
}
@keyframes runPulse {
  0% { box-shadow: 0 0 0 0 rgba(91, 168, 255, .55); }
  70% { box-shadow: 0 0 0 5px rgba(91, 168, 255, 0); }
  100% { box-shadow: 0 0 0 0 rgba(91, 168, 255, 0); }
}
@keyframes sheen {
  0% { transform: translateX(-100%); }
  55%, 100% { transform: translateX(220%); }
}
@keyframes floaty {
  0%, 100% { transform: translateY(0); }
  50% { transform: translateY(-5px); }
}
@keyframes dotPulse {
  0%, 100% { box-shadow: 0 0 0 3px var(--accent-bg); }
  50% { box-shadow: 0 0 0 7px transparent; }
}

/* ------------------------------------------------------------ responsive */
@media (max-width: 1000px) {
  .detail-grid { grid-template-columns: minmax(0, 1fr); }
  .hero { grid-template-columns: minmax(0, 1fr) 170px; }
}
@media (max-width: 780px) {
  .topbar { padding: 9px 14px; gap: 10px; flex-wrap: wrap; }
  .brand-sub { display: none; }
  .brand-name { font-size: 16px; }
  .topbar-right { gap: 7px; }
  .ghost-btn { height: 28px; padding: 0 10px; font-size: 11.5px; }
  .live { height: 28px; padding: 0 10px; font-size: 11.5px; }
  main { padding: 16px 13px 52px; }
  .page-title { font-size: 18px; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 9px; }
  .stats > .stat:last-child:nth-child(odd) { grid-column: 1 / -1; }
  .stat { padding: 11px 12px 10px; }
  .stat-value { font-size: 22px; }
  .stats > .stat:last-child:nth-child(odd) .stat-ico { top: 10px; right: 10px; }
  .hosts { grid-template-columns: minmax(0, 1fr); }
  .hero { grid-template-columns: minmax(0, 1fr); padding: 16px 15px; }
  .hero-art { display: none; }
  .hero-id { font-size: 19px; }
  .meta { grid-template-columns: repeat(auto-fill, minmax(132px, 1fr)); }
  .step-line { width: 16px; margin: 0 5px; }
  .step-label { font-size: 10.5px; }
  .block { margin-bottom: 20px; }
  .only-narrow { display: inline; }
  .hide-narrow { display: none; }
  /* 窄屏：状态搬到 id 下方，隐藏次要列，保证首屏看到 id + 状态 + label */
  .c-host, .c-slot, .c-lock, .c-sub, .c-sha, .c-dirty, .c-start, .c-status,
  .c-cpu, .c-mem, .c-est { display: none; }
  .mob-status { display: inline-flex; }
  .id-full { display: none; }
  .id-short { display: inline; }
  thead th, tbody td { padding: 7px 8px; }
  thead th { font-size: 10px; }
  tbody td { font-size: 11.5px; }
  /* id 在窄屏折成两行，把宽度让给 label；状态徒章单独一行放在 id 下方 */
  .c-id { white-space: normal; }
  .c-id code {
    display: block;
    min-width: 9.5em;
    max-width: 120px;
    white-space: normal;
    line-height: 1.35;
  }
  .c-id code .nw { white-space: nowrap; }
  .mob-status { display: flex; width: fit-content; margin-top: 5px; }
  td.wrap { white-space: normal; min-width: 0; max-width: none; overflow-wrap: break-word; }
  pre.log { max-height: 320px; }
}
@media (max-width: 470px) {
  .brand-sub { display: none; }
  .stats { grid-template-columns: repeat(2, minmax(0, 1fr)); }
  #history .c-proj, #queue .c-proj { display: none; }
  .block-head { flex-wrap: wrap; }
  .hide-xs { display: none; }
}

@media (prefers-reduced-motion: reduce) {
  *, *::before, *::after {
    animation-duration: .001ms !important;
    animation-iteration-count: 1 !important;
    transition-duration: .001ms !important;
    scroll-behavior: auto !important;
  }
  .pulse-dot, .pulse-dot::after, .run-dot, .empty-art, .hero-float,
  .bar > i::after, .badge .ic-arc { animation: none !important; }}
</style>"""

# --------------------------------------------------------------------------
# 正文
# --------------------------------------------------------------------------
_BODY = r"""<header class="topbar">
  <a class="brand" href="#/" aria-label="calltable 看板首页">
    <svg class="brand-mark" viewBox="0 0 40 40" aria-hidden="true">
      <defs>
        <linearGradient id="brandGrad" x1="0" y1="0" x2="1" y2="1">
          <stop class="st1" offset="0" stop-color="#6cb6ff"/>
          <stop class="st2" offset="1" stop-color="#a99bff"/>
        </linearGradient>
      </defs>
      <rect class="tile" x="1.5" y="1.5" width="37" height="37" rx="11" fill="url(#brandGrad)"/>
      <rect class="edge" x="1.5" y="1.5" width="37" height="37" rx="11" fill="none" stroke="url(#brandGrad)"/>
      <path d="M11 10h12.6a2 2 0 0 1 2 2v3.4a2.8 2.8 0 0 0 0 5.6V26a2 2 0 0 1-2 2H11a2 2 0 0 1-2-2v-5a2.8 2.8 0 0 0 0-5.6V12a2 2 0 0 1 2-2Z" fill="none" stroke="url(#brandGrad)" stroke-width="1.7" stroke-linejoin="round"/>
      <path d="M15.7 11.4v15.2" stroke="url(#brandGrad)" stroke-width="1.3" stroke-dasharray="1.8 2.4" opacity=".75"/>
      <path d="M19.6 15.2h5.2M19.6 19h5.2M19.6 22.8h3.2" stroke="url(#brandGrad)" stroke-width="1.7" stroke-linecap="round"/>
    </svg>
    <span class="brand-text">
      <span class="brand-name">calltable</span>
      <span class="brand-sub">验证任务队列 · 取号 · 排队 · 叫号</span>
    </span>
  </a>
  <div class="topbar-right">
    <span class="live" id="live" role="status" aria-live="polite" title="hub 连接状态">
      <i class="pulse-dot"></i><span class="state-txt">连接中…</span>
    </span>
    <span class="spinner" id="spin" aria-hidden="true" title="正在刷新"></span>
    <a class="ghost-btn" id="beszel" href="#" target="_blank" rel="noreferrer">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M3.8 12h3.4l2-5.2 3.2 10.4 2.2-6.4 1.6 3.2h3.8"/></svg>
      <span class="hide-xs">Beszel 监控</span>
    </a>
    <button class="ghost-btn" type="button" onclick="showTokenBox()">Token</button>
  </div>
</header>

<main>
  <div class="token-box" id="tokenbox" hidden>
    <label for="tokeninput">需要 hub token</label>
    <input id="tokeninput" type="password" autocomplete="off" spellcheck="false" placeholder="粘贴 hub token 后回车保存">
    <button class="ghost-btn" type="button" onclick="saveToken()">保存并刷新</button>
  </div>

  <div id="overview" class="view">
    <div class="page-head">
      <div>
        <h1 class="page-title">调度概览</h1>
        <p class="page-sub" id="summary" aria-live="polite">正在连接 hub…</p>
      </div>
      <span class="chip-updated" id="updated">尚未刷新</span>
    </div>

    <div class="stats" id="stats" aria-label="关键指标"></div>

    <section class="block">
      <div class="block-head"><h3>主机</h3><span class="block-hint" id="host-hint">心跳 / 槽位 / 锁</span></div>
      <div class="hosts" id="hosts"></div>
    </section>

    <section class="block">
      <div class="block-head"><h3>排队</h3><span class="block-hint hide-narrow">按槽位与锁竞争排序</span><span class="block-hint only-narrow">已等 / 前面人数</span></div>
      <div id="queue"></div>
    </section>

    <section class="block">
      <div class="block-head"><h3>最近任务</h3><span class="block-hint hide-narrow">最近 100 条</span><span class="block-hint only-narrow">状态贴在任务号下方</span></div>
      <div id="history"></div>
    </section>
  </div>

  <div id="detail" class="view">
    <a class="back" href="#/">
      <svg class="icon" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true"><path d="M14.5 5.5 8 12l6.5 6.5"/></svg>
      返回看板
    </a>

    <section class="card hero">
      <div class="hero-body">
        <div class="hero-eyebrow">validation job</div>
        <h2 class="hero-id" id="d-id">—</h2>
        <p class="hero-sub" id="d-sub">正在载入任务…</p>
        <div class="hero-status" id="d-status"></div>
        <div class="steps" id="d-steps"></div>
      </div>
      <div class="hero-art" id="d-hero-art"></div>
    </section>

    <div class="detail-grid">
      <section class="card pad">
        <div class="card-head"><h3>任务信息</h3><span class="card-hint" id="d-exit"></span></div>
        <div class="meta" id="d-meta"></div>
        <div class="toolbar">
          <button class="ghost-btn" type="button" id="follow" aria-pressed="true" onclick="toggleFollow()">自动刷新：开</button>
          <button class="ghost-btn danger" type="button" id="d-cancel" onclick="cancelJob()">取消任务</button>
        </div>
      </section>

      <section class="card pad">
        <div class="card-head"><h3>资源采样</h3><span class="card-hint" id="d-samples">—</span></div>
        <div class="chart-body">
          <svg id="d-chart" viewBox="0 0 640 190" role="img" aria-label="任务资源采样曲线"></svg>
        </div>
        <p class="legend" id="d-legend"></p>
      </section>
    </div>

    <section class="block">
      <div class="block-head"><h3>日志</h3><span class="block-hint" id="d-logsize">末尾 200 KB</span></div>
      <div class="term">
        <div class="term-bar">
          <span class="term-dots"><i></i><i></i><i></i></span>
          <span class="term-title" id="d-term-title">tail</span>
          <span class="term-live"><span class="pulse-dot"></span>跟随中</span>
        </div>
        <pre class="log" id="d-log"></pre>
      </div>
    </section>

    <section class="block">
      <div class="block-head"><h3>产物</h3><span class="block-hint" id="d-artifact-count"></span></div>
      <div id="d-artifacts" class="artifacts"></div>
    </section>

    <section class="block">
      <div class="block-head"><h3>叫号通知</h3><span class="block-hint" id="d-webhook-count"></span></div>
      <div id="d-webhooks" class="artifacts"></div>
    </section>
  </div>

  <noscript><p class="sub">看板需要启用 JavaScript 才能轮询 hub。</p></noscript>
</main>
"""

# --------------------------------------------------------------------------
# 脚本（分块拼装）
# --------------------------------------------------------------------------
_JS_UTIL = r"""
const TOKEN = __TOKEN__;
const BESZEL = __BESZEL__;
const REDUCED = window.matchMedia('(prefers-reduced-motion: reduce)');

let detailId = null, follow = true, logOffset = 0, logText = '', timer = null;
let busy = false, hadData = false;

/* --------------------------------------------------------------- 工具函数 */
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, function (c) {
    return ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' })[c];
  });
}
function fmtDur(sec) {
  if (sec == null || isNaN(sec)) return '–';
  sec = Math.max(0, Math.round(sec));
  if (sec < 60) return sec + 's';
  const m = Math.floor(sec / 60), s = sec % 60;
  if (m < 60) return m + 'm' + String(s).padStart(2, '0') + 's';
  const h = Math.floor(m / 60);
  if (h < 24) return h + 'h' + String(m % 60).padStart(2, '0') + 'm';
  return Math.floor(h / 24) + 'd' + (h % 24) + 'h';
}
function fmtBytes(n) {
  if (!n) return '0 B';
  const units = ['B', 'KB', 'MB', 'GB', 'TB'];
  let i = 0;
  while (n >= 1024 && i < units.length - 1) { n /= 1024; i++; }
  return n.toFixed(i ? 1 : 0) + ' ' + units[i];
}
function fmtTime(ts) {
  if (!ts) return '–';
  const d = new Date(ts * 1000);
  const p = function (v) { return String(v).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate()) +
    ' ' + p(d.getHours()) + ':' + p(d.getMinutes()) + ':' + p(d.getSeconds());
}
function fmtClock(date) {
  const p = function (v) { return String(v).padStart(2, '0'); };
  return p(date.getHours()) + ':' + p(date.getMinutes()) + ':' + p(date.getSeconds());
}
function fmtAgo(ts) {
  if (!ts) return '未知';
  const d = Math.max(0, Date.now() / 1000 - ts);
  if (d < 8) return '刚刚';
  if (d < 90) return Math.round(d) + ' 秒前';
  if (d < 3600) return Math.round(d / 60) + ' 分钟前';
  if (d < 86400) return Math.round(d / 3600) + ' 小时前';
  return Math.round(d / 86400) + ' 天前';
}
function setText(el, text) {
  if (!el) return;
  text = text == null ? '' : String(text);
  if (el.textContent !== text) el.textContent = text;
}
function bump(el) {
  if (!el || REDUCED.matches) return;
  el.classList.remove('bump');
  void el.offsetWidth;
  el.classList.add('bump');
}
function flashRow(el) {
  if (!el || REDUCED.matches) return;
  el.classList.remove('row-flash');
  void el.offsetWidth;
  el.classList.add('row-flash');
}
function mkRow(cells) {
  const tpl = document.createElement('template');
  tpl.innerHTML = '<tr>' + cells + '</tr>';
  return tpl.content.firstElementChild;
}

/* --------------------------------------------------------------- 图标集合 */
/* 全部手写 24x24 线性图标，跟着 currentColor 变色 */
const ICON_PATHS = {
  clock: '<circle cx="12" cy="12" r="8.6"/><path d="M12 7.2V12l3.2 2.1"/>',
  queue: '<path d="M9.4 7h11M9.4 12h11M9.4 17h11"/>' +
    '<circle cx="4.6" cy="7" r="1.1" fill="currentColor" stroke="none"/>' +
    '<circle cx="4.6" cy="12" r="1.1" fill="currentColor" stroke="none"/>' +
    '<circle cx="4.6" cy="17" r="1.1" fill="currentColor" stroke="none"/>',
  play: '<circle cx="12" cy="12" r="8.6" opacity=".26"/>' +
    '<circle cx="12" cy="12" r="8.6" stroke-dasharray="17 37" class="ic-arc"/>',
  check: '<path d="M20.4 6.6 9.7 17.3l-5.1-5.1"/>',
  x: '<path d="M6.6 6.6l10.8 10.8M17.4 6.6 6.6 17.4"/>',
  ban: '<circle cx="12" cy="12" r="8.6"/><path d="M6.4 17.6 17.6 6.4"/>',
  hourglass: '<path d="M7 3.9h10M7 20.1h10M8.3 3.9v2c0 2.6 3.7 3.4 3.7 6.1s-3.7 3.5-3.7 6.1v2' +
    'M15.7 3.9v2c0 2.6-3.7 3.4-3.7 6.1s3.7 3.5 3.7 6.1v2"/>',
  alert: '<path d="M12 4.1 21 19.7H3L12 4.1Z"/><path d="M12 9.5v4.6"/>' +
    '<circle cx="12" cy="17" r=".95" fill="currentColor" stroke="none"/>',
  lock: '<rect x="4.8" y="10.4" width="14.4" height="9.4" rx="2.2"/>' +
    '<path d="M8.4 10.4V7.8a3.6 3.6 0 0 1 7.2 0v2.6"/>',
  cpu: '<rect x="7" y="7" width="10" height="10" rx="2.2"/>' +
    '<path d="M10 3.2v2.2M14 3.2v2.2M10 18.6v2.2M14 18.6v2.2M3.2 10h2.2M3.2 14h2.2M18.6 10h2.2M18.6 14h2.2"/>',
  mem: '<rect x="2.8" y="7.6" width="18.4" height="8.8" rx="2"/>' +
    '<path d="M7 7.6V5.2M12 7.6V5.2M17 7.6V5.2M7 16.4v2.4M12 16.4v2.4M17 16.4v2.4"/>',
  disk: '<rect x="3.4" y="4.6" width="17.2" height="14.8" rx="2.4"/>' +
    '<path d="M3.4 12.4h17.2"/><circle cx="8" cy="16" r="1.05" fill="currentColor" stroke="none"/>',
  server: '<rect x="3.4" y="4.2" width="17.2" height="6.2" rx="2"/>' +
    '<rect x="3.4" y="13.6" width="17.2" height="6.2" rx="2"/>' +
    '<circle cx="7.2" cy="7.3" r="1" fill="currentColor" stroke="none"/>' +
    '<circle cx="7.2" cy="16.7" r="1" fill="currentColor" stroke="none"/>',
  terminal: '<rect x="2.8" y="4.4" width="18.4" height="15.2" rx="2.6"/>' +
    '<path d="M6.8 10l2.6 2.2-2.6 2.2M12.6 14.6h4.6"/>',
  windows: '<rect x="3.6" y="5" width="7.4" height="6.4" rx="1.1"/>' +
    '<rect x="13" y="5" width="7.4" height="6.4" rx="1.1"/>' +
    '<rect x="3.6" y="12.6" width="7.4" height="6.4" rx="1.1"/>' +
    '<rect x="13" y="12.6" width="7.4" height="6.4" rx="1.1"/>',
  laptop: '<rect x="4" y="5" width="16" height="10.6" rx="1.8"/><path d="M2.6 18.6h18.8"/>',
  file: '<path d="M13.6 3.6H7.4a2 2 0 0 0-2 2v12.8a2 2 0 0 0 2 2h9.2a2 2 0 0 0 2-2V8.6Z"/>' +
    '<path d="M13.6 3.6v5h5"/>',
  bell: '<path d="M6.2 16.6V11a5.8 5.8 0 0 1 11.6 0v5.6l1.6 1.8H4.6Z"/>' +
    '<path d="M10 20.6a2.2 2.2 0 0 0 4 0"/>',
  download: '<path d="M12 4.4v10.2M8.2 11.2 12 15l3.8-3.8"/>' +
    '<path d="M4.6 17.4v1.2a1.8 1.8 0 0 0 1.8 1.8h11.2a1.8 1.8 0 0 0 1.8-1.8v-1.2"/>',
  bolt: '<path d="M13.2 2.8 5.6 13.4h5.1L9.9 21.2l7.6-10.8h-5.1Z"/>',
  refresh: '<path d="M20 12a8 8 0 1 1-2.4-5.7"/><path d="M20 4.4V9h-4.6"/>',
  ticket: '<path d="M3.6 8.6a2 2 0 0 1 2-2h12.8a2 2 0 0 1 2 2v1.2a2.2 2.2 0 0 0 0 4.4v1.2a2 2 0 0 1-2 2H5.6a2 2 0 0 1-2-2v-1.2a2.2 2.2 0 0 0 0-4.4Z"/>' +
    '<path d="M9.2 7v1.4M9.2 11v1.4M9.2 15v1.4" stroke-dasharray="1 2"/>',
  sparkle: '<path d="M12 3.6l2.2 5 5.4.6-4 3.6 1.1 5.3-4.7-2.7-4.7 2.7 1.1-5.3-4-3.6 5.4-.6Z"/>',
  key: '<circle cx="8.2" cy="15.8" r="3.4"/><path d="M10.7 13.3 19.6 4.4M16.2 7.8l2.6 2.6"/>',
  tri: '<path d="M9.2 7.4 16.6 12l-7.4 4.6Z" fill="currentColor" stroke="none"/>',
  dot: '<circle cx="12" cy="12" r="4" fill="currentColor" stroke="none"/>'
};

function icon(name, cls) {
  return '<svg class="icon' + (cls ? ' ' + cls : '') + '" viewBox="0 0 24 24" aria-hidden="true"' +
    ' fill="none" stroke="currentColor" stroke-width="1.75" stroke-linecap="round"' +
    ' stroke-linejoin="round">' + (ICON_PATHS[name] || '') + '</svg>';
}
function osIcon(os) {
  const s = String(os || '').toLowerCase();
  if (s.indexOf('win') >= 0) return 'windows';
  if (s.indexOf('darwin') >= 0 || s.indexOf('mac') >= 0) return 'laptop';
  if (s.indexOf('linux') >= 0) return 'terminal';
  return 'server';
}

/* ----------------------------------------------------------- 状态映射/徽章 */
const STATUS = {
  queued: { label: '排队中', icon: 'clock', cls: 'st-queued' },
  running: { label: '运行中', icon: 'play', cls: 'st-running' },
  succeeded: { label: '成功', icon: 'check', cls: 'st-ok' },
  failed: { label: '失败', icon: 'x', cls: 'st-bad' },
  cancelled: { label: '已取消', icon: 'ban', cls: 'st-warn' },
  timeout: { label: '超时', icon: 'hourglass', cls: 'st-warn' },
  error: { label: '错误', icon: 'alert', cls: 'st-bad' }
};
function statusMeta(status) {
  return STATUS[status] || { label: status || '未知', icon: 'dot', cls: '' };
}
function statusBadge(status, exitCode) {
  const meta = statusMeta(status);
  const exit = (exitCode == null || exitCode === '') ? '' : ' (' + esc(exitCode) + ')';
  return '<span class="badge ' + meta.cls + '">' + icon(meta.icon) + esc(meta.label) + exit + '</span>';
}
function isFailure(status) {
  return status === 'failed' || status === 'timeout' || status === 'error';
}

/* ------------------------------------------------------------- 空态插画 */
const ART = {
  hosts: '<svg class="empty-art" viewBox="0 0 140 92" aria-hidden="true">' +
    '<ellipse cx="70" cy="82" rx="46" ry="6" fill="currentColor" opacity=".08"/>' +
    '<g class="art-accent">' +
    '<rect x="33" y="18" width="62" height="14" rx="6" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<rect x="33" y="37" width="62" height="14" rx="6" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".72"/>' +
    '<rect x="33" y="56" width="62" height="14" rx="6" fill="none" stroke="currentColor" stroke-width="1.6" opacity=".48"/>' +
    '<circle cx="42" cy="25" r="2" fill="currentColor" stroke="none"/>' +
    '<circle cx="42" cy="44" r="2" fill="currentColor" stroke="none"/>' +
    '<circle cx="42" cy="63" r="2" fill="currentColor" stroke="none"/>' +
    '<path d="M51 25h30M51 44h24M51 63h18" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" opacity=".45"/>' +
    '</g>' +
    '<g opacity=".5">' +
    '<circle cx="112" cy="25" r="12" fill="none" stroke="currentColor" stroke-width="1.4" stroke-dasharray="2.6 3"/>' +
    '<path d="M112 20v10M107 25h10" stroke="currentColor" stroke-width="1.4" stroke-linecap="round"/>' +
    '</g></svg>',
  queue: '<svg class="empty-art" viewBox="0 0 140 92" aria-hidden="true">' +
    '<ellipse cx="70" cy="82" rx="40" ry="6" fill="currentColor" opacity=".08"/>' +
    '<g class="art-accent">' +
    '<rect x="34" y="20" width="72" height="46" rx="10" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<path d="M53 20v46" stroke="currentColor" stroke-width="1.3" stroke-dasharray="2.4 3.2" opacity=".8"/>' +
    '<path d="M62 33h31M62 41h31M62 49h19" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" opacity=".4"/>' +
    '</g>' +
    '<g opacity=".55">' +
    '<rect x="90" y="10" width="30" height="17" rx="6" fill="none" stroke="currentColor" stroke-width="1.4"/>' +
    '<path d="M97 18.5h11" stroke="currentColor" stroke-width="1.8" stroke-linecap="round"/>' +
    '<path d="M20 46h6M17 55h9" stroke="currentColor" stroke-width="1.6" stroke-linecap="round" stroke-dasharray="2 3"/>' +
    '</g></svg>',
  jobs: '<svg class="empty-art" viewBox="0 0 140 92" aria-hidden="true">' +
    '<ellipse cx="70" cy="82" rx="40" ry="6" fill="currentColor" opacity=".08"/>' +
    '<g class="art-accent">' +
    '<rect x="42" y="16" width="56" height="58" rx="10" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<rect x="56" y="10" width="28" height="12" rx="6" fill="none" stroke="currentColor" stroke-width="1.6"/>' +
    '<path d="M53 34h26M53 44h26M53 54h16" stroke="currentColor" stroke-width="2.2" stroke-linecap="round" opacity=".4"/>' +
    '</g>' +
    '<g opacity=".45">' +
    '<circle cx="112" cy="26" r="2.4" fill="currentColor" stroke="none"/>' +
    '<circle cx="120" cy="40" r="1.5" fill="currentColor" stroke="none"/>' +
    '<circle cx="26" cy="34" r="2" fill="currentColor" stroke="none"/>' +
    '<circle cx="22" cy="52" r="1.4" fill="currentColor" stroke="none"/>' +
    '</g></svg>',
  logs: '<svg class="empty-art" viewBox="0 0 140 92" aria-hidden="true">' +
    '<ellipse cx="70" cy="82" rx="40" ry="6" fill="currentColor" opacity=".08"/>' +
    '<g class="art-accent">' +
    '<rect x="26" y="20" width="88" height="52" rx="9" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<path d="M26 33h88" stroke="currentColor" stroke-width="1.3" opacity=".5"/>' +
    '<circle cx="33" cy="26.5" r="1.8" fill="currentColor" stroke="none" opacity=".7"/>' +
    '<circle cx="39" cy="26.5" r="1.8" fill="currentColor" stroke="none" opacity=".45"/>' +
    '<circle cx="45" cy="26.5" r="1.8" fill="currentColor" stroke="none" opacity=".3"/>' +
    '<path d="M34 43l6 5-6 5" stroke="currentColor" stroke-width="1.9" stroke-linecap="round" stroke-linejoin="round"/>' +
    '<path d="M46 53h16" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".5"/>' +
    '<path d="M34 65h44" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".22"/>' +
    '</g>' +
    '<circle cx="120" cy="70" r="3" fill="currentColor" opacity=".3"/></svg>',
  artifacts: '<svg class="empty-art" viewBox="0 0 140 92" aria-hidden="true">' +
    '<ellipse cx="70" cy="82" rx="40" ry="6" fill="currentColor" opacity=".08"/>' +
    '<g class="art-accent">' +
    '<path d="M26 32a6 6 0 0 1 6-6h20l7 8h49a6 6 0 0 1 6 6v30a6 6 0 0 1-6 6H32a6 6 0 0 1-6-6Z" fill="none" stroke="currentColor" stroke-width="1.7"/>' +
    '<path d="M26 42h88" stroke="currentColor" stroke-width="1.3" opacity=".4"/>' +
    '<path d="M46 60h30" stroke="currentColor" stroke-width="2" stroke-linecap="round" opacity=".25"/>' +
    '</g>' +
    '<g opacity=".45">' +
    '<circle cx="116" cy="22" r="2.4" fill="currentColor" stroke="none"/>' +
    '<circle cx="106" cy="14" r="1.4" fill="currentColor" stroke="none"/>' +
    '<circle cx="124" cy="34" r="1.3" fill="currentColor" stroke="none"/>' +
    '</g></svg>'
};

function emptyState(key, title, hint) {
  return '<div class="empty">' + (ART[key] || ART.jobs) +
    '<div><strong>' + esc(title) + '</strong></div>' +
    (hint ? '<p>' + esc(hint) + '</p>' : '') + '</div>';
}

/* ----------------------------------------------------------------- API */
function saveToken() {
  localStorage.setItem('calltable_token', document.getElementById('tokeninput').value.trim());
  location.reload();
}
function showTokenBox() {
  const box = document.getElementById('tokenbox');
  box.hidden = false;
  const input = document.getElementById('tokeninput');
  if (input) { input.focus(); input.select(); }
}
function tok() {
  return TOKEN || localStorage.getItem('calltable_token') || '';
}
function api(path, opts) {
  opts = opts || {};
  opts.headers = Object.assign({ 'Authorization': 'Bearer ' + tok() }, opts.headers || {});
  return fetch(path, opts).then(function (r) {
    if (r.status === 401) { showTokenBox(); throw new Error('unauthorized'); }
    if (!r.ok) throw new Error(r.status + ' ' + r.statusText);
    return r;
  });
}
function spin(on) {
  const el = document.getElementById('spin');
  if (!el) return;
  if (on) { clearTimeout(el._t); el.classList.add('active'); return; }
  el._t = setTimeout(function () { el.classList.remove('active'); }, 340);
}
function setConn(ok, label) {
  const live = document.getElementById('live');
  if (!live) return;
  live.classList.toggle('offline', !ok);
  const txt = live.querySelector('.state-txt');
  setText(txt, label || (ok ? 'hub 已连接' : '连接中断'));
}
"""

_JS_OVERVIEW = r"""
/* --------------------------------------------------------------- 概览统计 */
function dayStamp(ts) {
  const d = new Date(ts * 1000);
  return d.getFullYear() + '/' + d.getMonth() + '/' + d.getDate();
}
function countToday(jobs, statuses) {
  const today = dayStamp(Date.now() / 1000);
  let n = 0;
  (jobs || []).forEach(function (j) {
    if (statuses.indexOf(j.status) < 0) return;
    const ts = j.finished || j.created;
    if (ts && dayStamp(ts) === today) n++;
  });
  return n;
}

function patchStats(state) {
  const hosts = state.hosts || [];
  const online = hosts.filter(function (h) { return h.online; }).length;
  const running = (state.running || []).length;
  const queued = (state.queued || []).length;
  const ok = countToday(state.jobs, ['succeeded']);
  const bad = countToday(state.jobs, ['failed', 'timeout', 'error']);
  const items = [
    { label: '运行中', value: running, icon: 'play', tone: running ? 'run' : '',
      foot: running ? '正在占用槽位' : '槽位空闲' },
    { label: '排队中', value: queued, icon: 'queue', tone: queued ? 'warn' : '',
      foot: queued ? '等待叫号' : '队列为空' },
    { label: '在线主机', value: online + ' / ' + hosts.length, icon: 'server',
      tone: !hosts.length ? '' : (online === hosts.length ? 'ok' : 'warn'),
      foot: hosts.length ? hosts.length + ' 台已注册' : '尚未注册主机' },
    { label: '今日成功', value: ok, icon: 'check', tone: ok ? 'ok' : '',
      foot: '近 100 条任务' },
    { label: '今日异常', value: bad, icon: 'alert', tone: bad ? 'bad' : '',
      foot: '失败 / 超时 / 错误' }
  ];
  const box = document.getElementById('stats');
  while (box.children.length > items.length) box.removeChild(box.lastElementChild);
  items.forEach(function (item, i) {
    let el = box.children[i];
    if (!el) {
      el = document.createElement('div');
      el.className = 'stat';
      el.style.animationDelay = Math.min(i, 8) * 55 + 'ms';
      el.innerHTML = '<div class="stat-ico"></div>' +
        '<div class="stat-label"></div>' +
        '<div class="stat-value"><span class="v"></span><span class="u"></span></div>' +
        '<div class="stat-foot"></div>';
      box.appendChild(el);
    }
    el.dataset.tone = item.tone;
    if (el.dataset.icon !== item.icon) {
      el.querySelector('.stat-ico').innerHTML = icon(item.icon);
      el.dataset.icon = item.icon;
    }
    setText(el.querySelector('.stat-label'), item.label);
    setText(el.querySelector('.stat-foot'), item.foot);
    const v = el.querySelector('.stat-value .v');
    const next = String(item.value);
    if (v.textContent !== next) { setText(v, next); bump(el); }
  });
}

/* ----------------------------------------------------------------- 主机卡 */
function buildHostCard() {
  const el = document.createElement('article');
  el.className = 'card host card-in';
  el.innerHTML =
    '<div class="host-top">' +
      '<div class="host-ico"></div>' +
      '<div class="host-id">' +
        '<div class="host-title"><span class="hname"></span>' +
          '<span class="host-state"><i class="pulse-dot"></i><span class="state-txt"></span></span>' +
        '</div>' +
        '<div class="host-meta"></div>' +
      '</div>' +
    '</div>' +
    '<div class="host-alert" hidden></div>' +
    '<div class="slot-list"></div>' +
    '<div class="lock-row"></div>' +
    '<div class="host-foot"></div>' +
    '<div class="host-running"></div>';
  return el;
}

function patchSlots(box, host) {
  const entries = Object.keys(host.slots || {});
  if (!entries.length) {
    if (!box.querySelector('.sub')) {
      box.innerHTML = '<div class="sub">未配置 slot</div>';
    }
    return;
  }
  if (box.querySelector('.sub')) box.innerHTML = '';
  const existing = new Map();
  Array.from(box.children).forEach(function (el) {
    if (el.dataset.slot) existing.set(el.dataset.slot, el);
  });
  entries.forEach(function (name) {
    const limit = host.slots[name];
    const used = (host.used || {})[name] || 0;
    let row = existing.get(name);
    if (!row) {
      row = document.createElement('div');
      row.className = 'slot';
      row.dataset.slot = name;
      row.innerHTML = '<div class="slot-head"><span class="slot-name"></span>' +
        '<span class="slot-num"></span></div><div class="bar"><i></i></div>';
      box.appendChild(row);
    }
    existing.delete(name);
    setText(row.querySelector('.slot-name'), name);
    setText(row.querySelector('.slot-num'), used + ' / ' + limit);
    const pct = limit ? Math.min(100, Math.round(used / limit * 100)) : 0;
    const bar = row.querySelector('.bar');
    bar.classList.toggle('full', limit > 0 && used >= limit);
    bar.classList.toggle('idle', used === 0);
    const fill = bar.firstElementChild;
    const width = pct + '%';
    if (fill.style.width !== width) fill.style.width = width;
  });
  existing.forEach(function (el) { el.remove(); });
}

function patchLocks(box, locks) {
  const sig = JSON.stringify(locks || []);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = (locks || []).map(function (l) {
    return '<span class="chip chip-accent">' + icon('lock') + esc(l) + '</span>';
  }).join('');
}

function patchFoot(box, host) {
  const sys = host.system || {};
  const chips = [];
  if (host.queue) chips.push(['排队 ' + host.queue, 'chip-accent']);
  if (sys.disk_free_gb != null) {
    chips.push(['盘余 ' + sys.disk_free_gb + 'G', sys.disk_free_gb < 5 ? 'chip-warn' : '']);
  }
  if (sys.disk_runs_gb != null) {
    chips.push(['runs ' + (sys.disk_runs_partial ? '≥' : '') + sys.disk_runs_gb + 'G', '']);
  }
  if (sys.mem_free_gb) {
    chips.push(['内存 ' + sys.mem_free_gb + '/' + (sys.mem_total_gb || '?') + 'G', '']);
  }
  if (sys.load1 != null && sys.load1 !== '') chips.push(['load ' + sys.load1, '']);
  if (host.draining) chips.push(['draining', 'chip-warn']);
  (host.labels || []).slice(0, 3).forEach(function (l) { chips.push([l, '']); });
  const sig = JSON.stringify(chips);
  if (box.dataset.sig === sig) return;
  box.dataset.sig = sig;
  box.innerHTML = chips.map(function (pair) {
    return '<span class="chip ' + pair[1] + '">' + esc(pair[0]) + '</span>';
  }).join('');
}

function patchRunning(box, jobs) {
  if (!jobs.length) {
    if (box.children.length) box.innerHTML = '';
    return;
  }
  const existing = new Map();
  Array.from(box.children).forEach(function (el) { existing.set(el.dataset.job, el); });
  jobs.forEach(function (job) {
    let a = existing.get(job.id);
    if (!a) {
      a = document.createElement('a');
      a.className = 'run-item';
      a.href = '#/job/' + encodeURIComponent(job.id);
      a.dataset.job = job.id;
      a.innerHTML = '<span class="run-dot"></span>' +
        '<span class="run-main"><code class="run-id"></code>' +
        '<span class="run-sub"><span class="run-proj"></span>' +
        '<span class="run-label"></span></span></span>';
      box.appendChild(a);
    }
    existing.delete(job.id);
    setText(a.querySelector('.run-id'), job.id);
    setText(a.querySelector('.run-proj'), job.project || '');
    setText(a.querySelector('.run-label'), job.label || '');
    a.title = job.id + (job.label ? ' · ' + job.label : '');
  });
  existing.forEach(function (el) { el.remove(); });
}

function patchHost(card, host, group) {
  const online = !!host.online;
  card.dataset.host = host.name;
  card.classList.toggle('offline', !online);
  setText(card.querySelector('.hname'), host.name);
  const state = card.querySelector('.host-state');
  state.className = 'host-state ' + (online ? 'on' : 'off');
  const stateTxt = online ? '在线' : '离线';
  setText(state.querySelector('.state-txt'), stateTxt);

  const ico = card.querySelector('.host-ico');
  const icoName = osIcon(host.os);
  if (ico.dataset.icon !== icoName) {
    ico.innerHTML = icon(icoName);
    ico.dataset.icon = icoName;
    ico.title = host.os || 'unknown';
  }

  const bits = [];
  if (group) bits.push('group ' + group);
  if (host.os) bits.push(host.os);
  if (host.version) bits.push('v' + host.version);
  bits.push('心跳 ' + fmtAgo(host.last_seen));
  if (host.note) bits.push(host.note);
  setText(card.querySelector('.host-meta'), bits.join(' · '));
  card.querySelector('.host-meta').title = bits.join(' · ');

  const alert = card.querySelector('.host-alert');
  if (host.disk_low) {
    alert.hidden = false;
    const reason = (host.system && host.system.disk_reason) || '';
    const text = '磁盘紧张' + (reason ? '：' + reason : '') + ' · 暂停领取';
    if (alert.dataset.text !== text) {
      alert.innerHTML = icon('alert') + '<span></span>';
      alert.querySelector('span').textContent = text;
      alert.dataset.text = text;
    }
  } else if (!alert.hidden) {
    alert.hidden = true;
    alert.dataset.text = '';
  }

  patchSlots(card.querySelector('.slot-list'), host);
  patchLocks(card.querySelector('.lock-row'), host.locks || []);
  patchFoot(card.querySelector('.host-foot'), host);
  patchRunning(card.querySelector('.host-running'), host.running || []);
}

function patchHosts(hosts, groups) {
  const box = document.getElementById('hosts');
  const membership = {};
  Object.keys(groups || {}).forEach(function (name) {
    (groups[name] || []).forEach(function (m) { membership[m] = name; });
  });
  const online = hosts.filter(function (h) { return h.online; }).length;
  setText(document.getElementById('host-hint'),
    hosts.length ? hosts.length + ' 台已注册 · ' + online + ' 台在线' : '心跳 / 槽位 / 锁');
  if (!hosts.length) {
    box.classList.add('is-empty');
    box.innerHTML = emptyState('hosts', '还没有主机上报心跳',
      '在目标机器上启动 executor，它会自动注册到这里。');
    return;
  }
  box.classList.remove('is-empty');
  if (box.querySelector('.empty')) box.innerHTML = '';
  const existing = new Map();
  Array.from(box.children).forEach(function (el) {
    if (el.dataset.host) existing.set(el.dataset.host, el);
  });
  hosts.forEach(function (host, i) {
    let card = existing.get(host.name);
    if (!card) {
      card = buildHostCard();
      if (!REDUCED.matches) card.style.animationDelay = Math.min(i, 8) * 45 + 'ms';
      box.appendChild(card);
    } else {
      existing.delete(host.name);
    }
    patchHost(card, host, membership[host.name]);
  });
  existing.forEach(function (el) { el.remove(); });
}

/* ----------------------------------------------------------------- 表格 */
/* [标题, 列 class]：窄屏时靠 class 隐藏次要列，见 CSS 里的 .c-* */
const QUEUE_HEADS = [
  ['id', 'c-id'], ['项目', 'c-proj'], ['主机', 'c-host'], ['slot', 'c-slot'],
  ['锁', 'c-lock'], ['已等', 'c-wait'], ['前面', 'c-ahead'], ['预计等待', 'c-est'],
  ['预计耗时', 'c-est'], ['提交人', 'c-sub'],
  ['label', 'c-label'], ['创建', 'c-start']
];
const HISTORY_HEADS = [
  ['id', 'c-id'], ['项目', 'c-proj'], ['主机', 'c-host'], ['提交人', 'c-sub'],
  ['label', 'c-label'], ['SHA', 'c-sha'], ['dirty', 'c-dirty'], ['开始', 'c-start'],
  ['耗时', 'c-dur'], ['状态', 'c-status'], ['CPU 总秒 / 峰值核数', 'c-cpu'],
  ['内存峰值', 'c-mem']
];

function mountTable(box, heads) {
  let table = box.querySelector('table.tbl');
  if (table) return table.querySelector('tbody');
  box.innerHTML = '';
  const wrap = document.createElement('div');
  wrap.className = 'table-wrap';
  wrap.innerHTML = '<table class="tbl"><thead><tr>' +
    heads.map(function (h) { return '<th class="' + h[1] + '">' + esc(h[0]) + '</th>'; }).join('') +
    '</tr></thead><tbody></tbody></table>';
  box.appendChild(wrap);
  return wrap.querySelector('tbody');
}

function patchRows(tbody, rows, cellsFn) {
  const first = tbody.dataset.booted !== '1';
  const existing = new Map();
  Array.from(tbody.children).forEach(function (tr) { existing.set(tr.dataset.key, tr); });
  let cursor = tbody.firstElementChild;
  rows.forEach(function (row, index) {
    const cells = cellsFn(row.data);
    let tr = existing.get(row.key);
    if (tr) {
      if (tr !== cursor) tbody.insertBefore(tr, cursor);
      cursor = tr.nextElementSibling;
      existing.delete(row.key);
      if (tr.dataset.sig !== row.sig) {
        const prev = tr.dataset.status;
        tr.replaceChildren.apply(tr, Array.from(mkRow(cells).childNodes));
        tr.dataset.sig = row.sig;
        tr.dataset.status = row.data.status || '';
        if (prev && prev !== row.data.status) flashRow(tr);
      }
      return;
    }
    tr = mkRow(cells);
    tr.className = 'row clickable';
    tr.dataset.key = row.key;
    tr.dataset.sig = row.sig;
    tr.dataset.status = row.data.status || '';
    if (first) {
      tr.classList.add('row-in');
      if (!REDUCED.matches) tr.style.animationDelay = Math.min(index, 10) * 26 + 'ms';
    }
    if (cursor) tbody.insertBefore(tr, cursor); else tbody.appendChild(tr);
    cursor = tr.nextElementSibling;
  });
  existing.forEach(function (tr) { tr.remove(); });
  tbody.dataset.booted = '1';
}

function lockChips(job) {
  const locks = job.locks_display || job.locks || [];
  if (!locks.length) return '<span class="sub">–</span>';
  return locks.map(function (l) {
    return '<span class="chip">' + icon('lock') + esc(l) + '</span>';
  }).join(' ');
}

/* 预计时间缺历史数据时 hub 给 null，显示「未知」而不是 0 */
function fmtEst(sec) {
  return sec == null ? '未知' : fmtDur(sec);
}

function queueCells(job) {
  return '<td class="c-id">' + idCell(job.id) + '</td>' +
    '<td class="c-proj td-strong">' + esc(job.project) + '</td>' +
    '<td class="c-host">' + esc(job.host) + '</td>' +
    '<td class="c-slot"><span class="chip">' + esc(job.slot) + '</span></td>' +
    '<td class="c-lock">' + lockChips(job) + '</td>' +
    '<td class="c-wait num">' + fmtDur(job.queued_seconds) + '</td>' +
    '<td class="c-ahead num">' + (job.ahead == null ? '–' : job.ahead) + '</td>' +
    '<td class="c-est num dim">' + fmtEst((job.queue || {}).wait_seconds) + '</td>' +
    '<td class="c-est num dim">' + fmtEst((job.queue || {}).runtime_seconds) + '</td>' +
    '<td class="c-sub">' + esc(job.submitter) + '</td>' +
    '<td class="c-label wrap dim">' + esc(job.label) + '</td>' +
    '<td class="c-start dim num">' + fmtTime(job.created) + '</td>';
}

/* 窄屏用短 id（去掉 job- 前缀），把位置留给状态与 label */
function idCell(id) {
  const short = String(id).replace(/^job-/, '');
  const cut = short.lastIndexOf('-');
  const shortHtml = cut > 0
    ? '<span class="nw">' + esc(short.slice(0, cut + 1)) + '</span><span class="nw">' +
      esc(short.slice(cut + 1)) + '</span>'
    : esc(short);
  return '<a class="joblink" href="#/job/' + encodeURIComponent(id) + '" title="' + esc(id) + '">' +
    '<code><span class="id-full">' + esc(id) + '</span>' +
    '<span class="id-short">' + shortHtml + '</span></code></a>';
}

function historyCells(job) {
  const dur = job.started ? ((job.finished || Date.now() / 1000) - job.started) : null;
  const sha = job.sha ? job.sha.slice(0, 9) : '–';
  const badge = statusBadge(job.status, job.exit_code);
  return '<td class="c-id">' + idCell(job.id) +
      /* 窄屏看不到「状态」列，就把徒章放到 id 下面 */
      '<span class="mob-status">' + badge + '</span></td>' +
    '<td class="c-proj td-strong">' + esc(job.project) + '</td>' +
    '<td class="c-host">' + esc(job.host) + '</td>' +
    '<td class="c-sub">' + esc(job.submitter) +
      (job.agent ? ' <span class="chip">' + esc(job.agent) + '</span>' : '') + '</td>' +
    '<td class="c-label wrap dim">' + esc(job.label) + '</td>' +
    '<td class="c-sha mono dim">' + esc(sha) + '</td>' +
    '<td class="c-dirty">' +
      (job.dirty ? '<span class="chip chip-warn">' + icon('alert') + 'dirty</span>' : '') + '</td>' +
    '<td class="c-start dim num">' + fmtTime(job.started || job.created) + '</td>' +
    '<td class="c-dur num">' + fmtDur(dur) + '</td>' +
    '<td class="c-status">' + badge + '</td>' +
    '<td class="c-cpu num dim">' + (job.cpu_total_sec || 0).toFixed(1) + 's / ' +
      (job.cpu_peak_cores || 0).toFixed(2) + 'c</td>' +
    '<td class="c-mem num dim">' + fmtBytes(job.mem_peak) + '</td>';
}

function sigOf(job, keys) {
  return keys.map(function (k) {
    const v = job[k];
    return Array.isArray(v) ? v.join(',') : String(v == null ? '' : v);
  }).join('|');
}

const QUEUE_SIG = ['id', 'project', 'host', 'slot', 'locks_display', 'locks',
  'queued_seconds', 'ahead', 'submitter', 'label', 'created', 'status'];

function queueSig(job) {
  const q = job.queue || {};
  return sigOf(job, QUEUE_SIG) + '|' + q.wait_seconds + '|' + q.runtime_seconds;
}
const HISTORY_SIG = ['id', 'project', 'host', 'submitter', 'agent', 'label', 'sha',
  'dirty', 'started', 'created', 'finished', 'status', 'exit_code',
  'cpu_total_sec', 'cpu_peak_cores', 'mem_peak'];

function renderQueue(jobs) {
  const box = document.getElementById('queue');
  if (!jobs.length) {
    if (!box.querySelector('.empty')) {
      box.innerHTML = emptyState('queue', '队列为空', '取号完成，所有任务都已被叫号。');
    }
    return;
  }
  const tbody = mountTable(box, QUEUE_HEADS);
  patchRows(tbody, jobs.map(function (job) {
    return { key: job.id, data: job, sig: queueSig(job) };
  }), queueCells);
}

function renderHistory(jobs) {
  const box = document.getElementById('history');
  if (!jobs.length) {
    if (!box.querySelector('.empty')) {
      box.innerHTML = emptyState('jobs', '还没有任务', '用 CLI 提交第一个任务：calltable run -- <命令>');
    }
    return;
  }
  const tbody = mountTable(box, HISTORY_HEADS);
  patchRows(tbody, jobs.map(function (job) {
    return { key: job.id, data: job, sig: sigOf(job, HISTORY_SIG) };
  }), historyCells);
}

/* --------------------------------------------------------------- 总刷新 */
function renderOffline() {
  const specs = [
    ['hosts', 'hosts', '无法读取主机状态', '检查 hub 是否在运行、token 是否正确。'],
    ['queue', 'queue', '无法读取队列', '令牌无效或 hub 不可达。'],
    ['history', 'jobs', '无法读取任务', '令牌无效或 hub 不可达。']
  ];
  specs.forEach(function (spec) {
    const box = document.getElementById(spec[0]);
    if (!box || box.children.length) return;
    if (spec[0] === 'hosts') box.classList.add('is-empty');
    box.innerHTML = emptyState(spec[1], spec[2], spec[3]);
  });
}

function refreshOverview() {
  if (busy) return;
  busy = true;
  spin(true);
  api('/api/state').then(function (r) { return r.json(); }).then(function (state) {
    setConn(true);
    hadData = true;
    const hosts = state.hosts || [];
    setText(document.getElementById('summary'),
      hosts.length + ' 台主机 · ' + (state.queued || []).length + ' 排队 · ' +
      (state.running || []).length + ' 运行中' +
      (state.version ? ' · hub v' + state.version : ''));
    setText(document.getElementById('updated'), '更新于 ' + fmtClock(new Date()));
    patchStats(state);
    patchHosts(hosts, state.groups || {});
    renderQueue(state.queued || []);
    renderHistory(state.jobs || []);
  }).catch(function (err) {
    const message = String(err && err.message);
    if (message === 'unauthorized') {
      setConn(false, '需要 token');
      setText(document.getElementById('summary'), '需要 hub token 才能读取状态');
      renderOffline();
    } else {
      setConn(false);
      if (!hadData) {
        setText(document.getElementById('summary'), '无法连接 hub');
        renderOffline();
      }
    }
  }).then(function () {
    busy = false;
    spin(false);
  });
}
"""

_JS_DETAIL = r"""
/* ------------------------------------------------------------ 详情：头部 */
let busyDetail = false;

function metaItem(label, value, cls) {
  return '<div class="meta-item ' + (cls || '') + '"><span class="meta-k">' + esc(label) +
    '</span><span class="meta-v">' + value + '</span></div>';
}

function renderSteps(job) {
  const box = document.getElementById('d-steps');
  const labels = ['提交', '排队', '运行', '完成'];
  let done = 0, active = -1, bad = -1;
  if (job.status === 'queued') { done = 1; active = 1; }
  else if (job.status === 'running') { done = 2; active = 2; }
  else if (job.status === 'succeeded') { done = 4; }
  else if (isFailure(job.status) || job.status === 'cancelled') {
    if (job.started) { done = 3; bad = 3; } else { done = 1; bad = 1; }
  } else { done = 1; active = 1; }
  box.innerHTML = labels.map(function (label, i) {
    let cls = 'step';
    if (i === bad) cls += ' bad';
    else if (i < done) cls += ' done';
    if (i === active) cls += ' active';
    let glyph = 'dot';
    if (i === bad) glyph = isFailure(job.status) ? 'x' : 'ban';
    else if (i < done) glyph = 'check';
    else if (i === active) glyph = label === '排队' ? 'clock' : 'tri';
    return '<div class="' + cls + '">' +
      '<span class="step-dot">' + icon(glyph) + '</span>' +
      '<span class="step-label">' + esc(label) + '</span>' +
      (i < labels.length - 1 ? '<span class="step-line"></span>' : '') +
      '</div>';
  }).join('');
}

function heroArt(status) {
  const meta = statusMeta(status);
  const glyphName = status === 'running' ? 'tri' : meta.icon;
  return '<svg class="hero-svg" viewBox="0 0 214 128" aria-hidden="true">' +
    '<defs><linearGradient id="heroGrad" x1="0" y1="0" x2="1" y2="1">' +
    '<stop offset="0" stop-color="currentColor" stop-opacity=".9"/>' +
    '<stop offset="1" stop-color="currentColor" stop-opacity=".3"/></linearGradient></defs>' +
    '<g class="hero-float">' +
    '<rect x="22" y="18" width="114" height="80" rx="14" fill="none" stroke="url(#heroGrad)" stroke-width="2"/>' +
    '<path d="M56 18v80" stroke="url(#heroGrad)" stroke-width="1.4" stroke-dasharray="3 4" opacity=".75"/>' +
    '<path d="M30 34h16M30 46h10" stroke="currentColor" stroke-width="2.6" stroke-linecap="round" opacity=".45"/>' +
    '<path d="M66 40h56M66 52h56M66 64h34" stroke="currentColor" stroke-width="3" stroke-linecap="round" opacity=".26"/>' +
    '</g>' +
    '<circle cx="146" cy="66" r="23" fill="currentColor" opacity=".07"/>' +
    '<circle cx="146" cy="66" r="23" fill="none" stroke="currentColor" stroke-width="1.8" opacity=".8"/>' +
    '<g transform="translate(134 54)" fill="none" stroke="currentColor" stroke-width="1.9"' +
    ' stroke-linecap="round" stroke-linejoin="round">' + (ICON_PATHS[glyphName] || '') + '</g>' +
    '<circle cx="192" cy="38" r="3" fill="currentColor" opacity=".32"/>' +
    '<circle cx="184" cy="98" r="2.2" fill="currentColor" opacity=".22"/>' +
    '<circle cx="166" cy="24" r="1.6" fill="currentColor" opacity=".26"/>' +
    '</svg>';
}

function renderDetail(job) {
  setText(document.getElementById('d-id'), job.id);
  const dur = job.started ? ((job.finished || Date.now() / 1000) - job.started) : null;
  setText(document.getElementById('d-sub'),
    [job.project, job.host + ' / ' + job.slot, job.submitter]
      .filter(function (x) { return x; }).join(' · '));
  document.getElementById('d-status').innerHTML =
    statusBadge(job.status, job.exit_code) +
    (job.cancel_requested ? '<span class="chip chip-warn">' + icon('ban') + '取消中</span>' : '');
  setText(document.getElementById('d-exit'),
    job.exit_code == null ? '' : 'exit code ' + job.exit_code);
  setText(document.getElementById('d-term-title'), job.id + ' · tail -f');
  const art = document.getElementById('d-hero-art');
  art.dataset.status = job.status || '';
  art.innerHTML = heroArt(job.status);
  renderSteps(job);

  const rows = [];
  rows.push(metaItem('项目', esc(job.project)));
  rows.push(metaItem('主机 / slot', esc(job.host) + ' / <span class="mono">' + esc(job.slot) + '</span>'));
  rows.push(metaItem('提交人', esc(job.submitter) +
    (job.agent ? ' <span class="chip">' + esc(job.agent) + '</span>' : '')));
  rows.push(metaItem('label', esc(job.label || '–')));
  rows.push(metaItem('SHA', '<span class="mono">' + esc(job.sha || '–') + '</span>' +
    (job.dirty ? ' <span class="chip chip-warn">dirty</span>' : ''), 'wide'));
  let baseText = '<span class="dim">–</span>';
  if (job.base_ref || job.base_ref_sha) {
    baseText = '<span class="mono">' + esc(job.base_ref || '(no ref)') + '</span> = ' +
      '<span class="mono">' + esc((job.base_ref_sha || '–').slice(0, 12)) + '</span>' +
      (job.base_sha && job.dirty ? ' <span class="chip">baseline ' +
        esc((job.base_sha || '–').slice(0, 12)) + '</span>' : '');
  }
  rows.push(metaItem('base', baseText, 'wide'));
  rows.push(metaItem('锁', esc(((job.locks_display || job.locks || []).join(', ')) || '–')));
  if (job.status === 'queued') {
    rows.push(metaItem('排队中', esc(fmtDur(job.queued_seconds)) + ' · 前面 ' +
      (job.ahead == null ? '–' : job.ahead) + ' 个'));
    rows.push(metaItem('预计等待', esc(fmtEst((job.queue || {}).wait_seconds))));
    rows.push(metaItem('预计耗时', esc(fmtEst((job.queue || {}).runtime_seconds))));
  }
  if (job.hold_seconds) {
    rows.push(metaItem('号码绑定', esc(fmtDur(job.hold_seconds)) +
      ' <span class="dim">无心跳就自动取消</span>', 'wide'));
  }
  rows.push(metaItem('创建', fmtTime(job.created)));
  rows.push(metaItem('开始', fmtTime(job.started)));
  rows.push(metaItem('耗时', esc(fmtDur(dur))));
  rows.push(metaItem('超时', esc(fmtDur(job.timeout))));
  rows.push(metaItem('CPU 总秒', (job.cpu_total_sec || 0).toFixed(3) + 's'));
  rows.push(metaItem('峰值核数', (job.cpu_peak_cores || 0).toFixed(2) + 'c'));
  rows.push(metaItem('内存峰值', esc(fmtBytes(job.mem_peak))));
  rows.push(metaItem('命令', '<span class="mono">' +
    esc((job.cmd || []).join(' ') || '–') + '</span>', 'wide cmd'));
  if (job.error) rows.push(metaItem('错误', esc(job.error), 'wide'));
  document.getElementById('d-meta').innerHTML = rows.join('');
}

/* ------------------------------------------------------------ 详情：图表 */
let chartSamples = [];

function drawChart(samples) {
  chartSamples = (samples || []).filter(function (s) { return s && s.ts != null; });
  renderChart();
}

function renderChart() {
  const svg = document.getElementById('d-chart');
  const legend = document.getElementById('d-legend');
  const badge = document.getElementById('d-samples');
  const list = chartSamples;
  if (!svg) return;
  const W = Math.max(260, Math.round(svg.clientWidth || 640));
  const H = Math.max(150, Math.round(svg.clientHeight || 190));
  const T = 16, B = 30;
  const narrow = W < 470;
  const L = narrow ? 46 : 58, R = narrow ? 42 : 54;
  svg.setAttribute('viewBox', '0 0 ' + W + ' ' + H);
  if (badge) setText(badge, list.length ? list.length + ' 个采样' : '暂无采样');
  if (list.length < 2) {
    svg.innerHTML = '<text x="' + Math.round(W / 2) + '" y="' + Math.round(H / 2) + '"' +
      ' text-anchor="middle" fill="currentColor" opacity=".45" font-size="' + (narrow ? 10 : 13) +
      '">采样不足，任务运行后自动出现曲线</text>';
    setText(legend, '');
    return;
  }
  const innerW = W - L - R, innerH = H - T - B;
  const n = list.length;
  const t0 = list[0].ts, t1 = list[n - 1].ts;
  const span = Math.max(1, t1 - t0);
  const memMax = Math.max.apply(null, list.map(function (s) {
    return Math.max(s.mem_peak || 0, s.mem_current || 0);
  }).concat([1]));
  const cpuMax = Math.max.apply(null, list.map(function (s) {
    return s.cpu_usec || 0;
  }).concat([1]));
  const x = function (s) {
    return L + innerW * Math.min(1, Math.max(0, (s.ts - t0) / span));
  };
  const ym = function (v) { return T + innerH * (1 - (v || 0) / memMax); };
  const yc = function (v) { return T + innerH * (1 - (v || 0) / cpuMax); };
  const line = function (key, yf) {
    return list.map(function (s, i) {
      return (i ? 'L' : 'M') + x(s).toFixed(1) + ',' + yf(s[key]).toFixed(1);
    }).join(' ');
  };
  const memPath = line('mem_peak', ym);
  const curPath = line('mem_current', ym);
  const cpuPath = line('cpu_usec', yc);
  const area = memPath + ' L' + x(list[n - 1]).toFixed(1) + ',' + (T + innerH) +
    ' L' + x(list[0]).toFixed(1) + ',' + (T + innerH) + ' Z';
  let marks = '';
  for (let i = 0; i <= 4; i++) {
    const yy = (T + innerH * i / 4).toFixed(1);
    marks += '<line x1="' + L + '" y1="' + yy + '" x2="' + (W - R) + '" y2="' + yy +
      '" stroke="currentColor" stroke-width="1" opacity="' + (i === 4 ? '.18' : '.07') + '"/>';
    marks += '<text x="' + (L - 9) + '" y="' + (Number(yy) + 3.5) + '" text-anchor="end"' +
      ' font-size="9.5" fill="currentColor" opacity=".45">' +
      esc(fmtBytes(memMax * (1 - i / 4))) + '</text>';
    marks += '<text x="' + (W - R + 9) + '" y="' + (Number(yy) + 3.5) + '" text-anchor="start"' +
      ' font-size="9.5" fill="currentColor" opacity=".45">' +
      esc((cpuMax * (1 - i / 4) / 1e6).toFixed(1)) + 's</text>';
  }
  const ticks = narrow ? Math.min(2, n - 1) : Math.min(4, n - 1);
  const labelSize = narrow ? 9 : 9.5;
  for (let i = 0; i <= ticks; i++) {
    const s = list[Math.round((n - 1) * i / Math.max(1, ticks))];
    marks += '<text x="' + x(s).toFixed(1) + '" y="' + (H - 9) + '" text-anchor="middle"' +
      ' font-size="' + labelSize + '" fill="currentColor" opacity=".4">' +
      esc(fmtDur(s.ts - t0)) + '</text>';
  }
  const last = list[n - 1];
  const dot = function (cx, cy, color) {
    return '<circle cx="' + cx.toFixed(1) + '" cy="' + cy.toFixed(1) + '" r="3.4" fill="' + color +
      '" stroke="currentColor" stroke-opacity=".22"/>';
  };
  svg.innerHTML =
    '<defs><linearGradient id="memFill" x1="0" y1="0" x2="0" y2="1">' +
      '<stop offset="0" stop-color="#5ba8ff" stop-opacity=".32"/>' +
      '<stop offset="1" stop-color="#5ba8ff" stop-opacity="0"/>' +
    '</linearGradient></defs>' +
    marks +
    '<path d="' + area + '" fill="url(#memFill)" stroke="none"/>' +
    '<path d="' + memPath + '" fill="none" stroke="#5ba8ff" stroke-width="2"' +
      ' stroke-linejoin="round" stroke-linecap="round"/>' +
    '<path d="' + curPath + '" fill="none" stroke="#3ddc97" stroke-width="1.4"' +
      ' stroke-dasharray="4 3" opacity=".9"/>' +
    '<path d="' + cpuPath + '" fill="none" stroke="#f5b544" stroke-width="1.6"' +
      ' opacity=".85" stroke-linejoin="round"/>' +
    dot(x(last), ym(last.mem_peak || 0), '#5ba8ff') +
    dot(x(last), yc(last.cpu_usec || 0), '#f5b544');
  legend.innerHTML =
    '<span class="lg"><i class="sw" style="background:#5ba8ff"></i>内存峰值 ' +
      esc(fmtBytes(last.mem_peak)) + '</span>' +
    '<span class="lg"><i class="sw" style="background:#3ddc97"></i>内存当前 ' +
      esc(fmtBytes(last.mem_current)) + '</span>' +
    '<span class="lg"><i class="sw" style="background:#f5b544"></i>累计 CPU ' +
      esc((last.cpu_usec / 1e6).toFixed(1)) + 's</span>' +
    '<span class="lg">左右纵轴各自归一化 · 时长 ' + esc(fmtDur(span)) + '</span>';
}

/* ------------------------------------------------------------ 详情：日志 */
function loadLogs(jobId) {
  return api('/api/jobs/' + encodeURIComponent(jobId) + '/logs?offset=' + logOffset)
    .then(function (r) {
      const next = r.headers.get('X-Calltable-Next');
      return r.text().then(function (text) {
        if (text) {
          logText += text;
          if (logText.length > 200000) logText = logText.slice(-200000);
          const pre = document.getElementById('d-log');
          const atBottom = pre.scrollTop + pre.clientHeight >= pre.scrollHeight - 28;
          pre.textContent = logText;
          if (atBottom) pre.scrollTop = pre.scrollHeight;
          setText(document.getElementById('d-logsize'), fmtBytes(logText.length) + ' / 上限 200 KB');
        }
        if (next) logOffset = parseInt(next, 10);
      });
    }).catch(function () {});
}

/* ------------------------------------------------------------ 详情：产物 */
function downloadArtifact(jobId, name) {
  /* 用 fetch 带 Authorization header 下载；不把 token 拼进 URL（A9）。 */
  api('/api/jobs/' + encodeURIComponent(jobId) + '/artifacts/' + encodeURIComponent(name))
    .then(function (r) { return r.blob(); })
    .then(function (blob) {
      const url = URL.createObjectURL(blob);
      const anchor = document.createElement('a');
      anchor.href = url;
      anchor.download = name.split('/').pop();
      document.body.appendChild(anchor);
      anchor.click();
      anchor.remove();
      setTimeout(function () { URL.revokeObjectURL(url); }, 10000);
    })
    .catch(function () { alert('产物下载失败'); });
}

function renderArtifacts(list, jobId) {
  const box = document.getElementById('d-artifacts');
  if (!list || !list.length) {
    setText(document.getElementById('d-artifact-count'), '');
    box.innerHTML = emptyState('artifacts', '没有产物',
      '任务把文件写进 artifacts/ 目录后会自动收集到这里。');
    return;
  }
  setText(document.getElementById('d-artifact-count'), list.length + ' 个文件');
  box.innerHTML = list.map(function (a) {
    return '<div class="artifact">' +
      '<span class="file-ico">' + icon('file') + '</span>' +
      '<span class="file-main"><span class="file-name">' + esc(a.name) + '</span>' +
      '<span class="file-meta">' + esc(fmtBytes(a.size)) + ' · sha256 ' +
      esc((a.sha256 || '').slice(0, 16)) + '…</span></span>' +
      '<a class="file-dl" href="#" data-job="' + esc(jobId) + '" data-name="' + esc(a.name) +
      '" title="下载" aria-label="下载 ' + esc(a.name) + '">' + icon('download') + '</a></div>';
  }).join('');
  Array.from(box.querySelectorAll('.file-dl')).forEach(function (link) {
    link.addEventListener('click', function (event) {
      event.preventDefault();
      downloadArtifact(link.dataset.job, link.dataset.name);
    });
  });
}

const WEBHOOK_CHIP = { delivered: 'chip-ok', failed: 'chip-bad', pending: 'chip-accent' };

function renderWebhooks(deliveries) {
  const box = document.getElementById('d-webhooks');
  if (!box) return;
  if (!deliveries.length) {
    setText(document.getElementById('d-webhook-count'), '');
    box.innerHTML = '<div class="sub">没有叫号通知。提交时带上 webhook 后，投递记录会显示在这里。</div>';
    return;
  }
  setText(document.getElementById('d-webhook-count'), deliveries.length + ' 个地址');
  box.innerHTML = deliveries.map(function (d) {
    const events = (d.events || []).map(function (e) {
      const detail = '尝试 ' + (e.attempts || 0) +
        (e.last_status ? ' · HTTP ' + e.last_status : '') +
        (e.last_error ? ' · ' + e.last_error : '');
      return '<span class="chip ' + (WEBHOOK_CHIP[e.status] || '') + '" title="' + esc(detail) + '">' +
        esc(e.event) + ' · ' + esc(e.status) + '</span> <span class="file-meta">' +
        esc(detail) + '</span>';
    }).join('<br>');
    return '<div class="artifact">' +
      '<span class="file-ico">' + icon('bell') + '</span>' +
      '<span class="file-main"><span class="file-name">' + esc(d.url) + '</span>' +
      '<span class="file-meta">' + (events || '还没有事件') + '</span></span></div>';
  }).join('');
}

/* ------------------------------------------------------------ 详情：操作 */
function toggleFollow() {
  follow = !follow;
  const btn = document.getElementById('follow');
  setText(btn, '自动刷新：' + (follow ? '开' : '关'));
  btn.setAttribute('aria-pressed', follow ? 'true' : 'false');
  if (follow) refreshDetail();
}

function cancelJob() {
  if (!detailId) return;
  if (!confirm('确定取消 ' + detailId + '？')) return;
  api('/api/jobs/' + encodeURIComponent(detailId) + '/cancel', { method: 'POST' })
    .then(function () { refreshDetail(); })
    .catch(function () { alert('取消失败'); });
}

function renderMissing(jobId) {
  setText(document.getElementById('d-id'), jobId);
  setText(document.getElementById('d-sub'), '找不到这个任务：可能编号有误，或已按保留期清理。');
  document.getElementById('d-status').innerHTML =
    '<span class="chip chip-warn">' + icon('alert') + '任务不存在</span>';
  document.getElementById('d-steps').innerHTML = '';
  document.getElementById('d-cancel').hidden = true;
}

function refreshDetail() {
  if (!detailId || busyDetail) return;
  busyDetail = true;
  spin(true);
  const jobId = detailId;
  api('/api/jobs/' + encodeURIComponent(jobId)).then(function (r) {
    return r.json();
  }).then(function (job) {
    if (jobId !== detailId) return;
    setConn(true);
    renderDetail(job);
    renderArtifacts(job.artifacts || [], job.id);
    renderWebhooks(job.webhook_deliveries || []);
    if (follow) loadLogs(job.id);
    return api('/api/jobs/' + encodeURIComponent(job.id) + '/samples')
      .then(function (r) { return r.json(); })
      .then(function (payload) {
        if (jobId === detailId) drawChart(payload.samples || payload || []);
      });
  }).catch(function (err) {
    const msg = String(err && err.message);
    if (msg.indexOf('404') === 0) {
      /* hub 正常，只是没有这个任务：别报「连接中断」，也别一直显示载入中 */
      setConn(true);
      if (jobId === detailId) renderMissing(jobId);
    } else if (msg !== 'unauthorized') {
      setConn(false);
    }
  }).then(function () {
    busyDetail = false;
    spin(false);
  });
}

/* ------------------------------------------------------------------ 路由 */
let chartResizeTimer = null;
let chartObserver = null;

function watchChartSize() {
  const body = document.querySelector('.chart-body');
  if (!body || !window.ResizeObserver) return;
  if (chartObserver) chartObserver.disconnect();
  chartObserver = new ResizeObserver(function () {
    clearTimeout(chartResizeTimer);
    chartResizeTimer = setTimeout(renderChart, 80);
  });
  chartObserver.observe(body);
}

window.addEventListener('resize', function () {
  if (!detailId) return;
  clearTimeout(chartResizeTimer);
  chartResizeTimer = setTimeout(renderChart, 180);
});

function enter(el) {
  if (!el || REDUCED.matches) return;
  el.classList.remove('view-enter');
  void el.offsetWidth;
  el.classList.add('view-enter');
}

function route() {
  const hash = location.hash || '';
  const match = hash.match(/^#\/job\/(.+)$/);
  const overview = document.getElementById('overview');
  const detail = document.getElementById('detail');
  if (match) {
    const id = decodeURIComponent(match[1]);
    if (id !== detailId) {
      detailId = id;
      logOffset = 0;
      logText = '';
      document.getElementById('d-log').textContent = '';
      document.getElementById('d-meta').innerHTML = '';
      document.getElementById('d-artifacts').innerHTML = '';
      document.getElementById('d-webhooks').innerHTML = '';
      document.getElementById('d-cancel').hidden = false;
      document.getElementById('d-status').innerHTML = '';
      setText(document.getElementById('d-logsize'), '末尾 200 KB');
    }
    overview.classList.add('hidden');
    detail.classList.add('visible');
    enter(detail);
    refreshDetail();
  } else {
    detailId = null;
    detail.classList.remove('visible');
    overview.classList.remove('hidden');
    enter(overview);
    refreshOverview();
  }
}

/* ------------------------------------------------------------- 启动/轮询 */
document.addEventListener('click', function (event) {
  const tr = event.target.closest ? event.target.closest('tr.clickable') : null;
  if (!tr) return;
  if (event.target.closest('a, button, input, select')) return;
  const id = tr.dataset.key;
  if (id) location.hash = '#/job/' + encodeURIComponent(id);
});

document.addEventListener('visibilitychange', function () {
  if (document.hidden) return;
  if (detailId) refreshDetail(); else refreshOverview();
});

window.addEventListener('hashchange', route);

(function boot() {
  if (BESZEL) {
    document.getElementById('beszel').href = BESZEL;
  } else {
    document.getElementById('beszel').style.display = 'none';
  }
  const input = document.getElementById('tokeninput');
  if (input) {
    input.addEventListener('keydown', function (event) {
      if (event.key === 'Enter') saveToken();
    });
  }
  if (!tok()) showTokenBox();
  route();
  watchChartSize();
  timer = setInterval(function () {
    if (document.hidden) return;
    if (detailId) refreshDetail(); else refreshOverview();
  }, 3000);
})();
"""

_SCRIPT = "<script>\n" + _JS_UTIL + _JS_OVERVIEW + _JS_DETAIL + "</script>"

PAGE = (_HEAD + _STYLE + "</head>\n<body>\n" + _BODY + _SCRIPT
        + "\n</body>\n</html>\n")


def render_page(token: str, beszel_url: str = "") -> str:
    return (PAGE
            .replace("__TOKEN__", json.dumps(token))
            .replace("__BESZEL__", json.dumps(beszel_url or "")))
