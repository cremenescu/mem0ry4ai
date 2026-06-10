<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — git history of the store: the memory timeline (commits on store/), per-commit diff, commit from the UI.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

/* AJAX: one commit's diff */
if (isset($_GET['diff'])) {
    header('Content-Type: text/plain; charset=utf-8');
    $d = git_store_diff(trim($_GET['diff']));
    echo $d ?? t('(diff unavailable)');
    exit;
}

/* POST: commit the store changes */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    csrf_check();
    [$ok, $msg] = git_commit_store((string)($_POST['msg'] ?? ''));
    flash($ok ? t('Committed:') . " $msg" : t('Error') . ": $msg", $ok ? 'success' : 'error');
    redirect('git.php');
}

$dirty = git_dirty_files();
$log = git_store_log(40);
$flash = take_flash();
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= t('Git history') ?> — mem0ry4ai</title>
<link rel="icon" href="data:image/svg+xml,<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 32 32'><rect width='32' height='32' rx='7' fill='%23006fff'/><text x='16' y='23' font-size='19' text-anchor='middle' fill='white' font-family='sans-serif' font-weight='bold'>m</text></svg>">
<link rel="stylesheet" href="<?= h(asset('assets/style.css')) ?>">
</head>
<body>
<div class="topbar">
  <a class="brand" href="index.php">mem0ry4ai <small><?= t('local memory') ?></small></a>
  <div class="right"><a href="index.php">← <?= t('Memories') ?></a> <?= lang_switch_html() ?></div>
</div>

<main>
<div class="layout">
<div class="content">
  <h2><?= t('Git history') ?> <span class="count"><?= t('the store timeline · last') ?> <?= count($log) ?> <?= t('commits') ?></span></h2>

  <?php if ($flash): ?><div class="flash flash-<?= h($flash[1]) ?>"><?= h($flash[0]) ?></div><?php endif; ?>

  <?php if ($log === [] && $dirty === []): ?>
    <div class="empty"><?= t('Git unavailable (not a repo, or exec is disabled in PHP).') ?></div>
  <?php endif; ?>

  <?php if ($dirty): ?>
  <div class="card">
    <h3><?= t('Uncommitted changes') ?> (<?= count($dirty) ?>)</h3>
    <ul class="dirty-list">
      <?php foreach ($dirty as $f): ?><li><code><?= h($f) ?></code></li><?php endforeach; ?>
    </ul>
    <form method="post" class="form-actions" style="margin-top:10px">
      <input type="hidden" name="csrf" value="<?= h(csrf_token()) ?>">
      <input type="text" name="msg" placeholder="<?= t('commit message (empty = default message)') ?>" style="flex:1 1 280px">
      <button class="btn btn-primary" type="submit"><?= t('Commit the store') ?></button>
    </form>
  </div>
  <?php elseif ($log): ?>
  <p class="meta"><?= t('Store clean — everything is committed.') ?></p>
  <?php endif; ?>

  <?php if ($log): ?>
  <div class="gitlog">
    <?php foreach ($log as $c): ?>
    <div class="gcommit" data-hash="<?= h($c['hash']) ?>">
      <div class="ghead" onclick="toggleDiff(this)">
        <code class="ghash"><?= h($c['hash']) ?></code>
        <span class="gsubj"><?= h($c['subject']) ?></span>
        <span class="gdate"><?= h($c['date']) ?></span>
      </div>
      <pre class="gdiff" style="display:none" data-loaded="0"></pre>
    </div>
    <?php endforeach; ?>
  </div>
  <?php endif; ?>

  <p class="foot"><?= t('Only commits touching') ?> <code>store/</code> <?= t('(the memory). Code has its own history in the same repo.') ?></p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Git history') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('git.help') : 'Every commit on <code>store/</code> is one step in the memory\'s evolution: what was learned, what got superseded, when.' ?></p>
  <h4><?= t('Diff') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('git.help.diff') : 'Click a commit → its diff (store files only). Green = added, red = removed. The most recent one opens automatically.' ?></p>
  <h4><?= t('Commit from the UI') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('git.help.commit') : 'The button commits ONLY <code>store/</code>, authored as <code>mem0ry4ai web</code>, no signing. Works when the server runs as your user (<code>server_web.sh</code>).' ?></p>
</aside>
</div><!-- /layout -->
</main>

<script>
var TXT = { loading: <?= json_encode(t('Loading...')) ?>, fail: <?= json_encode(t('Failed to load the diff.')) ?> };
function toggleDiff(head){
  var box = head.parentNode.querySelector('.gdiff');
  if (box.style.display !== 'none') { box.style.display = 'none'; return; }
  box.style.display = '';
  if (box.dataset.loaded === '1') return;
  box.textContent = TXT.loading;
  fetch('git.php?diff=' + encodeURIComponent(head.parentNode.dataset.hash))
    .then(function(r){ return r.text(); })
    .then(function(t){
      box.dataset.loaded = '1';
      box.innerHTML = '';
      t.split('\n').forEach(function(line){
        var span = document.createElement('span');
        if (line.startsWith('+') && !line.startsWith('+++')) span.className = 'dl-add';
        else if (line.startsWith('-') && !line.startsWith('---')) span.className = 'dl-del';
        else if (line.startsWith('@@') || line.startsWith('diff ') || line.startsWith('commit ')) span.className = 'dl-meta';
        span.textContent = line;
        box.appendChild(span);
        box.appendChild(document.createTextNode('\n'));
      });
    })
    .catch(function(){ box.textContent = TXT.fail; });
}
// the newest commit opens expanded by default
var first = document.querySelector('.gcommit .ghead');
if (first) toggleDiff(first);
</script>
</body>
</html>
