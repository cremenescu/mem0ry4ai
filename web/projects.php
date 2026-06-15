<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — projects summary: every project with its active count, open todos and current
// status (where you left off), each card linking into the project page.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$projects = projects_overview();
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= t('Projects') ?> — mem0ry4ai</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>">
<link rel="stylesheet" href="<?= h(asset('assets/style.css')) ?>">
</head>
<body>
<?= render_topbar('projects') ?>

<main>
<div class="layout">
<div class="content">
  <div class="crumb"><a href="index.php"><?= t('Dashboard') ?></a> / <?= t('Projects') ?></div>
  <h2><?= t('Projects') ?> <span class="count"><?= count($projects) ?></span></h2>

  <?php if (!$projects): ?>
    <div class="empty"><?= t('No projects yet.') ?></div>
  <?php endif; ?>

  <div class="projcards">
    <?php foreach ($projects as $slug => $p): ?>
    <a class="projcard" href="project.php?slug=<?= h($slug) ?>">
      <div class="pchead">
        <span class="pcname"><?= h($slug) ?></span>
        <span class="pcmeta"><?= (int)$p['n'] ?> <?= t('memories') ?><?php if ($p['todo']): ?> · <span class="ptodo"><?= (int)$p['todo'] ?> todo</span><?php endif; ?></span>
      </div>
      <?php if ($p['status'] !== ''): ?>
        <div class="pcstatus"><span class="badge t-status">status</span> <?= h(mb_substr($p['status'], 0, 120)) ?></div>
      <?php endif; ?>
    </a>
    <?php endforeach; ?>
  </div>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Projects') ?></h3>
  <p><?= ui_lang() === 'ro'
        ? 'Toate proiectele dintr-o privire: cate memorii are fiecare, cate todo-uri deschise si unde ai ramas (status). Click pe un proiect → pagina lui.'
        : 'Every project at a glance: how many memories it has, open todos, and where you left off (status). Click a project → its page.' ?></p>
</aside>
</div><!-- /layout -->
</main>
</body>
</html>
