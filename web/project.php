<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — single project page: status + todos pinned on top, the rest grouped by type.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$slug = trim($_GET['slug'] ?? '');
if ($slug === '' || str_contains($slug, '/') || str_contains($slug, '..')) { redirect('index.php'); }
$scope = "project:$slug";

$all = array_values(array_filter(all_records(), fn($r) => ($r['meta']['scope'] ?? '') === $scope));
$active = array_values(array_filter($all, fn($r) => ($r['meta']['status'] ?? 'active') === 'active'));
usort($active, fn($a, $b) => strcmp($b['meta']['created'] ?? '', $a['meta']['created'] ?? ''));

$status = array_values(array_filter($active, fn($r) => ($r['meta']['type'] ?? '') === 'status'));
$todos  = array_values(array_filter($active, fn($r) => ($r['meta']['type'] ?? '') === 'todo'));
$rest   = array_values(array_filter($active, fn($r) => !in_array($r['meta']['type'] ?? '', ['status', 'todo'], true)));
$byType = [];
foreach ($rest as $r) $byType[$r['meta']['type'] ?? '?'][] = $r;
$typeOrder = ['gotcha', 'decision', 'fact', 'command', 'preference'];
uksort($byType, fn($a, $b) => (array_search($a, $typeOrder) ?? 9) <=> (array_search($b, $typeOrder) ?? 9));
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= h($slug) ?> — mem0ry4ai</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>">
<link rel="stylesheet" href="<?= h(asset('assets/style.css')) ?>">
</head>
<body>
<div class="topbar">
  <a class="brand" href="index.php">mem0ry4ai <small><?= t('local memory') ?></small></a>
  <div class="right"><a href="inject.php?scope=<?= h($scope) ?>"><?= t('What Claude sees here') ?></a> <?= lang_switch_html() ?></div>
</div>

<main>
<div class="layout">
<div class="content">
  <div class="crumb"><a href="index.php"><?= t('Memories') ?></a> / <?= t('project') ?></div>
  <h2><?= h($slug) ?> <span class="count"><?= count($active) ?> <?= t('active') ?><?= count($all) - count($active) ? ' · ' . (count($all) - count($active)) . ' superseded' : '' ?></span></h2>

  <?php if (!$active): ?>
    <div class="empty"><?= t('No active memories for') ?> <code><?= h($scope) ?></code>.</div>
  <?php endif; ?>

  <?php foreach ($status as $r): ?>
  <div class="pin">
    <h3><span class="badge t-status">status</span> <?= h(rec_summary($r)) ?>
      <span class="count" style="font-weight:400">· <?= h(mb_substr($r['meta']['created'] ?? '', 0, 16)) ?></span></h3>
    <div class="body"><?= render_body($r['body']) ?></div>
  </div>
  <?php endforeach; ?>

  <?php if ($todos): ?>
  <div class="pin todo">
    <h3><span class="badge t-todo">todo</span> <?= t('To do') ?> (<?= count($todos) ?>)</h3>
    <ul>
      <?php foreach ($todos as $r): ?>
      <li><b><?= h(rec_summary($r)) ?></b><?php $b = trim($r['body']); if ($b !== ''): ?> — <?= h(mb_substr(str_replace("\n", ' ', $b), 0, 160)) ?><?php endif; ?>
          <a class="meta" href="index.php?id=<?= h($r['id']) ?>"><?= h($r['id']) ?></a></li>
      <?php endforeach; ?>
    </ul>
  </div>
  <?php endif; ?>

  <?php foreach ($byType as $ty => $rs): ?>
  <div class="type-block">
    <h3><?= type_badge($ty) ?> <span class="count"><?= count($rs) ?></span></h3>
    <table class="mem">
      <tbody>
      <?php foreach ($rs as $r): ?>
        <tr data-id="<?= h($r['id']) ?>">
          <td class="summary" onclick="toggleBody(this)">
            <b><?= h(rec_summary($r)) ?></b>
            <div class="meta"><a href="index.php?id=<?= h($r['id']) ?>"><?= h($r['id']) ?></a> · conf <?= h($r['meta']['confidence'] ?? '?') ?> · <?= h($r['meta']['source'] ?? '') ?> · <?= h(mb_substr($r['meta']['created'] ?? '', 0, 16)) ?></div>
          </td>
        </tr>
        <tr class="bodyrow" style="display:none"><td><?= render_body($r['body']) ?></td></tr>
      <?php endforeach; ?>
      </tbody>
    </table>
  </div>
  <?php endforeach; ?>

  <p class="foot"><?= t('Edit / bulk:') ?> <a href="index.php?scope=<?= h(urlencode($scope)) ?>"><?= t('see the main list') ?></a> · CLI: <code>./mem.py list --scope <?= h($scope) ?></code></p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Project page') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('help.project') : 'The visual equivalent of the SessionStart injection: <b>status</b> (where you left off) and <b>todo</b> (what is next) on top, then knowledge grouped by type.' ?></p>
  <p><?= ui_lang() === 'ro' ? t('help.project2') : 'Open it when you return to a project after a break.' ?></p>
  <h4><?= t('Actions') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('help.project.actions') : 'Click a row → the full body. Edit / supersede / re-scope: from the main list.' ?></p>
</aside>
</div><!-- /layout -->
</main>

<script>
function toggleBody(cell){ var r = cell.closest('tr').nextElementSibling;
  if (r && r.classList.contains('bodyrow')) r.style.display = (r.style.display === 'none') ? '' : 'none'; }
</script>
</body>
</html>
