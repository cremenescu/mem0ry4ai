<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — relations overview: a force-directed graph of every related-to / blocked-by edge,
// plus a grouped text list below. No external libraries (offline-first): a tiny SVG force sim.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

/* ---------- POST: confirm or dismiss a suggested link (AJAX) ---------- */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    csrf_check();
    $action = $_POST['action'] ?? '';
    $a = trim($_POST['a'] ?? ''); $b = trim($_POST['b'] ?? '');
    $ok = false;
    if ($action === 'link')        $ok = link_records($a, $b);
    elseif ($action === 'dismiss') $ok = dismiss_pair($a, $b);
    header('Content-Type: application/json; charset=utf-8');
    echo json_encode(['ok' => $ok]);
    exit;
}

$edges = all_links();
$suggestions = suggested_links();

// graph data: unique node per record that participates in an edge
$nodeMap = [];
foreach ($edges as $e) {
    foreach (['a', 'b'] as $k) {
        $r = $e[$k];
        if (!isset($nodeMap[$r['id']])) {
            $nodeMap[$r['id']] = [
                'id'    => $r['id'],
                'label' => mb_substr(rec_summary($r), 0, 34),
                'type'  => $r['meta']['type'] ?? '?',
                'scope' => $r['meta']['scope'] === 'global' ? 'global' : scope_label($r['meta']['scope'] ?? 'global'),
                'url'   => 'memories.php?id=' . $r['id'],
            ];
        }
    }
}
$gnodes = array_values($nodeMap);
$gedges = array_map(fn($e) => ['s' => $e['a']['id'], 't' => $e['b']['id'], 'kind' => $e['kind']], $edges);

// group the text list by scope of the "a" record
$groups = [];
foreach ($edges as $e) { $groups[$e['a']['meta']['scope'] ?? 'global'][] = $e; }
uksort($groups, fn($x, $y) => $x === 'global' ? -1 : ($y === 'global' ? 1 : strcmp($x, $y)));

function endpoint_html(array $r): string {
    $sc = $r['meta']['scope'] ?? '';
    $scl = $sc === 'global' ? '' : ' <span class="lk-scope">' . h(scope_label($sc)) . '</span>';
    return type_badge($r['meta']['type'] ?? '?')
        . ' <a class="lk-sum" href="memories.php?id=' . h($r['id']) . '">' . h(mb_substr(rec_summary($r), 0, 90)) . '</a>'
        . $scl;
}
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= t('Links') ?> — mem0ry4ai</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>">
<link rel="stylesheet" href="<?= h(asset('assets/style.css')) ?>">
</head>
<body>
<?= render_topbar('links') ?>

<main>
<div class="layout">
<div class="content">
  <div class="crumb"><a href="index.php"><?= t('Dashboard') ?></a> / <?= t('Links') ?></div>
  <h2><?= t('Links') ?> <span class="count"><?= count($edges) ?></span></h2>

  <?php if ($suggestions): $sb = records_by_id(); ?>
  <div class="suggest">
    <h3><?= t('Suggested links') ?> <span class="count"><?= count($suggestions) ?></span>
      <small><?= ui_lang() === 'ro' ? 'semantic — tu confirmi' : 'semantic — you confirm' ?></small></h3>
    <ul id="suggest-list">
      <?php
      $sg_end = function (array $r) {
          $sc = $r['meta']['scope'] ?? '';
          $scl = $sc !== 'global' ? '<span class="sg-scope">' . h(scope_label($sc)) . '</span> ' : '';
          ob_start(); ?>
        <div class="sg-end">
          <div class="sg-top"><?= type_badge($r['meta']['type'] ?? '?') ?>
            <a class="sg-sum" href="memories.php?id=<?= h($r['id']) ?>"><?= $scl . h(rec_summary($r)) ?></a></div>
          <div class="sg-full"><?= render_body($r['body']) ?><?= rec_extras_html($r) ?></div>
        </div>
      <?php return ob_get_clean(); };
      foreach ($suggestions as $s): $A = $sb[$s['a']] ?? null; $B = $sb[$s['b']] ?? null; if (!$A || !$B) continue; ?>
      <li data-a="<?= h($s['a']) ?>" data-b="<?= h($s['b']) ?>">
        <div class="sg-head"><span class="sg-pct" title="semantic similarity"><?= round($s['sim'] * 100) ?>%</span>
          <span class="sg-act"><button type="button" class="btn btn-primary sg-link"><?= t('Link') ?></button>
            <button type="button" class="btn btn-ghost sg-dismiss"><?= t('Dismiss') ?></button></span></div>
        <div class="sg-pair"><?= $sg_end($A) ?><div class="sg-mid">&harr;</div><?= $sg_end($B) ?></div>
        <button type="button" class="sg-more"><span class="sg-arrow">&#9662;</span> <?= t('full content') ?></button>
      </li>
      <?php endforeach; ?>
    </ul>
  </div>
  <?php endif; ?>

  <?php if (!$edges): ?>
    <div class="empty"><?= ui_lang() === 'ro'
        ? 'Nicio legatura inca. Leaga memorii inrudite cu <code>mem.py link &lt;id&gt; &lt;alt&gt;</code> sau <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.'
        : 'No links yet. Connect related memories with <code>mem.py link &lt;id&gt; &lt;other&gt;</code> or <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.' ?></div>
  <?php else: ?>

  <div class="graphwrap">
    <svg id="graph" viewBox="0 0 920 540" preserveAspectRatio="xMidYMid meet" role="img" aria-label="relations graph">
      <defs>
        <marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="7" markerHeight="7" orient="auto-start-reverse">
          <path d="M0,0 L10,5 L0,10 z" fill="#c98a3a"></path>
        </marker>
      </defs>
      <g id="g-edges"></g>
      <g id="g-nodes"></g>
    </svg>
    <div class="graph-legend" id="graph-legend"></div>
  </div>

  <details class="lk-listwrap">
    <summary><?= ui_lang() === 'ro' ? 'Lista detaliata' : 'Detailed list' ?> (<?= count($edges) ?>)</summary>
    <?php foreach ($groups as $sc => $es): ?>
    <div class="lk-group">
      <h3><?= $sc === 'global' ? t('Global') : t('Project:') . ' ' . h(scope_label($sc)) ?>
        <span class="count"><?= count($es) ?></span></h3>
      <ul class="links">
        <?php foreach ($es as $e): ?>
        <li class="lk lk-<?= $e['kind'] ?>">
          <?= endpoint_html($e['a']) ?>
          <span class="lk-rel"><?= $e['kind'] === 'blocked' ? '⟂ ' . t('blocked by') : '↔' ?></span>
          <?= endpoint_html($e['b']) ?>
        </li>
        <?php endforeach; ?>
      </ul>
    </div>
    <?php endforeach; ?>
  </details>
  <?php endif; ?>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Links') ?></h3>
  <p><?= ui_lang() === 'ro'
        ? 'Graful tuturor legaturilor dintre memorii. Linie plina <b>↔</b> = inrudite (related-to); sageata portocalie <b>⟂</b> = un todo blocat de altceva. Culoarea nodului = tipul memoriei; marimea = cate legaturi are.'
        : 'The graph of every link between memories. A solid line <b>↔</b> = related (related-to); an orange arrow <b>⟂</b> = a todo blocked by something. Node color = memory type; size = how many links it has.' ?></p>
  <h4><?= ui_lang() === 'ro' ? 'Interactiune' : 'Interaction' ?></h4>
  <p><?= ui_lang() === 'ro'
        ? 'Trage un nod ca sa rearanjezi. Click pe un nod → memoria lui. Treci cu mouse-ul pentru a evidentia vecinii.'
        : 'Drag a node to rearrange. Click a node → its memory. Hover to highlight its neighbours.' ?></p>
</aside>
</div><!-- /layout -->
</main>

<?php if ($edges): ?>
<script>
var GRAPH = { nodes: <?= json_encode($gnodes, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?>, edges: <?= json_encode($gedges, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES) ?> };

(function(){
  var W = 920, H = 540, CX = W/2, CY = H/2;
  var COLOR = { gotcha:'#c77f0a', fact:'#006fff', decision:'#7c3aed', command:'#475569',
                preference:'#1f9d4d', todo:'#e8590c', status:'#0c8599' };
  var SVG = 'http://www.w3.org/2000/svg';
  var svg = document.getElementById('graph');
  var gE = document.getElementById('g-edges'), gN = document.getElementById('g-nodes');

  var nodes = GRAPH.nodes, edges = GRAPH.edges;
  var byId = {}; nodes.forEach(function(n){ byId[n.id] = n; });
  // degree -> radius; init positions on a circle
  nodes.forEach(function(n){ n.deg = 0; });
  edges.forEach(function(e){ if (byId[e.s]) byId[e.s].deg++; if (byId[e.t]) byId[e.t].deg++; });
  nodes.forEach(function(n, i){
    var a = (i / nodes.length) * Math.PI * 2;
    n.x = CX + Math.cos(a) * 150 + (i%3-1)*8;
    n.y = CY + Math.sin(a) * 150 + (i%2)*8;
    n.vx = 0; n.vy = 0;
    n.r = Math.min(20, 8 + n.deg * 1.6);
  });
  var adj = {}; nodes.forEach(function(n){ adj[n.id] = {}; });
  edges.forEach(function(e){ if (adj[e.s]&&adj[e.t]){ adj[e.s][e.t]=1; adj[e.t][e.s]=1; } });

  // build SVG elements once
  var eEls = edges.map(function(e){
    var ln = document.createElementNS(SVG, 'line');
    ln.setAttribute('class', 'gedge ' + (e.kind === 'blocked' ? 'gedge-blocked' : 'gedge-related'));
    if (e.kind === 'blocked') ln.setAttribute('marker-end', 'url(#arrow)');
    e._el = ln; gE.appendChild(ln); return ln;
  });
  var nEls = nodes.map(function(n){
    var g = document.createElementNS(SVG, 'g');
    g.setAttribute('class', 'gnode'); g.setAttribute('data-id', n.id); g.style.cursor = 'pointer';
    var c = document.createElementNS(SVG, 'circle');
    c.setAttribute('r', n.r); c.setAttribute('fill', COLOR[n.type] || '#888');
    c.setAttribute('stroke', '#fff'); c.setAttribute('stroke-width', '2');
    var ti = document.createElementNS(SVG, 'title'); ti.textContent = '[' + n.type + '] ' + n.label;
    c.appendChild(ti);
    var tx = document.createElementNS(SVG, 'text');
    tx.setAttribute('class', 'glabel'); tx.setAttribute('text-anchor', 'middle');
    tx.setAttribute('dy', n.r + 11); tx.textContent = n.label.length > 22 ? n.label.slice(0,21) + '…' : n.label;
    g.appendChild(c); g.appendChild(tx); n._el = g; n._circle = c; gN.appendChild(g); return g;
  });

  // legend
  var lg = document.getElementById('graph-legend');
  var used = {}; nodes.forEach(function(n){ used[n.type] = 1; });
  Object.keys(used).forEach(function(t){
    var s = document.createElement('span'); s.className = 'lgitem';
    s.innerHTML = '<i style="background:' + (COLOR[t]||'#888') + '"></i>' + t;
    lg.appendChild(s);
  });

  // force simulation
  var REP = 5200, LEN = 118, SPRING = 0.035, GRAV = 0.018, DAMP = 0.9, alpha = 1, drag = null;
  function physics(a){
    var i, j, n, dx, dy, d2, d, f, ux, uy;
    for (i = 0; i < nodes.length; i++) { nodes[i].fx = 0; nodes[i].fy = 0; }
    for (i = 0; i < nodes.length; i++) {
      for (j = i + 1; j < nodes.length; j++) {
        dx = nodes[i].x - nodes[j].x; dy = nodes[i].y - nodes[j].y;
        d2 = dx*dx + dy*dy || 0.01; d = Math.sqrt(d2); f = REP / d2;
        ux = dx/d; uy = dy/d;
        nodes[i].fx += ux*f; nodes[i].fy += uy*f;
        nodes[j].fx -= ux*f; nodes[j].fy -= uy*f;
      }
    }
    edges.forEach(function(e){
      var na = byId[e.s], nb = byId[e.t]; if (!na || !nb) return;
      var ex = nb.x - na.x, ey = nb.y - na.y, ed = Math.sqrt(ex*ex + ey*ey) || 0.01;
      var ef = (ed - LEN) * SPRING, eux = ex/ed, euy = ey/ed;
      na.fx += eux*ef; na.fy += euy*ef; nb.fx -= eux*ef; nb.fy -= euy*ef;
    });
    for (i = 0; i < nodes.length; i++) {
      n = nodes[i];
      n.fx += (CX - n.x) * GRAV; n.fy += (CY - n.y) * GRAV;
      if (n === drag) continue;
      n.vx = (n.vx + n.fx * a) * DAMP; n.vy = (n.vy + n.fy * a) * DAMP;
      n.x += n.vx; n.y += n.vy;
      n.x = Math.max(n.r+4, Math.min(W-n.r-4, n.x));
      n.y = Math.max(n.r+4, Math.min(H-n.r-30, n.y));
    }
  }
  function tick(){
    alpha = drag ? 0.5 : Math.max(0, alpha * 0.985);
    physics(alpha);
    render();
    requestAnimationFrame(tick);
  }
  function render(){
    edges.forEach(function(e){
      var a = byId[e.s], b = byId[e.t]; if (!a || !b) return;
      e._el.setAttribute('x1', a.x); e._el.setAttribute('y1', a.y);
      e._el.setAttribute('x2', b.x); e._el.setAttribute('y2', b.y);
    });
    nodes.forEach(function(n){ n._el.setAttribute('transform', 'translate(' + n.x + ',' + n.y + ')'); });
  }

  // pointer: drag vs click
  var startPt = null, moved = false;
  function toSvg(ev){
    var pt = svg.createSVGPoint(); pt.x = ev.clientX; pt.y = ev.clientY;
    var p = pt.matrixTransform(svg.getScreenCTM().inverse()); return p;
  }
  gN.addEventListener('pointerdown', function(ev){
    var g = ev.target.closest('.gnode'); if (!g) return;
    drag = byId[g.getAttribute('data-id')]; startPt = toSvg(ev); moved = false;
    g.setPointerCapture(ev.pointerId);
  });
  gN.addEventListener('pointermove', function(ev){
    if (!drag) return; var p = toSvg(ev);
    if (Math.abs(p.x - startPt.x) + Math.abs(p.y - startPt.y) > 3) moved = true;
    drag.x = p.x; drag.y = p.y; drag.vx = 0; drag.vy = 0;
  });
  gN.addEventListener('pointerup', function(ev){
    var g = ev.target.closest('.gnode');
    if (drag && !moved && g) { window.location = byId[g.getAttribute('data-id')].url; }
    drag = null;
  });

  // hover highlight
  gN.addEventListener('mouseover', function(ev){
    var g = ev.target.closest('.gnode'); if (!g) return;
    var id = g.getAttribute('data-id');
    nodes.forEach(function(n){ n._el.classList.toggle('dim', n.id !== id && !adj[id][n.id]); });
    edges.forEach(function(e){ e._el.classList.toggle('hot', e.s === id || e.t === id); e._el.classList.toggle('dim', !(e.s === id || e.t === id)); });
  });
  gN.addEventListener('mouseout', function(){
    nodes.forEach(function(n){ n._el.classList.remove('dim'); });
    edges.forEach(function(e){ e._el.classList.remove('hot'); e._el.classList.remove('dim'); });
  });

  // warmup: pre-settle the layout synchronously so the first paint (and screenshots) show a
  // laid-out graph instead of a blob — even before any animation frame runs.
  (function(){ var a = 1; for (var k = 0; k < 280; k++) { physics(a); a *= 0.985; } })();
  alpha = 0.06;
  render(); requestAnimationFrame(tick);
})();
</script>
<?php endif; ?>
<?php if ($suggestions): ?>
<script>
(function(){
  var CSRF = <?= json_encode(csrf_token()) ?>;
  document.getElementById('suggest-list').addEventListener('click', function(e){
    var li = e.target.closest('li[data-a]'); if (!li) return;
    if (e.target.closest('.sg-more')) {           // expand/collapse full content
      li.classList.toggle('expanded');
      return;
    }
    var link = e.target.classList.contains('sg-link');
    var dismiss = e.target.classList.contains('sg-dismiss');
    if (!link && !dismiss) return;
    e.target.disabled = true;
    var fd = new FormData(); fd.set('csrf', CSRF);
    fd.set('action', link ? 'link' : 'dismiss'); fd.set('a', li.dataset.a); fd.set('b', li.dataset.b);
    fetch('links.php', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd })
      .then(function(r){ return r.json(); })
      .then(function(j){
        if (!j.ok) { e.target.disabled = false; return; }
        if (link) { location.reload(); return; }   // new edge -> refresh graph
        li.remove();
        var c = document.querySelector('.suggest .count'); if (c) c.textContent = document.querySelectorAll('#suggest-list li').length;
      })
      .catch(function(){ e.target.disabled = false; });
  });
})();
</script>
<?php endif; ?>
</body>
</html>
