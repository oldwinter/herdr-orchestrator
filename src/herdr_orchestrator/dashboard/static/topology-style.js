"use strict";

function topologyStyles({ compact = false, animate = true } = {}) {
  const mono = "ui-monospace, SFMono-Regular, Menlo, Consolas, monospace";
  const projectPadding = compact ? 28 : 44;
  const worktreePadding = compact ? 24 : 34;
  const projectWidth = compact ? 220 : 280;
  const projectHeight = compact ? 76 : 90;
  const worktreeWidth = compact ? 210 : 250;
  const worktreeHeight = compact ? 72 : 82;
  const tabWidth = compact ? 148 : 190;
  const tabHeight = compact ? 48 : 58;
  const tabPadding = compact ? 20 : 27;
  const paneWidth = compact ? 156 : 218;
  const paneHeight = compact ? 62 : 78;
  const fontSize = compact ? 9 : 11;
  return [
    {
      selector: "node",
      style: {
        "font-family": mono,
        "font-size": fontSize,
        "font-weight": 600,
        "label": "data(displayLabel)",
        "text-wrap": "wrap",
        "text-max-width": compact ? 150 : 210,
        "color": "#bcc3c8",
        "overlay-opacity": 0,
        "transition-property": "border-color, background-color, opacity",
        "transition-duration": animate ? "160ms" : "0ms",
      },
    },
    {
      selector: "node[kind = 'project']",
      style: {
        "shape": "round-rectangle",
        "background-color": "#11161a",
        "background-opacity": 0.72,
        "border-color": "#525d66",
        "border-width": 1,
        "padding": projectPadding,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 12,
        "text-margin-y": 12,
        "font-size": compact ? 14 : 13,
        "color": "#f0eee8",
      },
    },
    {
      selector: "node[kind = 'project'], node[kind = 'worktree'], node[kind = 'tab']",
      style: {
        "text-background-color": "#0d1114",
        "text-background-opacity": 1,
        "text-background-padding": 5,
        "text-background-shape": "rectangle",
      },
    },
    {
      selector: "node[kind = 'worktree']",
      style: {
        "shape": "round-rectangle",
        "background-color": "#162016",
        "background-opacity": 0.72,
        "border-color": "#879d48",
        "border-style": "dashed",
        "border-width": 1,
        "padding": worktreePadding,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 10,
        "text-margin-y": 10,
        "color": "#d7ff64",
        "font-size": compact ? 12 : 11,
      },
    },
    {
      selector: "node[kind = 'tab']",
      style: {
        "shape": "round-rectangle",
        "width": tabWidth,
        "height": tabHeight,
        "background-color": "#13202b",
        "background-opacity": 0.82,
        "border-color": "#4e83aa",
        "border-width": 1,
        "padding": tabPadding,
        "text-valign": "top",
        "text-halign": "left",
        "text-margin-x": 9,
        "text-margin-y": 9,
        "color": "#91c9f7",
        "font-size": compact ? 12 : 11,
      },
    },
    {
      selector: "node[kind = 'pane']",
      style: {
        "shape": "round-rectangle",
        "width": paneWidth,
        "height": paneHeight,
        "background-color": "#1b2024",
        "border-color": "#4a535b",
        "border-width": 1,
        "text-valign": "center",
        "text-halign": "center",
        "line-height": compact ? 1.4 : 1.65,
        "color": "#d8dde0",
        "font-size": compact ? 14 : 11,
        "text-max-width": compact ? 130 : 170,
        "text-overflow-wrap": "anywhere",
      },
    },
    {
      selector: "node[kind = 'project']:childless",
      style: { "width": projectWidth, "height": projectHeight },
    },
    {
      selector: "node[kind = 'worktree']:childless",
      style: { "width": worktreeWidth, "height": worktreeHeight },
    },
    {
      selector: "node.status-working",
      style: { "border-color": "#75baff", "border-width": 2 },
    },
    {
      selector: "node.status-blocked",
      style: { "border-color": "#ff756d", "border-width": 2, "background-color": "#2b1a1b" },
    },
    {
      selector: "node.status-done, node.status-idle",
      style: { "border-color": "#6ee7a8" },
    },
    {
      selector: "node.is-focused",
      style: { "border-style": "double", "border-width": 4 },
    },
    {
      selector: "node.is-hovered",
      style: {
        "border-color": "#d7ff64",
        "border-width": 2,
        "overlay-color": "#d7ff64",
        "overlay-opacity": 0.06,
        "overlay-padding": 6,
      },
    },
    ...(compact ? [{
      selector: "node[kind = 'project']:selected, node[kind = 'worktree']:selected, node[kind = 'tab']:selected, "
        + "node[kind = 'worktree'].is-selection-path",
      style: {
        "text-halign": "center",
        "text-margin-x": 0,
      },
    }] : []),
    {
      selector: "node:selected",
      style: {
        "border-color": "#d7ff64",
        "border-width": 3,
        "overlay-color": "#d7ff64",
        "overlay-opacity": 0.08,
        "overlay-padding": 8,
      },
    },
  ];
}
