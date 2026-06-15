<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai web UI — dashboard: system status (stat cards, health, recent activity).
// The memories list lives on memories.php; per-project summary on projects.php.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$stats  = store_stats();
$health = health_checks();
$flash  = take_flash();
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
    <a href="memories.php"><?= t('Memories') ?></a>
    <a href="projects.php"><?= t('Projects') ?></a>
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

  <h2><?= t('System status') ?></h2>
  <div class="dash" id="dash-cards"><?= render_dash_cards($stats) ?></div>
  <div class="dash-row">
    <div class="dash-col">
      <h4><?= t('Health') ?></h4>
      <ul class="health">
        <?php foreach ($health as [$lbl, $ok, $det]): ?>
        <li><span class="dot <?= $ok === null ? 'dot-unk' : ($ok ? 'dot-ok' : 'dot-err') ?>"></span> <?= h($lbl) ?> <span class="hd"><?= h($det) ?></span></li>
        <?php endforeach; ?>
      </ul>
    </div>
    <div class="dash-col">
      <h4><?= t('Recent activity') ?></h4>
      <ul class="recent" id="recent-list"><?= render_recent_list($stats) ?></ul>
    </div>
  </div>

  <p class="foot"><?= t('Source of truth:') ?> <code>store/*.md</code> <?= t('(markdown + git). CLI:') ?> <code>./mem.py</code>.</p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Quick guide') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('help.intro') : 'A memory is one short fact you want to keep between sessions. The source of truth is markdown + git; the web UI and <code>mem.py</code> are two windows onto the same store.' ?></p>
  <h4><?= t('Navigation') ?></h4>
  <p><?= ui_lang() === 'ro'
        ? 'Cardurile de sus duc in <a href="memories.php">Memorii</a> (lista filtrabila) si <a href="projects.php">Proiecte</a> (sumar per proiect). <a href="inject.php">Ce vede Claude</a> = injectarea la SessionStart; <a href="git.php">Istoric git</a> = timeline-ul store-ului.'
        : 'The cards above lead to <a href="memories.php">Memories</a> (the filterable list) and <a href="projects.php">Projects</a> (per-project summary). <a href="inject.php">What Claude sees</a> = the SessionStart injection; <a href="git.php">Git history</a> = the store timeline.' ?></p>
</aside>
</div><!-- /layout -->
</main>

<script>
/* ---------- live poll: the dashboard updates itself ---------- */
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
      if (pollVer === '') { pollVer = j.ver; return; }
      if (!j.changed) return;
      pollVer = j.ver;
      var dc = document.getElementById('dash-cards');
      var rl = document.getElementById('recent-list');
      if (dc && j.cards_html) { dc.innerHTML = j.cards_html; flashEl(dc); }
      if (rl && j.recent_html) { rl.innerHTML = j.recent_html; flashEl(rl); }
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
