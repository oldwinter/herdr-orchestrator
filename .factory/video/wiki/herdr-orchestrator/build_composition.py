from __future__ import annotations

import html
import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parent
SCENES = json.loads((ROOT / "scenes-timed.json").read_text(encoding="utf-8"))
TOTAL_DURATION = 438.672


def esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def evidence(scene: dict[str, object]) -> str:
    paths = "".join(
        f'<span class="path">{esc(path)}</span>' for path in scene["evidence"]
    )
    return f'<div class="evidence reveal">{paths}</div>'


def scene_shell(scene: dict[str, object], body: str) -> str:
    start = float(scene["start"])
    duration = max(0.001, float(scene["duration"]) - 0.002)
    track = 1 + (int(str(scene["id"])) - 1) % 4
    return f"""
    <section class="clip scene scene-{esc(scene['id'])}" id="scene-{esc(scene['id'])}"
      data-start="{start:.3f}" data-duration="{duration:.3f}" data-track-index="{track}">
      <header class="factory-shell">
        <div class="factory-label">FACTORY <span>/</span> HERDR ORCHESTRATOR</div>
        <div class="scene-counter">{esc(scene['id'])} <span>/ 12</span></div>
      </header>
      <div class="scene-body">
        <div class="kicker">{esc(scene['goal'])}</div>
        <h1 class="scene-title">{esc(scene['title'])}</h1>
        {body}
        {evidence(scene)}
      </div>
    </section>
    """


BODIES = {
    "01": """
      <div class="hero-layout">
        <div class="hero-statement reveal">HERDR 承载终端<br><span>COORDINATOR 拥有事实</span></div>
        <div class="control-map reveal">
          <div class="node model">MODEL OUTPUT</div>
          <div class="connector orange"></div>
          <div class="node control active">DETERMINISTIC<br>CONTROL PLANE</div>
          <div class="branch branch-a"></div><div class="branch branch-b"></div>
          <div class="node sqlite">SQLITE<br>DURABLE STATE</div>
          <div class="node herdr">HERDR<br>PTY RUNTIME</div>
        </div>
      </div>
      <div class="hero-tags reveal"><span>LOCAL-FIRST</span><span>6 HARNESSES</span><span>NO MODEL-OWNED STATE</span></div>
    """,
    "02": """
      <div class="pipeline reveal">
        <div class="stage"><small>01 / CONFIG</small><strong>WORKFLOW TOML</strong><p>并发 · 租约 · 超时 · placement</p></div>
        <div class="arrow">→</div>
        <div class="stage selected"><small>02 / ROUTE</small><strong>COMPACT CATALOG</strong><p>仅当前候选的紧凑能力</p></div>
        <div class="arrow">→</div>
        <div class="stage"><small>03 / DISPATCH</small><strong>FULL PROFILE</strong><p>选中后才加载完整上下文</p></div>
      </div>
      <div class="harness-strip reveal">
        <span>DROID</span><span>GROK</span><span>CODEX</span><span>PI</span><span>CLAUDE</span><span>HERMES</span>
      </div>
    """,
    "03": """
      <div class="state-board reveal">
        <div class="state pending"><i></i><b>PENDING</b><small>dedupe + available_at</small></div>
        <div class="flowline"></div>
        <div class="state running active"><i></i><b>RUNNING</b><small>attempt + lease + correlation</small></div>
        <div class="fanout"></div>
        <div class="outcomes">
          <div class="state success"><i></i><b>SUCCEEDED</b><small>settled + receipt</small></div>
          <div class="state blocked"><i></i><b>BLOCKED</b><small>manual resume</small></div>
          <div class="state failed"><i></i><b>FAILED</b><small>budget exhausted</small></div>
        </div>
      </div>
      <div class="invariant reveal"><span>BEGIN IMMEDIATE</span><span>STALE ATTEMPT → JOB_LEASE_LOST</span><span>WAVE ≤ MAX_PARALLEL</span></div>
    """,
    "04": """
      <div class="ladder reveal">
        <div class="rung done"><em>01</em><div><b>PROVISIONED</b><small>pane + shell process</small></div></div>
        <div class="rung done"><em>02</em><div><b>INTERACTIVE READY</b><small>interactive_ready = true</small></div></div>
        <div class="rung active"><em>03</em><div><b>TURN OBSERVED</b><small>state_change_seq strictly advances</small></div></div>
        <div class="rung"><em>04</em><div><b>SETTLED</b><small>stable idle / done only</small></div></div>
        <div class="rung"><em>05</em><div><b>VERIFIED</b><small>current-turn receipt</small></div></div>
      </div>
      <div class="reject-panel reveal"><small>NOT SUCCESS</small><span>BLOCKED</span><span>WORKING</span><span>UNKNOWN</span><span>TIMEOUT</span></div>
    """,
    "05": """
      <div class="topology-scene reveal">
        <div class="project-box">
          <div class="project-label">PROJECT / WORKFLOW</div>
          <div class="placement-grid">
            <div class="placement"><b>TAB</b><small>独立可见位置</small><div class="mini-pane one"></div></div>
            <div class="placement active"><b>PANE</b><small>wave 共享 tab</small><div class="mini-pane split"></div></div>
            <div class="placement"><b>WORKTREE</b><small>branch + checkout + workspace</small><div class="mini-branch">ho/&lt;workflow&gt;/&lt;task&gt;</div></div>
          </div>
        </div>
        <div class="decision-stack">
          <span>1 · EXPLICIT OVERRIDE</span><span>2 · WORKER DEFAULT</span><span>3 · READ / WRITE SIGNALS</span><span>4 · BOUNDED CONTROLLER</span>
        </div>
      </div>
      <div class="warning reveal">WORKTREE = CHECKOUT ISOLATION <span>≠ SECURITY SANDBOX</span></div>
    """,
    "06": """
      <div class="harness-grid reveal">
        <div><small>01</small><b>DROID</b><code>--auto high</code></div>
        <div><small>02</small><b>GROK</b><code>--always-approve</code></div>
        <div><small>03</small><b>CODEX</b><code>bypass approvals</code></div>
        <div><small>04</small><b>PI</b><code>--approve</code></div>
        <div class="active"><small>05</small><b>CLAUDE</b><code>skip permissions</code></div>
        <div><small>06</small><b>HERMES</b><code>--yolo --accept-hooks</code></div>
      </div>
      <div class="trust-guard reveal">
        <div><small>CLAUDE TRUST GUARD</small><b>3 stable markers</b></div>
        <div class="plus">+</div>
        <div><small>EXACT LINE MATCH</small><b>resolved execution root</b></div>
        <div class="equals">=</div>
        <div class="enter">SEND ENTER</div>
      </div>
    """,
    "07": """
      <div class="evidence-compare reveal">
        <div class="receipt-card">
          <small>OUTPUT PREFIX</small><h3>NEW LINES ONLY</h3>
          <div class="terminal-lines"><i></i><i></i><i class="new"></i><i class="new short"></i></div>
          <p>prompt echo 与旧 turn 不算证据</p>
        </div>
        <div class="receipt-card">
          <small>FILE RECEIPT</small><h3>BASELINE → SHA-256</h3>
          <div class="hash-row"><span>before</span><code>71d…</code></div>
          <div class="hash-row changed"><span>after</span><code>c84…</code></div>
          <p>拒绝 absolute · .. · symlink escape</p>
        </div>
      </div>
      <div class="recovery-row reveal"><span>FAILED → RETRY + BUDGET</span><span>BLOCKED → SAME AGENT · PANE · ATTEMPT</span></div>
    """,
    "08": """
      <div class="dashboard-frame reveal">
        <div class="dash-top"><span>QUEUE 12</span><span class="orange-text">ATTENTION 2</span><span>AGENTS 6</span><span>SSE LIVE</span></div>
        <div class="dash-columns">
          <div class="kanban"><small>DURABLE QUEUE</small><div class="job"></div><div class="job compact"></div><div class="job warning-job"></div></div>
          <div class="graph">
            <small>COMPOUND TOPOLOGY</small>
            <div class="graph-project">PROJECT
              <div class="graph-worktree">WORKTREE
                <div class="graph-tab">TAB<div class="graph-pane active">PANE</div><div class="graph-pane">PANE</div></div>
              </div>
            </div>
          </div>
          <div class="attention-list"><small>ATTENTION</small><p>job_blocked</p><p>lease_expired</p><p>runtime drift</p></div>
        </div>
      </div>
      <div class="security-strip reveal"><span>LOOPBACK</span><span>HOST CHECK</span><span>CSP</span><span>READ-ONLY</span><span>NO PROMPT</span></div>
    """,
    "09": """
      <div class="dual-track reveal">
        <div class="track queue-track"><small>DEFAULT SURFACE</small><h3>DURABLE QUEUE</h3><p>enqueue → claim → dispatch → receipt</p><span>普通任务，不自动扩大权限</span></div>
        <div class="gate">EXPLICIT<br>OPT-IN</div>
        <div class="track delivery-track"><small>DELIVERY SURFACE</small><h3>STANDARDIZED DELIVERY</h3>
          <div class="delivery-steps"><i>SPEC</i><i>TICKET DAG</i><i>WORKTREES</i><i>REVIEW</i></div>
          <span>成功停在隔离 integration branch</span>
        </div>
      </div>
      <div class="review-axis reveal"><b>STANDARDS</b><span>∥</span><b>SPEC</b><span>→ CONTROLLER ADJUDICATION → ≤ 2 REPAIRS</span></div>
    """,
    "10": """
      <div class="boundary-grid reveal">
        <div><small>MODEL OUTPUT</small><b>STRICT SCHEMA</b><p>exact keys · enum · allowlist</p></div>
        <div><small>TELEMETRY</small><b>LOCAL + SANITIZED</b><p>exporters default off</p></div>
        <div><small>INSTALLER</small><b>OWNERSHIP HASH</b><p>symlink fails closed</p></div>
        <div><small>TERMINAL GC</small><b>DRY-RUN FIRST</b><p>created pane evidence</p></div>
      </div>
      <div class="trust-note reveal"><span>TRUST DOMAIN</span><b>SAME OS USER</b><span>RECEIPT</span><b>CONSERVATIVE EVIDENCE, NOT A SIGNATURE</b></div>
    """,
    "11": """
      <div class="release-map reveal">
        <div class="release-node"><small>SOURCE</small><b>MAIN</b></div><div class="release-arrow">→</div>
        <div class="release-node"><small>GATE</small><b>JUST CHECK</b></div><div class="release-arrow">→</div>
        <div class="release-node selected"><small>SELF-HOSTED</small><b>REGISTRY PLAN</b></div><div class="release-arrow">→</div>
        <div class="release-node"><small>GITHUB-HOSTED</small><b>OIDC PUBLISH</b></div><div class="release-arrow">→</div>
        <div class="release-node"><small>PUBLIC</small><b>NPM + RELEASE</b></div>
      </div>
      <div class="zero-deps reveal"><div><strong>0</strong><span>PYTHON RUNTIME<br>PACKAGE DEPS</span></div><div><strong>0</strong><span>NPM RUNTIME<br>PACKAGE DEPS</span></div><p>NODE 20+ · PYTHON 3.12+ · HERDR 0.8.2+</p></div>
    """,
    "12": """
      <div class="metrics-grid reveal">
        <div><strong>19</strong><span>REACHABLE<br>COMMITS</span></div>
        <div><strong>26</strong><span>PYTHON<br>MODULES</span></div>
        <div><strong>198</strong><span>STATIC TEST<br>FUNCTIONS</span></div>
        <div><strong>0.88:1</strong><span>TEST / SOURCE<br>PYTHON LINES</span></div>
      </div>
      <div class="recap reveal">
        <div><em>01</em><b>模型输出受约束</b></div>
        <div><em>02</em><b>状态由 Coordinator 拥有</b></div>
        <div><em>03</em><b>成功由当前执行的新证据证明</b></div>
      </div>
      <div class="final-command reveal"><code>just check</code><span>→</span><code>just doctor</code><span>→</span><code>just smoke</code></div>
    """,
}


CSS = r"""
@font-face{font-family:Geist;src:url("./assets/fonts/Geist-Light.woff2") format("woff2");font-weight:300}
@font-face{font-family:"Geist Mono";src:url("./assets/fonts/GeistMono-Regular.woff2") format("woff2");font-weight:400}
@font-face{font-family:"Noto Sans CJK SC";src:local("Noto Sans CJK SC")}
:root{--bg:#000;--surface:#161413;--border:#342f2d;--gray:#9b8e87;--muted:#cbc5c2;--white:#fff;--orange:#ee6018;--light:#f2f0f0}
*{box-sizing:border-box}html,body{margin:0;width:1280px;height:720px;overflow:hidden;background:#000;color:var(--white);font-family:Geist,"Noto Sans CJK SC",sans-serif;font-weight:300}
body:before{content:"";position:fixed;inset:0;opacity:.24;background-image:linear-gradient(rgba(255,255,255,.025) 1px,transparent 1px),linear-gradient(90deg,rgba(255,255,255,.025) 1px,transparent 1px);background-size:32px 32px;pointer-events:none}
#root{position:relative;width:1280px;height:720px;background:#000}.scene{position:absolute;inset:0;padding:94px 64px 54px;overflow:hidden}.scene-body{height:100%;position:relative}
.factory-shell{position:absolute;left:0;right:0;top:0;height:66px;border-bottom:1px solid var(--border);display:flex;align-items:center;justify-content:space-between;padding:0 36px;font-family:"Geist Mono","Noto Sans CJK SC",monospace;font-size:13px;letter-spacing:.08em;color:#fff}
.factory-label span,.scene-counter span{color:var(--gray)}.scene-counter{font-variant-numeric:tabular-nums}.kicker{font-family:"Geist Mono","Noto Sans CJK SC",monospace;color:var(--orange);font-size:13px;letter-spacing:.12em;text-transform:uppercase;margin-bottom:10px}
.scene-title{font-size:42px;line-height:1.05;letter-spacing:-.02em;font-weight:300;margin:0 0 28px;max-width:900px}.evidence{position:absolute;left:0;right:0;bottom:0;display:flex;gap:8px;flex-wrap:wrap}
.path{font-family:"Geist Mono","Noto Sans CJK SC",monospace;font-size:11px;color:var(--gray);border:1px solid var(--border);background:#0b0a0a;padding:7px 10px;border-radius:2px}
.clip{visibility:hidden}.progress-shell{position:absolute;left:0;right:0;bottom:0;height:8px;border-top:1px solid var(--border);background:#090808;z-index:20}.progress-fill{height:100%;width:0;background:var(--orange)}
.reveal{will-change:transform,opacity}.hero-layout{display:grid;grid-template-columns:1fr 1.2fr;gap:48px;align-items:center;height:390px}.hero-statement{font-size:30px;line-height:1.4;color:var(--muted)}.hero-statement span{color:#fff}.control-map{height:360px;position:relative;border:1px solid var(--border);background:var(--surface)}
.node{position:absolute;border:1px solid var(--border);background:#0b0a0a;padding:14px 18px;font:13px/1.45 "Geist Mono",monospace}.node.model{left:34px;top:140px}.node.control{left:225px;top:120px}.node.active{border-color:var(--orange)}.node.sqlite{left:435px;top:64px}.node.herdr{left:435px;top:222px}
.connector{position:absolute;left:154px;top:165px;width:70px;height:1px;background:var(--orange)}.connector:after,.branch:after{content:"";position:absolute;right:-1px;top:-4px;border-left:7px solid var(--orange);border-top:4px solid transparent;border-bottom:4px solid transparent}.branch{position:absolute;left:370px;width:65px;height:1px;background:var(--muted);transform-origin:left}.branch-a{top:157px;transform:rotate(-35deg)}.branch-b{top:181px;transform:rotate(35deg)}.branch:after{border-left-color:var(--muted)}
.hero-tags,.invariant,.security-strip,.recovery-row{display:flex;gap:10px}.hero-tags span,.invariant span,.security-strip span,.recovery-row span{font:12px "Geist Mono",monospace;border:1px solid var(--border);padding:10px 14px;background:var(--surface);color:var(--muted)}
.pipeline{display:grid;grid-template-columns:1fr 42px 1fr 42px 1fr;align-items:stretch;margin-top:54px}.stage{border:1px solid var(--border);background:var(--surface);padding:26px;min-height:190px}.stage.selected{border-color:var(--orange)}.stage small,.receipt-card small,.track small,.boundary-grid small,.release-node small{font:11px "Geist Mono",monospace;color:var(--orange)}.stage strong{display:block;font-size:22px;font-weight:300;margin:28px 0 12px}.stage p{color:var(--gray);font-size:16px}.arrow,.release-arrow{display:flex;align-items:center;justify-content:center;color:var(--orange);font:22px "Geist Mono",monospace}.harness-strip{display:grid;grid-template-columns:repeat(6,1fr);margin-top:22px;border:1px solid var(--border)}.harness-strip span{text-align:center;padding:13px;border-right:1px solid var(--border);font:12px "Geist Mono",monospace;color:var(--muted)}.harness-strip span:last-child{border-right:0}
.state-board{display:grid;grid-template-columns:210px 70px 240px 70px 1fr;align-items:center;height:315px}.state{border:1px solid var(--border);background:var(--surface);padding:22px;min-height:112px}.state.active{border-color:var(--orange)}.state i{display:block;width:7px;height:7px;background:var(--gray);margin-bottom:18px}.state.active i,.state.success i{background:var(--orange)}.state b{display:block;font:17px "Geist Mono",monospace;font-weight:400}.state small{display:block;color:var(--gray);margin-top:9px}.flowline,.fanout{height:1px;background:var(--orange);position:relative}.flowline:after,.fanout:after{content:"";position:absolute;right:0;top:-4px;border-left:7px solid var(--orange);border-top:4px solid transparent;border-bottom:4px solid transparent}.outcomes{display:grid;gap:12px}.outcomes .state{min-height:76px;padding:15px}.outcomes .state i{float:left;margin:4px 14px 0 0}.invariant{margin-top:22px}
.ladder{width:720px;margin:6px 0 0 80px}.rung{display:grid;grid-template-columns:64px 1fr;align-items:center;border-left:1px solid var(--border);padding:0 0 18px 24px}.rung em{font:13px "Geist Mono",monospace;color:var(--gray);font-style:normal}.rung div{border:1px solid var(--border);background:var(--surface);padding:13px 18px}.rung.done div{border-left-color:var(--muted)}.rung.active div{border-color:var(--orange)}.rung b{font:15px "Geist Mono",monospace;font-weight:400}.rung small{color:var(--gray);margin-left:22px}.reject-panel{position:absolute;right:15px;top:92px;width:240px;border:1px solid var(--border);background:var(--surface);padding:20px}.reject-panel small{display:block;color:var(--orange);font:11px "Geist Mono",monospace;margin-bottom:18px}.reject-panel span{display:block;border-top:1px solid var(--border);padding:10px 0;color:var(--gray);font:13px "Geist Mono",monospace}
.topology-scene{display:grid;grid-template-columns:1fr 330px;gap:34px}.project-box{border:1px solid var(--border);padding:18px;background:var(--surface)}.project-label{font:12px "Geist Mono",monospace;color:var(--orange);margin-bottom:18px}.placement-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:12px}.placement{min-height:225px;border:1px solid var(--border);background:#0b0a0a;padding:18px}.placement.active{border-color:var(--orange)}.placement b{font:17px "Geist Mono",monospace;font-weight:400}.placement small{display:block;color:var(--gray);margin-top:9px}.mini-pane{height:100px;margin-top:28px;border:1px solid var(--border)}.mini-pane.one:after{content:"PANE";display:block;padding:38px 0;text-align:center;font:11px "Geist Mono",monospace;color:var(--gray)}.mini-pane.split{background:linear-gradient(90deg,transparent 49.7%,var(--border) 50%,transparent 50.3%)}.mini-branch{font:10px "Geist Mono",monospace;color:var(--gray);margin-top:60px;border-bottom:1px solid var(--orange);padding-bottom:10px}.decision-stack{display:flex;flex-direction:column;gap:12px}.decision-stack span{border:1px solid var(--border);background:var(--surface);padding:21px;font:12px "Geist Mono",monospace;color:var(--muted)}.warning{margin-top:18px;font:12px "Geist Mono",monospace;color:var(--gray)}.warning span{color:var(--orange)}
.harness-grid{display:grid;grid-template-columns:repeat(3,1fr);gap:10px}.harness-grid>div{border:1px solid var(--border);background:var(--surface);padding:17px;min-height:100px}.harness-grid>div.active{border-color:var(--orange)}.harness-grid small{color:var(--gray);font:10px "Geist Mono",monospace}.harness-grid b{display:block;font:16px "Geist Mono",monospace;font-weight:400;margin:9px 0}.harness-grid code{color:var(--muted);font:11px "Geist Mono",monospace}.trust-guard{display:grid;grid-template-columns:1fr 42px 1fr 42px 160px;align-items:stretch;margin-top:18px}.trust-guard>div{border:1px solid var(--border);padding:16px;background:#0b0a0a}.trust-guard small{color:var(--orange);font:10px "Geist Mono",monospace}.trust-guard b{display:block;font-size:15px;font-weight:300;margin-top:8px}.trust-guard .plus,.trust-guard .equals{border:0;background:transparent;display:flex;align-items:center;justify-content:center;color:var(--gray)}.trust-guard .enter{border-color:var(--orange);display:flex;align-items:center;justify-content:center;font:12px "Geist Mono",monospace}
.evidence-compare{display:grid;grid-template-columns:1fr 1fr;gap:18px}.receipt-card{border:1px solid var(--border);background:var(--surface);padding:22px;min-height:260px}.receipt-card h3{font-size:22px;font-weight:300;margin:18px 0}.receipt-card p{color:var(--gray);font-size:14px}.terminal-lines{background:#060606;border:1px solid var(--border);padding:18px}.terminal-lines i{display:block;width:78%;height:6px;background:#342f2d;margin:9px 0}.terminal-lines i.new{background:#cbc5c2}.terminal-lines i.short{width:47%;background:var(--orange)}.hash-row{display:flex;justify-content:space-between;border-bottom:1px solid var(--border);padding:13px 5px;color:var(--gray)}.hash-row code{font:13px "Geist Mono",monospace}.hash-row.changed code{color:var(--orange)}.recovery-row{margin-top:16px}.recovery-row span{flex:1;text-align:center}
.dashboard-frame{border:1px solid var(--border);background:#0a0909}.dash-top{display:grid;grid-template-columns:repeat(4,1fr);border-bottom:1px solid var(--border)}.dash-top span{padding:14px;text-align:center;border-right:1px solid var(--border);font:11px "Geist Mono",monospace}.orange-text{color:var(--orange)}.dash-columns{display:grid;grid-template-columns:240px 1fr 220px;height:280px}.dash-columns>div{padding:17px;border-right:1px solid var(--border)}.dash-columns small{font:10px "Geist Mono",monospace;color:var(--gray)}.job{height:42px;border:1px solid var(--border);background:var(--surface);margin-top:15px}.job.compact{width:78%}.job.warning-job{border-left:3px solid var(--orange)}.graph-project{border:1px solid var(--border);padding:16px;margin-top:13px;font:10px "Geist Mono",monospace}.graph-worktree{border:1px solid var(--border);margin-top:12px;padding:12px}.graph-tab{border:1px solid var(--border);margin-top:10px;padding:10px;display:grid;grid-template-columns:1fr 1fr;gap:8px}.graph-pane{border:1px solid var(--border);padding:20px 8px;text-align:center}.graph-pane.active{border-color:var(--orange)}.attention-list p{font:11px "Geist Mono",monospace;color:var(--muted);border-bottom:1px solid var(--border);padding:12px 0;margin:0}.security-strip{margin-top:14px}.security-strip span{flex:1;text-align:center;padding:8px}
.dual-track{display:grid;grid-template-columns:1fr 110px 1.1fr;gap:16px;align-items:stretch}.track{border:1px solid var(--border);background:var(--surface);padding:26px;min-height:290px}.track h3{font-size:25px;font-weight:300}.track p{color:var(--muted);font-size:17px}.track>span{color:var(--gray);font-size:13px}.delivery-track{border-color:var(--orange)}.gate{display:flex;align-items:center;justify-content:center;text-align:center;font:11px "Geist Mono",monospace;color:var(--orange)}.delivery-steps{display:grid;grid-template-columns:1fr 1fr;gap:8px;margin:22px 0}.delivery-steps i{font-style:normal;font:11px "Geist Mono",monospace;border:1px solid var(--border);padding:12px;text-align:center}.review-axis{display:flex;align-items:center;gap:17px;margin-top:18px;border:1px solid var(--border);padding:13px 18px;font:12px "Geist Mono",monospace;color:var(--gray)}.review-axis b{font-weight:400;color:#fff}
.boundary-grid{display:grid;grid-template-columns:1fr 1fr;gap:12px}.boundary-grid>div{border:1px solid var(--border);background:var(--surface);padding:20px;min-height:126px}.boundary-grid b{display:block;font:17px "Geist Mono",monospace;font-weight:400;margin:13px 0}.boundary-grid p{color:var(--gray);margin:0}.trust-note{display:grid;grid-template-columns:auto 1fr auto 2fr;align-items:center;margin-top:14px;border:1px solid var(--border)}.trust-note span,.trust-note b{padding:14px;border-right:1px solid var(--border);font:11px "Geist Mono",monospace}.trust-note span{color:var(--orange)}.trust-note b{font-weight:400}
.release-map{display:grid;grid-template-columns:1fr 32px 1fr 32px 1fr 32px 1fr 32px 1fr;align-items:stretch;margin-top:26px}.release-node{border:1px solid var(--border);background:var(--surface);padding:19px;min-height:110px}.release-node.selected{border-color:var(--orange)}.release-node b{display:block;font:14px "Geist Mono",monospace;font-weight:400;margin-top:19px}.zero-deps{display:grid;grid-template-columns:190px 190px 1fr;gap:14px;align-items:stretch;margin-top:34px}.zero-deps>div{border:1px solid var(--border);display:flex;gap:15px;align-items:center;padding:15px}.zero-deps strong,.metrics-grid strong{font-size:42px;font-weight:300;color:#fff}.zero-deps span{font:10px "Geist Mono",monospace;color:var(--gray)}.zero-deps p{border:1px solid var(--border);margin:0;padding:28px;font:12px "Geist Mono",monospace;color:var(--muted)}
.metrics-grid{display:grid;grid-template-columns:repeat(4,1fr);gap:10px}.metrics-grid>div{border:1px solid var(--border);background:var(--surface);padding:20px;display:flex;align-items:center;gap:17px}.metrics-grid span{font:10px/1.4 "Geist Mono",monospace;color:var(--gray)}.metrics-grid>div:nth-child(3){border-color:var(--orange)}.recap{display:grid;grid-template-columns:repeat(3,1fr);gap:10px;margin-top:22px}.recap>div{border-top:1px solid var(--border);padding:18px 0;display:flex;gap:15px;align-items:center}.recap em{font:11px "Geist Mono",monospace;color:var(--orange);font-style:normal}.recap b{font-size:16px;font-weight:300}.final-command{display:flex;align-items:center;justify-content:center;gap:18px;margin-top:18px}.final-command code{font:14px "Geist Mono",monospace;border:1px solid var(--border);background:var(--surface);padding:10px 16px}.final-command span{color:var(--orange)}
"""


def main() -> None:
    sections = "\n".join(scene_shell(scene, BODIES[scene["id"]]) for scene in SCENES)
    animation_lines = [
        'tl.fromTo(".progress-fill",{width:"0%"},{width:"100%",duration:TOTAL,ease:"none"},0);'
    ]
    for scene in SCENES:
        scene_id = scene["id"]
        start = float(scene["start"])
        end = float(scene["end"])
        animation_lines.append(
            f'tl.fromTo("#scene-{scene_id} .kicker,#scene-{scene_id} .scene-title",'
            f'{{opacity:0,y:18}},{{opacity:1,y:0,duration:.65,stagger:.10,ease:"power2.out"}},{start:.3f});'
        )
        animation_lines.append(
            f'tl.fromTo("#scene-{scene_id} .reveal",{{opacity:0,y:22}},'
            f'{{opacity:1,y:0,duration:.8,stagger:.12,ease:"power2.out"}},{start + .35:.3f});'
        )
        animation_lines.append(
            f'tl.to("#scene-{scene_id}",{{opacity:0,duration:.28,ease:"power1.in"}},{max(start, end - .28):.3f});'
        )
        animation_lines.append(
            f'tl.set("#scene-{scene_id}",{{opacity:0}},{end:.3f});'
        )
    animations = "\n      ".join(animation_lines)
    document = f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=1280,height=720">
  <script src="./assets/gsap.min.js"></script>
  <style>{CSS}</style>
</head>
<body>
  <div id="root" data-composition-id="main" data-start="0" data-duration="{TOTAL_DURATION:.3f}" data-width="1280" data-height="720">
    {sections}
    <div class="clip progress-shell" id="global-progress" data-start="0" data-duration="{TOTAL_DURATION:.3f}" data-track-index="10"><div class="progress-fill"></div></div>
  </div>
  <script>
    window.__timelines=window.__timelines||{{}};
    const TOTAL={TOTAL_DURATION:.3f};
    const tl=gsap.timeline({{paused:true}});
    {animations}
    window.__timelines["main"]=tl;
  </script>
</body>
</html>
"""
    document = re.sub(r"\n\s*", "", document)
    (ROOT / "index.html").write_text(document, encoding="utf-8")
    (ROOT / "hyperframes.json").write_text(
        json.dumps(
            {
                "$schema": "https://hyperframes.heygen.com/schema/hyperframes.json",
                "registry": "https://raw.githubusercontent.com/heygen-com/hyperframes/main/registry",
                "paths": {
                    "blocks": "compositions",
                    "components": "compositions/components",
                    "assets": "assets",
                },
            },
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    (ROOT / "meta.json").write_text(
        json.dumps(
            {
                "id": "herdr-orchestrator",
                "name": "Herdr Orchestrator",
                "language": "zh-CN",
                "durationSeconds": TOTAL_DURATION,
                "width": 1280,
                "height": 720,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    print(f"wrote index.html ({len(document)} bytes), {len(SCENES)} scenes")


if __name__ == "__main__":
    main()
