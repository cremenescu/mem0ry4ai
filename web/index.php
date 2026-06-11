<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai web UI — dashboard: system status + grouped list + filters + FTS + CRUD (AJAX).
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

// Row renderer (main + body row) — used for page render AND in AJAX JSON responses (single source).
function render_row(array $r): string {
    $m = $r['meta'];
    $st = $m['status'] ?? 'active';
    $sum = rec_summary($r);
    $type = $m['type'] ?? '?';
    $scope = $m['scope'] ?? 'global';
    $hay = mb_strtolower($r['title'] . ' ' . $r['body']);
    ob_start(); ?>
<tr class="<?= $st === 'superseded' ? 'row-superseded' : '' ?>" data-id="<?= h($r['id']) ?>" data-status="<?= h($st) ?>"
    data-hay="<?= h($hay) ?>" data-type="<?= h($type) ?>" data-scope="<?= h($scope) ?>"
    data-summary="<?= h($sum) ?>" data-confidence="<?= h($m['confidence'] ?? '1.0') ?>" data-body="<?= h($r['body']) ?>"
    data-created="<?= h($m['created'] ?? '') ?>">
  <td class="sel"><input type="checkbox" class="rowsel"></td>
  <td><?= type_badge($type) ?></td>
  <td><a class="scope-tag" href="<?= $scope === 'global' ? 'index.php?scope=global' : 'project.php?slug=' . h(scope_label($scope)) ?>"><?= h(scope_label($scope)) ?></a></td>
  <td class="summary" onclick="toggleBody(this)">
    <?php if (($m['priority'] ?? '') === 'critical'): ?><span class="badge t-status" title="<?= t('critical rule: always injected, first, at SessionStart') ?>">critical</span> <?php endif; ?><b><?= h($sum) ?></b>
    <?php if ($st === 'superseded'): ?><span class="status-superseded">· superseded<?= !empty($m['superseded-by']) ? ' → <a href="index.php?id=' . h($m['superseded-by']) . '">' . h($m['superseded-by']) . '</a>' : '' ?></span><?php endif; ?>
    <div class="meta"><a href="index.php?id=<?= h($r['id']) ?>"><?= h($r['id']) ?></a> · conf <?= h($m['confidence'] ?? '?') ?> · <?= h($m['source'] ?? '') ?></div>
  </td>
  <td class="meta" style="white-space:nowrap"><?= h($m['created'] ?? '') ?></td>
  <td style="text-align:right">
    <div class="actwrap">
      <button type="button" class="act-toggle" onclick="toggleAct(this)">Actions ▾</button>
      <div class="actmenu">
        <button type="button" class="act-edit"><?= t('Edit') ?></button>
        <button type="button" class="act-rescope"><?= t('Re-scope') ?></button>
        <button type="button" class="act-supersede"><?= t('Supersede') ?></button>
        <a href="index.php?id=<?= h($r['id']) ?>"><?= t('View chain') ?></a>
        <button type="button" class="act-delete danger"><?= t('Delete') ?></button>
      </div>
    </div>
  </td>
</tr>
<tr class="bodyrow" data-bodyfor="<?= h($r['id']) ?>" style="display:none"><td colspan="6"><?= render_body($r['body']) ?></td></tr>
<?php
    return ob_get_clean();
}

/* ---------- POST actions (AJAX JSON or PRG fallback) ---------- */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    csrf_check();
    $action = $_POST['action'] ?? '';
    $id = trim($_POST['id'] ?? '');
    $ok = true; $err = null;
    try {
        if ($action === 'add') {
            $id = add_record(
                trim($_POST['type'] ?? ''), trim($_POST['scope'] ?? ''),
                trim($_POST['summary'] ?? ''), (string)($_POST['body'] ?? ''),
                trim($_POST['confidence'] ?? '1.0'), 'web'
            );
        } elseif ($action === 'edit') {
            $ok = update_record($id, [
                'type' => trim($_POST['type'] ?? ''), 'scope' => trim($_POST['scope'] ?? ''),
                'summary' => trim($_POST['summary'] ?? ''), 'body' => (string)($_POST['body'] ?? ''),
                'confidence' => trim($_POST['confidence'] ?? '1.0'),
            ]);
            // editing with a changed scope = re-scope (moves the record between files)
            if ($ok) {
                $rec = find_record($id);
                if ($rec && ($rec['meta']['scope'] ?? '') !== trim($_POST['scope'] ?? '')) {
                    $ok = rescope_record($id, trim($_POST['scope'] ?? ''));
                }
            }
        } elseif ($action === 'rescope') {
            $ok = rescope_record($id, trim($_POST['scope'] ?? ''));
        } elseif ($action === 'supersede') {
            $ok = supersede_record($id, trim($_POST['by'] ?? ''));
        } elseif ($action === 'delete') {
            $ok = delete_record($id);
        } else {
            $ok = false; $err = 'unknown action';
        }
    } catch (Throwable $e) {
        $ok = false; $err = $e->getMessage();
    }

    $xhr = (($_SERVER['HTTP_X_REQUESTED_WITH'] ?? '') === 'XMLHttpRequest');
    if ($xhr) {
        header('Content-Type: application/json; charset=utf-8');
        if ($err !== null) { echo json_encode(['ok' => false, 'error' => $err]); exit; }
        if (!$ok) { echo json_encode(['ok' => false, 'error' => t('Operation failed')]); exit; }
        $resp = ['ok' => true, 'action' => $action, 'id' => $id];
        if (in_array($action, ['add', 'edit', 'supersede', 'rescope'], true)) {
            $rec = find_record($id);
            $resp['html'] = $rec ? render_row($rec) : '';
            $resp['status'] = $rec['meta']['status'] ?? 'active';
            $resp['scope'] = $rec['meta']['scope'] ?? '';
        }
        echo json_encode($resp); exit;
    }
    flash($ok ? 'OK' : ($err ?? t('Operation failed')), $ok ? 'success' : 'error');
    redirect('index.php');
}

/* ---------- filters (GET) ---------- */
$q      = trim($_GET['q'] ?? '');
$fScope = trim($_GET['scope'] ?? '');
$fType  = trim($_GET['type'] ?? '');
$fStat  = trim($_GET['status'] ?? 'active');   // active | superseded | all
$fId    = trim($_GET['id'] ?? '');             // single record + its supersede chain

$records = all_records();
$chainIds = [];
if ($fId !== '') {
    $chainIds = supersede_chain($fId);
    $fStat = 'all';
}

// FTS ranked when there is a query; substring fallback
$ftsIds = null;
if ($q !== '' && $fId === '') {
    $ftsIds = fts_query($q);
}

$rows = array_filter($records, function ($r) use ($q, $fScope, $fType, $fStat, $fId, $chainIds, $ftsIds) {
    $m = $r['meta'];
    if ($fId !== '') return in_array($r['id'], $chainIds, true);
    if ($fScope !== '' && ($m['scope'] ?? '') !== $fScope) return false;
    if ($fType !== '' && ($m['type'] ?? '') !== $fType) return false;
    if ($fStat !== 'all' && ($m['status'] ?? 'active') !== $fStat) return false;
    if ($q !== '') {
        if (is_array($ftsIds)) return in_array($r['id'], $ftsIds, true);
        $blob = mb_strtolower($r['title'] . ' ' . $r['body'] . ' ' . ($m['scope'] ?? ''));
        if (!str_contains($blob, mb_strtolower($q))) return false;
    }
    return true;
});

// ordering: FTS = by relevance; chain = chain order; otherwise newest-first
if ($q !== '' && is_array($ftsIds)) {
    $pos = array_flip($ftsIds);
    usort($rows, fn($a, $b) => ($pos[$a['id']] ?? 9999) <=> ($pos[$b['id']] ?? 9999));
} elseif ($fId !== '') {
    $pos = array_flip($chainIds);
    usort($rows, fn($a, $b) => ($pos[$a['id']] ?? 9999) <=> ($pos[$b['id']] ?? 9999));
} else {
    usort($rows, fn($a, $b) => strcmp($b['meta']['created'] ?? '', $a['meta']['created'] ?? ''));
}

// group by scope when there is no scope filter/search/chain view
$grouped = ($fScope === '' && $q === '' && $fId === '');
$groups = [];
if ($grouped) {
    foreach ($rows as $r) {
        $sc = $r['meta']['scope'] ?? 'global';
        $groups[$sc][] = $r;
    }
    uksort($groups, function ($a, $b) {
        if ($a === 'global') return -1;
        if ($b === 'global') return 1;
        return strcmp($a, $b);
    });
}

$scopes = known_scopes();
$stats  = store_stats();
$health = health_checks();
$flash  = take_flash();
$qs = function (array $over) use ($q, $fScope, $fType, $fStat) {
    $p = array_filter(['q' => $q, 'scope' => $fScope, 'type' => $fType, 'status' => $fStat], fn($v) => $v !== '');
    return 'index.php?' . http_build_query(array_merge($p, $over));
};
$nq = count(queue_pending());
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>mem0ry4ai</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>">
<link rel="stylesheet" href="<?= h(asset('assets/style.css')) ?>">
</head>
<body>
<div class="topbar">
  <a class="brand" href="index.php">mem0ry4ai <small><?= t('local memory') ?></small></a>
  <div class="right">
    <a href="git.php"><?= t('Git history') ?></a>
    <a href="inject.php"><?= t('What Claude sees') ?></a>
    <?php if ($nq > 0): ?><a class="review-tag" href="queue.php"><?= $nq ?> <?= t('to review') ?></a><?php endif; ?>
    <?= lang_switch_html() ?>
  </div>
</div>

<main>
<div class="layout">
<div class="content">

  <?php if ($flash): ?><div class="flash flash-<?= h($flash[1]) ?>"><?= h($flash[0]) ?></div><?php endif; ?>

  <!-- System status dashboard -->
  <details class="dashwrap" open>
    <summary><?= t('System status') ?></summary>
    <div class="dash" id="dash-cards"><?= render_dash_cards($stats) ?></div>
    <div class="dash-row">
      <div class="dash-col">
        <h4><?= t('Health') ?></h4>
        <ul class="health">
          <?php foreach ($health as [$lbl, $ok, $det]): ?>
          <li><span class="dot <?= $ok === null ? 'dot-unk' : ($ok ? 'dot-ok' : 'dot-err') ?>"></span> <?= h($lbl) ?> <span class="hd"><?= h($det) ?></span></li>
          <?php endforeach; ?>
        </ul>
        <div class="newbanner" id="newbanner" style="display:none">
          <?= t('The store changed in the meantime.') ?><br>
          <button class="btn btn-primary" onclick="location.reload()"><?= t('Refresh list') ?></button>
          <button class="btn btn-ghost" onclick="this.parentNode.style.display='none'"><?= t('later') ?></button>
        </div>
      </div>
      <div class="dash-col">
        <h4><?= t('Recent activity') ?></h4>
        <ul class="recent" id="recent-list"><?= render_recent_list($stats) ?></ul>
      </div>
    </div>
  </details>

  <h2><?= t('Memories') ?> <span class="count"><?= count($rows) ?> <?= t('of') ?> <?= $stats['total'] ?></span>
    <?php if ($q !== '' && is_array($ftsIds)): ?><span class="count">· <?= t('ranked search (FTS)') ?></span><?php endif; ?>
  </h2>

  <?php if ($fId !== '' && count($chainIds) > 1): ?>
  <div class="chain"><b><?= t('Supersede chain:') ?></b>
    <?php foreach ($chainIds as $i => $cid): ?><?= $i ? '<span class="arrow">→</span>' : '' ?><a href="index.php?id=<?= h($cid) ?>"><?= h($cid) ?></a><?php endforeach; ?>
    <a class="btn btn-ghost" style="margin-left:12px" href="index.php"><?= t('close') ?></a>
  </div>
  <?php elseif ($fId !== ''): ?>
  <div class="chain"><code><?= h($fId) ?></code> <?= t('(no chain)') ?>. <a href="index.php"><?= t('back') ?></a></div>
  <?php endif; ?>

  <a href="#" class="add-link" id="open-add"><?= t('+ Add memory') ?></a>

  <datalist id="scopes"><?php foreach ($scopes as $s): ?><option value="<?= h($s) ?>"><?php endforeach; ?></datalist>

  <!-- Filters toolbar -->
  <div class="toolbar">
    <form class="filters" method="get">
      <input type="search" name="q" value="<?= h($q) ?>" placeholder="<?= t('search (FTS ranked)...') ?>" id="live">
      <select name="type" onchange="this.form.submit()">
        <option value=""><?= t('all types') ?></option>
        <?php foreach (TYPES as $ty): ?><option<?= $fType === $ty ? ' selected' : '' ?>><?= h($ty) ?></option><?php endforeach; ?>
      </select>
      <select name="scope" onchange="this.form.submit()">
        <option value=""><?= t('all scopes') ?></option>
        <?php foreach ($scopes as $s): ?><option<?= $fScope === $s ? ' selected' : '' ?> value="<?= h($s) ?>"><?= h(scope_label($s)) ?></option><?php endforeach; ?>
      </select>
      <input type="hidden" name="status" value="<?= h($fStat) ?>">
      <button class="btn" type="submit"><?= t('Search') ?></button>
    </form>
    <div class="pills">
      <a class="<?= $fStat === 'active' ? 'active' : '' ?>" href="<?= h($qs(['status' => 'active'])) ?>"><?= t('active') ?></a>
      <a class="<?= $fStat === 'superseded' ? 'active' : '' ?>" href="<?= h($qs(['status' => 'superseded'])) ?>"><?= t('superseded') ?></a>
      <a class="<?= $fStat === 'all' ? 'active' : '' ?>" href="<?= h($qs(['status' => 'all'])) ?>"><?= t('all') ?></a>
    </div>
  </div>

  <!-- Table -->
  <div id="table-wrap">
  <?php if (!$rows): ?>
    <div class="empty"><?= t('No memories match the current filter.') ?></div>
  <?php else: ?>
  <table class="mem">
    <thead><tr>
      <th class="sel"><input type="checkbox" id="selall" title="<?= t('select all') ?>"></th>
      <th class="sortable" data-sort="type"><?= t('Type') ?><span class="arrow"></span></th>
      <th class="sortable" data-sort="scope"><?= t('Scope') ?><span class="arrow"></span></th>
      <th class="sortable" data-sort="summary"><?= t('Memory') ?><span class="arrow"></span></th>
      <th class="sortable" data-sort="created"><?= t('Added') ?><span class="arrow"></span></th>
      <th></th>
    </tr></thead>
    <?php if ($grouped): foreach ($groups as $sc => $grs): ?>
    <tbody class="grp" data-scope="<?= h($sc) ?>">
      <tr class="group-head"><td colspan="6"><?= $sc === 'global' ? t('Global') : t('Project:') . ' ' . h(scope_label($sc)) ?>
        <span class="gcount">· <?= count($grs) ?> <?= t('memories') ?></span>
        <?php if ($sc !== 'global'): ?><a class="gcount" style="float:right" href="project.php?slug=<?= h(scope_label($sc)) ?>" onclick="event.stopPropagation()"><?= t('project page →') ?></a><?php endif; ?>
      </td></tr>
      <?php foreach ($grs as $r) echo render_row($r); ?>
    </tbody>
    <?php endforeach; else: ?>
    <tbody>
      <?php foreach ($rows as $r) echo render_row($r); ?>
    </tbody>
    <?php endif; ?>
  </table>
  <?php endif; ?>
  </div>

  <p class="foot"><?= t('Source of truth:') ?> <code>store/*.md</code> <?= t('(markdown + git). CLI:') ?> <code>./mem.py</code>.</p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Quick guide') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('help.intro') : 'A memory is one short fact you want to keep between sessions. The source of truth is markdown + git; the web UI and <code>mem.py</code> are two windows onto the same store.' ?></p>

  <h4><?= t('Types') ?></h4>
  <dl>
    <dt><span class="badge t-gotcha">gotcha</span></dt><dd><?= ui_lang() === 'ro' ? t('help.gotcha') : 'Trap / lesson: "X breaks because of Y, do Z".' ?></dd>
    <dt><span class="badge t-fact">fact</span></dt><dd><?= ui_lang() === 'ro' ? t('help.fact') : 'Stable fact: IP, port, path, deploy target.' ?></dd>
    <dt><span class="badge t-decision">decision</span></dt><dd><?= ui_lang() === 'ro' ? t('help.decision') : 'Architecture decision + the why.' ?></dd>
    <dt><span class="badge t-command">command</span></dt><dd><?= ui_lang() === 'ro' ? t('help.command') : 'A useful command.' ?></dd>
    <dt><span class="badge t-preference">preference</span></dt><dd><?= ui_lang() === 'ro' ? t('help.preference') : 'One of your preferences.' ?></dd>
    <dt><span class="badge t-todo">todo</span></dt><dd><?= ui_lang() === 'ro' ? t('help.todo') : 'What remains to be done. Done = superseded.' ?></dd>
    <dt><span class="badge t-status">status</span></dt><dd><?= ui_lang() === 'ro' ? t('help.status') : 'Where the project stands / where you left off.' ?></dd>
  </dl>

  <h4><?= t('Navigation') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('help.nav') : 'Click a <b>scope</b> → the project page (status + todos on top). Click an <b>id</b> → the supersede chain. Click a row → its body. Group header → collapse.' ?></p>

  <h4><?= t('Bulk') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('help.bulk') : 'Tick rows → bottom bar: Supersede / Re-scope / Delete all at once.' ?></p>

  <h4><?= t('Search') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('help.search') : 'Field + Enter = FTS ranked (same index as <code>mem.py search</code>). Typing also live-filters what is on screen.' ?></p>
</aside>
</div><!-- /layout -->
</main>

<!-- bulk bar -->
<div class="bulkbar" id="bulkbar">
  <span class="bn" id="bulkcount">0</span> <?= t('selected') ?>
  <button class="btn" id="bulk-rescope"><?= t('Re-scope') ?></button>
  <button class="btn" id="bulk-supersede"><?= t('Supersede') ?></button>
  <button class="btn btn-danger" id="bulk-delete"><?= t('Delete') ?></button>
  <button class="btn btn-ghost" id="bulk-clear"><?= t('Clear selection') ?></button>
</div>

<!-- add/edit modal (AJAX) -->
<div class="modal-overlay" id="modal" aria-hidden="true">
  <div class="modal">
    <div class="modal-head">
      <h3 id="modal-title"><?= t('Add memory') ?></h3>
      <button type="button" class="modal-close" data-close aria-label="close">&times;</button>
    </div>
    <form id="mem-form">
      <input type="hidden" name="action" id="m-action" value="add">
      <input type="hidden" name="id" id="m-id" value="">
      <div class="form-row">
        <label><?= t('Type') ?>
          <select name="type" id="m-type"><?php foreach (TYPES as $ty): ?><option><?= h($ty) ?></option><?php endforeach; ?></select>
        </label>
        <label><?= t('Scope') ?> <small><?= t('global or project:slug') ?></small>
          <input type="text" name="scope" id="m-scope" value="global" list="scopes">
        </label>
        <label>Confidence
          <input type="text" name="confidence" id="m-confidence" value="1.0">
        </label>
      </div>
      <label>Summary <input type="text" name="summary" id="m-summary" placeholder="<?= t('one-line summary') ?>" required></label>
      <label>Body <textarea name="body" id="m-body" placeholder="<?= t('memory details (simple markdown, inline `code`)') ?>" required></textarea></label>
      <div class="modal-err" id="m-err" style="display:none"></div>
      <div class="form-actions">
        <button class="btn btn-primary" id="m-submit" type="submit"><?= t('Save') ?></button>
        <button type="button" class="btn btn-ghost" data-close><?= t('Cancel') ?></button>
      </div>
    </form>
  </div>
</div>

<script>
var CSRF = <?= json_encode(csrf_token()) ?>;
var STATUS_FILTER = <?= json_encode($fStat) ?>;
var TXT = {
  supersedeQ: <?= json_encode(t('Mark as superseded?')) ?>,
  deleteQ: <?= json_encode(t('Delete permanently? (it stays in git history)')) ?>,
  rescopeQ: <?= json_encode(t('New scope (global or project:slug):')) ?>,
  failed: <?= json_encode(t('Operation failed')) ?>,
  network: <?= json_encode(t('Network error')) ?>,
  addTitle: <?= json_encode(t('Add memory')) ?>,
  editTitle: <?= json_encode(t('Edit')) ?>
};

function toggleBody(cell){ var r = cell.closest('tr').nextElementSibling;
  if (r && r.classList.contains('bodyrow')) r.style.display = (r.style.display === 'none') ? '' : 'none'; }
function toggleAct(btn){ var w = btn.closest('.actwrap'); var open = w.classList.contains('open');
  closeActMenus(); if (!open) w.classList.add('open'); }
function closeActMenus(){ document.querySelectorAll('.actwrap.open').forEach(function(x){ x.classList.remove('open'); }); }
document.addEventListener('click', function(e){ if (!e.target.closest('.actwrap')) closeActMenus(); });

// group collapse
document.querySelectorAll('tr.group-head').forEach(function(gh){
  gh.addEventListener('click', function(){
    gh.classList.toggle('closed');
    var hide = gh.classList.contains('closed');
    var tr = gh.nextElementSibling;
    while (tr) { tr.style.display = hide ? 'none' : (tr.classList.contains('bodyrow') ? 'none' : ''); tr = tr.nextElementSibling; }
  });
});

// live filter
var live = document.getElementById('live');
if (live) live.addEventListener('input', function(){ var q = this.value.toLowerCase();
  document.querySelectorAll('table.mem tbody tr[data-hay]').forEach(function(tr){
    var show = tr.getAttribute('data-hay').indexOf(q) !== -1;
    tr.style.display = show ? '' : 'none';
    var b = document.querySelector('tr[data-bodyfor="'+tr.dataset.id+'"]'); if (b && !show) b.style.display='none';
  }); });

// column sorting (inside each tbody)
var sortState = {};
document.querySelectorAll('th.sortable').forEach(function(th){
  th.addEventListener('click', function(){
    var key = th.dataset.sort;
    sortState[key] = !(sortState[key] || false);
    var asc = sortState[key];
    document.querySelectorAll('th.sortable .arrow').forEach(a => a.textContent = '');
    th.querySelector('.arrow').textContent = asc ? '▲' : '▼';
    document.querySelectorAll('table.mem tbody').forEach(function(tb){
      var pairs = [];
      tb.querySelectorAll('tr[data-id]').forEach(function(tr){
        pairs.push([tr, document.querySelector('tr[data-bodyfor="'+tr.dataset.id+'"]')]);
      });
      pairs.sort(function(a, b){
        var va = a[0].dataset[key] || '', vb = b[0].dataset[key] || '';
        return asc ? va.localeCompare(vb) : vb.localeCompare(va);
      });
      pairs.forEach(function(p){ tb.appendChild(p[0]); if (p[1]) tb.appendChild(p[1]); });
    });
  });
});

/* ---------- modal ---------- */
var modal = document.getElementById('modal');
var form = document.getElementById('mem-form');
function openModal(mode, d){
  d = d || {};
  document.getElementById('m-action').value = mode;
  document.getElementById('m-id').value = d.id || '';
  document.getElementById('m-type').value = d.type || 'gotcha';
  document.getElementById('m-scope').value = d.scope || 'global';
  document.getElementById('m-confidence').value = d.confidence || '1.0';
  document.getElementById('m-summary').value = d.summary || '';
  document.getElementById('m-body').value = d.body || '';
  document.getElementById('modal-title').textContent = (mode === 'edit') ? (TXT.editTitle + ' ' + (d.id || '')) : TXT.addTitle;
  var err = document.getElementById('m-err'); err.style.display = 'none'; err.textContent = '';
  modal.classList.add('open');
  setTimeout(function(){ document.getElementById('m-summary').focus(); }, 30);
}
function closeModal(){ modal.classList.remove('open'); }
document.getElementById('open-add').addEventListener('click', function(e){ e.preventDefault(); openModal('add'); });
document.querySelectorAll('[data-close]').forEach(function(b){ b.addEventListener('click', closeModal); });
modal.addEventListener('click', function(e){ if (e.target === modal) closeModal(); });
document.addEventListener('keydown', function(e){ if (e.key === 'Escape' && modal.classList.contains('open')) closeModal(); });

form.addEventListener('submit', function(e){
  e.preventDefault();
  var fd = new FormData(form); fd.set('csrf', CSRF);
  var btn = document.getElementById('m-submit'); btn.disabled = true;
  fetch('index.php', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
    .then(function(r){ return r.json(); })
    .then(function(j){
      btn.disabled = false;
      if (!j.ok){ var err = document.getElementById('m-err'); err.textContent = j.error || TXT.failed; err.style.display = 'block'; return; }
      location.reload();   // grouping/stats change — a reload is safer than DOM patching
    })
    .catch(function(){ btn.disabled = false; var err = document.getElementById('m-err'); err.textContent = TXT.network; err.style.display = 'block'; });
});

/* ---------- row actions ---------- */
document.addEventListener('click', function(e){
  var tr = e.target.closest('tr[data-id]'); if (!tr) return;
  if (e.target.classList.contains('act-edit')){
    closeActMenus();
    openModal('edit', { id: tr.dataset.id, type: tr.dataset.type, scope: tr.dataset.scope,
      confidence: tr.dataset.confidence, summary: tr.dataset.summary, body: tr.dataset.body });
  } else if (e.target.classList.contains('act-rescope')){
    closeActMenus();
    var ns = prompt(TXT.rescopeQ, tr.dataset.scope);
    if (ns && ns !== tr.dataset.scope) postAction('rescope', tr.dataset.id, { scope: ns }).then(function(){ location.reload(); });
  } else if (e.target.classList.contains('act-supersede')){
    closeActMenus();
    if (confirm(TXT.supersedeQ)) postAction('supersede', tr.dataset.id).then(function(){ location.reload(); });
  } else if (e.target.classList.contains('act-delete')){
    closeActMenus();
    if (confirm(TXT.deleteQ)) postAction('delete', tr.dataset.id).then(function(){ location.reload(); });
  }
});
function postAction(action, id, extra){
  var fd = new FormData(); fd.set('csrf', CSRF); fd.set('action', action); fd.set('id', id);
  if (extra) Object.keys(extra).forEach(function(k){ fd.set(k, extra[k]); });
  return fetch('index.php', { method: 'POST', headers: { 'X-Requested-With': 'XMLHttpRequest' }, body: fd })
    .then(function(r){ return r.json(); })
    .then(function(j){ if (!j.ok) { alert(j.error || TXT.failed); throw new Error(); } return j; });
}

/* ---------- bulk ---------- */
function selected(){ return Array.from(document.querySelectorAll('.rowsel:checked')).map(function(c){ return c.closest('tr').dataset.id; }); }
function refreshBulk(){
  var n = selected().length;
  document.getElementById('bulkcount').textContent = n;
  document.getElementById('bulkbar').classList.toggle('show', n > 0);
}
document.addEventListener('change', function(e){
  if (e.target.classList.contains('rowsel')) refreshBulk();
  if (e.target.id === 'selall'){
    document.querySelectorAll('.rowsel').forEach(function(c){
      if (c.closest('tr').style.display !== 'none') c.checked = e.target.checked;
    });
    refreshBulk();
  }
});
function bulkRun(fn){
  var ids = selected(); if (!ids.length) return;
  (function next(i){
    if (i >= ids.length) { location.reload(); return; }
    fn(ids[i]).then(function(){ next(i + 1); }).catch(function(){ location.reload(); });
  })(0);
}
document.getElementById('bulk-supersede').addEventListener('click', function(){
  if (confirm(TXT.supersedeQ + ' (' + selected().length + ')')) bulkRun(function(id){ return postAction('supersede', id); });
});
document.getElementById('bulk-delete').addEventListener('click', function(){
  if (confirm(TXT.deleteQ + ' (' + selected().length + ')')) bulkRun(function(id){ return postAction('delete', id); });
});
document.getElementById('bulk-rescope').addEventListener('click', function(){
  var ns = prompt(TXT.rescopeQ);
  if (ns) bulkRun(function(id){ return postAction('rescope', id, { scope: ns }); });
});
document.getElementById('bulk-clear').addEventListener('click', function(){
  document.querySelectorAll('.rowsel:checked').forEach(function(c){ c.checked = false; });
  var sa = document.getElementById('selall'); if (sa) sa.checked = false;
  refreshBulk();
});

/* ---------- live poll: dashboard self-updates; the table via banner ---------- */
var pollVer = '';
var pollBusy = false;
function pollStore(){
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  fetch('poll.php?ver=' + encodeURIComponent(pollVer))
    .then(function(r){ return r.json(); })
    .then(function(j){
      pollBusy = false;
      if (!j.ver) return;
      if (pollVer === '') { pollVer = j.ver; return; }   // first call: just remember the version
      if (!j.changed) return;
      pollVer = j.ver;
      var dc = document.getElementById('dash-cards');
      var rl = document.getElementById('recent-list');
      if (dc && j.cards_html) { dc.innerHTML = j.cards_html; flashEl(dc); }
      if (rl && j.recent_html) { rl.innerHTML = j.recent_html; flashEl(rl); }
      var h2c = document.querySelector('h2 .count');
      if (h2c) h2c.textContent = h2c.textContent.replace(/\d+$/, j.active);
      document.getElementById('newbanner').style.display = '';
    })
    .catch(function(){ pollBusy = false; });
}
function flashEl(el){ el.style.transition = 'none'; el.style.background = 'rgba(0,111,255,.07)';
  setTimeout(function(){ el.style.transition = 'background 1.2s'; el.style.background = ''; }, 60); }
setInterval(pollStore, 4000);
pollStore();
</script>
</body>
</html>
