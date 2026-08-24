# Herdr topology canvas technology options

**Research date:** 2026-08-24
**Scope:** read-only visualization of `project -> worktree/workspace -> tab -> pane`
relationships in the existing local dashboard.
**Decision:** adopt **Cytoscape.js 3.34.x**, self-host its pinned standalone build, and keep
the dashboard framework-free.

## Executive decision

Cytoscape.js is the best fit for this repository, even though React Flow and AntV G6 can
produce equally polished demos. The deciding factor is the complete system boundary:

- Cytoscape.js is a framework-independent graph model and renderer with no external runtime
  dependencies. Its official distribution includes `cytoscape.min.js` for a plain `<script>`
  environment, exactly matching this dashboard's current delivery model
  ([getting started](https://github.com/cytoscape/cytoscape.js/blob/unstable/documentation/md/getting-started.md),
  [factsheet](https://js.cytoscape.org/#introduction/factsheet)).
- Compound nodes are native graph elements, declared with a node's `parent` field. Parent
  bounds follow their descendants, which directly models project/worktree/tab containment
  ([compound nodes](https://js.cytoscape.org/#notation/compound-nodes)).
- Pan, wheel/pinch zoom, fit and viewport APIs are built in, and user navigation can remain
  enabled while nodes are locked for a read-only surface
  ([initialization and viewport options](https://js.cytoscape.org/#core/initialisation)).
- The standard renderer uses the browser Canvas API. An optional WebGL mode exists, but the
  project still describes it as a preview with visual-style limitations, so this topology
  should use the mature Canvas path
  ([official renderer note](https://blog.js.cytoscape.org/2025/01/13/webgl-preview/)).
- The built-in CoSE layout explicitly supports compound graphs, so the first implementation
  does not need a second layout dependency
  ([CoSE layout](https://js.cytoscape.org/#layouts/cose)).
- The core and first-party extensions are MIT licensed
  ([official factsheet](https://js.cytoscape.org/#introduction/factsheet),
  [package metadata](https://github.com/cytoscape/cytoscape.js/blob/unstable/package.json)).

The recommendation is therefore **Cytoscape.js core**, without ELK/fCoSE in the first version.
The built-in CoSE is the first generic-layout benchmark, but the implementation should keep the
layout choice behind the graph projection so real topology fixtures can decide between CoSE and a
deterministic preset.

## Repository constraints that drive the choice

The dashboard is not a frontend application with its own build. It is plain
`index.html` + `dashboard.css` + `dashboard.js`; the Python package includes only matching
static globs ([`pyproject.toml`](../../pyproject.toml),
[`index.html`](../../src/herdr_orchestrator/dashboard/static/index.html)). The npm package also
ships those raw assets, and has no application dependency or frontend build script
([`package.json`](../../package.json)).

The Python HTTP server uses an explicit static-asset allowlist and sends a strict
`default-src 'self'`, `script-src 'self'`, `style-src 'self'` CSP. Loading a graph library from
a CDN is therefore both incompatible with the current policy and undesirable for this
local-first tool
([`server.py`](../../src/herdr_orchestrator/dashboard/server.py)). Any selected browser artifact
must be pinned, checked into the packaged static assets, served with the correct content type,
and covered by distribution tests.

State arrives as a complete idempotent SSE snapshot rather than incremental graph events. The
browser currently rebuilds the topology HTML for every snapshot
([`dashboard.js`](../../src/herdr_orchestrator/dashboard/static/dashboard.js)); the projector
currently emits only `topology.workspaces[]`, nesting tabs and panes and attaching an optional
worktree object to each workspace
([`projector.py`](../../src/herdr_orchestrator/dashboard/projector.py)). The observer intentionally
whitelists topology fields and scopes observations to the configured workflow workspace
([`observer.py`](../../src/herdr_orchestrator/dashboard/observer.py),
[`docs/dashboard.md`](../dashboard.md)).

This creates two implementation requirements independent of library choice:

1. Preserve one graph instance across SSE events and reconcile elements by stable ID. Recreating
   the graph every two seconds would reset viewport state and make any layout visibly jump.
2. Define the missing project level. The current public snapshot has no `project_id` or
   `repo_root`, and a worktree is an attribute of a workspace rather than its own topology node.
   For the current single-workflow view, the configured workflow root can be projected as one
   synthetic project. True multi-project display requires an additive, whitelisted snapshot
   field rather than guessing project identity from labels or paths in JavaScript.

## Option comparison

Versions and package sizes below are an npm registry snapshot taken on the research date. The
`unpackedSize` values are installation/distribution sizes, not browser transfer sizes. Where a
project publishes a standalone browser artifact, the second size is the raw minified file and a
local `gzip -9` measurement read directly from that version's official npm tarball. These numbers
are useful for relative integration cost, not as a production performance benchmark.

| Option | Current status and license | Rendering / framework | Grouping and layout | Browser artifact / fit |
| --- | --- | --- | --- | --- |
| **Cytoscape.js** | 3.34.1, published 2026-08-11; MIT; actively maintained ([registry](https://www.npmjs.com/package/cytoscape), [repository](https://github.com/cytoscape/cytoscape.js)) | Canvas by default; framework-free; WebGL is optional preview | Native compound nodes; built-in compound-aware CoSE plus breadth-first/grid/concentric and an extension ecosystem ([layouts](https://js.cytoscape.org/#layouts)) | 435,503 B raw / 136,402 B gzip standalone UMD; 5.72 MB unpacked package; **best fit** |
| **React Flow** (`@xyflow/react`) | 12.11.3, published 2026-08-12; MIT; active ([registry](https://www.npmjs.com/package/@xyflow/react), [repository](https://github.com/xyflow/xyflow)) | React/ReactDOM >=17 peers; React components for node-based UIs ([package metadata](https://github.com/xyflow/xyflow/blob/main/packages/react/package.json)) | `parentId` and `extent: 'parent'` provide subflows/groups, but React Flow deliberately does not implement auto-layout; Dagre/D3/ELK are separate choices ([subflows](https://reactflow.dev/learn/layouting/sub-flows), [layout guide](https://reactflow.dev/learn/layouting/layouting)) | Library UMD + CSS is about 206 KB raw / 62 KB gzip, excluding React/ReactDOM and a layout engine. Excellent UI API, but adopting it means migrating this panel or the page to React and adding a build |
| **AntV G6** | 5.1.1, published 2026-06-10; MIT; active ([registry](https://www.npmjs.com/package/@antv/g6), [license](https://github.com/antvis/G6/blob/v5/LICENSE)) | Framework-free; Canvas default, optional SVG/WebGL renderers ([renderer](https://g6.antv.antgroup.com/en/manual/further-reading/renderer)) | Native nested combos, collapse/expand, and `combo-combined` per-level layouts are the strongest out-of-box grouping feature set in this comparison ([combos](https://g6.antv.antgroup.com/en/manual/element/combo/overview), [combo layout](https://g6.antv.antgroup.com/en/manual/layout/combo-combined-layout)); pan/zoom are built-in behaviors ([behaviors](https://g6.antv.antgroup.com/en/manual/behavior/overview)) | 1,383,347 B raw / 390,093 B gzip UMD; 7.60 MB unpacked before its substantial dependency graph. **Runner-up**, but the extra engine surface is not justified for the expected topology size |
| **AntV X6** | 3.1.8, published 2026-08-11; MIT; active ([registry](https://www.npmjs.com/package/@antv/x6), [repository](https://github.com/antvis/X6)) | SVG/HTML engine; framework-free core with optional React/Vue/Angular node rendering ([node rendering](https://x6.antv.antgroup.com/en/tutorial/basic/node)) | Parent-child embedding and group constraints are capable; pan/zoom are built in ([groups](https://x6.antv.antgroup.com/en/tutorial/intermediate/group), [graph navigation](https://x6.antv.antgroup.com/en/tutorial/basic/graph)). General auto-layout is normally composed separately | 583,499 B raw / 166,194 B gzip UMD; 8.56 MB unpacked. Optimized for graph **editing** (ports, routers, tools), while this feature is read-only |
| **D3 + elkjs** | D3 7.9.0 (ISC) and elkjs 0.12.0 (EPL-2.0 OR GPL-3.0-or-later) ([D3 registry](https://www.npmjs.com/package/d3), [elkjs registry](https://www.npmjs.com/package/elkjs), [elkjs license](https://github.com/kieler/elkjs/blob/master/LICENSE.md)) | D3 is rendering-agnostic; zoom works with HTML, SVG or Canvas. ELK only computes positions; it does not render ([D3 zoom](https://d3js.org/d3-zoom), [ELK overview](https://eclipse.dev/elk/), [elkjs](https://github.com/kieler/elkjs)) | ELK has excellent layered, port-aware and hierarchical/compound layout, including explicit hierarchy handling ([hierarchy option](https://eclipse.dev/elk/reference/options/org-eclipse-elk-hierarchyHandling.html)) | D3 + bundled ELK is about 1.89 MB raw / 559 KB gzip before application rendering code. Maximum control, but requires implementing scene reconciliation, node rendering, edge rendering and hit-testing glue |
| **tldraw** | 5.3.2, published 2026-08-24; source available under the tldraw SDK license; production requires a valid trial/commercial/hobby key ([registry](https://www.npmjs.com/package/tldraw), [license](https://tldraw.dev/community/license)) | React SDK; shapes render through a React component hierarchy, with some overlays on one HTML canvas ([shape architecture](https://tldraw.dev/sdk-features/shapes), [5.0 rendering changes](https://tldraw.dev/blog/tldraw-sdk-5-0)) | Excellent infinite-canvas camera, shapes, frames, groups and editing UX, but no topology-specific automatic graph layout | 14.70 MB unpacked for the top-level package before its many dependencies and app bundle. Licensing, React migration and editor-oriented state are all unnecessary here |
| **Konva** | 10.3.1, published 2026-08-15; MIT; active ([registry](https://www.npmjs.com/package/konva), [changelog](https://github.com/konvajs/konva/blob/master/CHANGELOG.md)) | Framework-free Canvas 2D object model; each layer uses a visible scene canvas and hidden hit canvas ([overview](https://konvajs.org/docs/overview.html)) | Nested shape groups exist, and official examples show infinite canvas pan/zoom, but containment semantics, graph edges and automatic topology layout must be built by the application ([groups](https://konvajs.org/docs/groups_and_layers/Groups.html), [infinite canvas](https://konvajs.org/docs/sandbox/Infinite_Canvas.html)) | 188,368 B raw / 56,198 B gzip standalone; 1.49 MB unpacked. Small and elegant as a drawing primitive, but too much graph logic remains custom |
| **PixiJS** | 8.20.0, published 2026-08-20; MIT; active ([registry](https://www.npmjs.com/package/pixi.js), [repository](https://github.com/pixijs/pixijs)) | GPU-first WebGL/WebGPU renderer with an interactive scene graph ([architecture](https://pixijs.com/8.x/guides/concepts/architecture), [events](https://pixijs.com/8.x/guides/components/events)) | Containers provide visual hierarchy, not graph compound-node semantics or topology layout ([containers](https://pixijs.com/8.x/guides/components/scene-objects/container)) | 818,297 B raw / 230,420 B gzip standalone; 74.15 MB unpacked. Valuable for very large/high-frame-rate animated scenes, not dozens or hundreds of mostly static operational nodes |

### Why not React Flow

React Flow has the best developer experience when the host application is already React and
nodes need rich DOM content. Its parent/subflow model is clear and its pan/zoom controls are
polished. Here it would introduce React, ReactDOM, state-store dependencies, CSS, bundling and a
layout package into a page currently served as three hand-authored assets. The topology panel is
read-only and can express pane status as concise Canvas labels, so React's main advantage does not
offset that migration.

### Why not G6

G6 is the closest technical competitor and its combos plus `combo-combined` layout are arguably
the most direct feature match. It is the fallback if topology grows into a large, multi-project,
combo-heavy visualization and empirical tests show Cytoscape's compound layout is inadequate.
For the current scoped observer, G6's roughly 3x standalone transfer size and much larger
dependency/API surface buy capabilities that are not yet required.

### Why not X6, D3 + ELK, or general canvas engines

X6 prioritizes editor mechanics; tldraw is a full whiteboard product foundation; Konva and PixiJS
are rendering engines rather than graph visualization systems. Each would make Herdr own more
layout and relationship behavior than necessary. D3 + ELK gives the most control and strongest
hierarchical layout, but it is an assembly: ELK returns coordinates and D3 supplies behaviors,
leaving the application responsible for the renderer and update lifecycle. It is appropriate
only after a concrete diagram requirement exceeds Cytoscape/G6.

## Recommended integration shape

This is an implementation boundary, not a mandate to expose mutation:

1. Vendor a pinned `cytoscape.min.js` under
   `src/herdr_orchestrator/dashboard/static/`; do not use a CDN. Extend the Python asset allowlist,
   Python package data, npm `files`, and packaged-asset tests. Record the upstream version,
   license and checksum.
2. Keep the existing vanilla page and SSE connection. Create one Cytoscape instance and reconcile
   incoming nodes by IDs such as `project:<id>`, `workspace:<id>`, `worktree:<stable-path-id>`,
   `tab:<tab_id>`, and `pane:<pane_id>`.
3. Use compound `parent` relations for containment. Recommended visual hierarchy:
   `project -> (worktree -> workspace | workspace) -> tab -> pane`. If product language should
   omit workspace, use the worktree-or-workspace node as one visual group rather than hiding the
   runtime identity from the data model. Keep the agent name/status as pane data and a badge, not
   another structural level.
4. Set nodes ungrabbable/unselectable for the read-only contract while retaining background pan,
   wheel/pinch zoom, fit and reset controls. A user should be able to navigate, not rearrange
   observed runtime state.
5. Run layout only on initial load or when a structural hash changes. Apply status, focus and label
   changes with `cy.batch()` without rerunning layout. Preserve zoom/pan across SSE snapshots and
   call `fit()` only on first non-empty render or an explicit control. Start by fixture-testing
   built-in CoSE against a deterministic hierarchy preset.
6. Maintain a semantic DOM summary/detail view for keyboard and screen-reader access because
   Canvas pixels do not replace accessible topology text. This can also serve as the no-JavaScript
   or renderer-failure fallback.
7. Keep renderer input restricted to the existing whitelist. Do not add pane output, prompts,
   environment variables or arbitrary terminal metadata to make the diagram richer.

## Acceptance risks to test before finalizing

- **Data identity:** worktree paths can change and may contain sensitive local information. Prefer
  opaque IDs for element identity and display only the already-approved label/branch/path fields.
- **Layout stability:** add, remove and status-only SSE fixtures; verify status updates do not move
  nodes and new panes do not reset the camera.
- **Nested bounds:** test direct workspace, linked worktree, multiple tabs, empty tabs, panes with
  and without agents, long labels and more than one workspace.
- **Packaging:** verify wheel/sdist/npm artifacts all contain the vendored library and the server
  serves it under the existing self-only CSP with `nosniff`.
- **Responsive rendering:** verify the graph has a stable explicit height, resizes with its panel,
  and remains legible on the dashboard's desktop and narrow layouts.
- **License hygiene:** ship the Cytoscape.js MIT notice with the vendored asset and keep the exact
  upstream version/checksum visible to future upgrades.

## Bottom line

Choose **Cytoscape.js now**. It is the smallest architectural change that provides real graph
semantics, nested grouping, layout APIs, Canvas rendering and whiteboard navigation. Keep
**G6 as the evidence-driven fallback** if multi-level combo layout quality, not scale alone,
becomes a demonstrated limitation.

## Implementation validation update

The integrated multi-worktree fixture showed that built-in CoSE spreads a pure containment graph
with no relationship edges into a long horizontal row. The shipped implementation therefore uses
Cytoscape's `preset` layout with deterministic grouping: worktrees form columns, tabs stack within
their worktree, and panes use a two-column grid. This is not a renderer fallback; compound nodes,
Canvas hit testing, selection, pan, zoom and fit remain Cytoscape-owned. It also makes structural
updates deterministic while status-only SSE snapshots leave every position unchanged.
