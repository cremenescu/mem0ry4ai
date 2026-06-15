<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — git history of the store: the memory timeline (commits on store/), per-commit diff,
// commit from the UI, live updates without refresh (poll on HEAD + dirty state).
declare(strict_types=1);
require __DIR__ . '/lib.php';

// Page fingerprint: latest commit on store + dirty state. Changes on any checkpoint/write.
function git_page_version(): string {
    $head = git_run(['log', '-1', '--format=%H', '--', 'store']);
    $dirty = git_run(['status', '--porcelain', 'store']);
    return md5(implode('|', $head['lines'] ?? []) . '#' . implode('|', $dirty['lines'] ?? []));
}

// Fragment: the uncommitted-changes card (or the "clean" note) — single source for page and poll.
function render_dirty_card(array $dirty, string $csrf, bool $hasLog): string {
    ob_start();
    if ($dirty): ?>
  <div class="card">
    <h3><?= t('Uncommitted changes') ?> (<?= count($dirty) ?>)</h3>
    <ul class="dirty-list">
      <?php foreach ($dirty as $f): ?><li><code><?= h($f) ?></code></li><?php endforeach; ?>
    </ul>
    <form method="post" class="form-actions" style="margin-top:10px">
      <input type="hidden" name="csrf" value="<?= h($csrf) ?>">
      <input type="text" name="msg" placeholder="<?= t('commit message (empty = default message)') ?>" style="flex:1 1 280px">
      <button class="btn btn-primary" type="submit"><?= t('Commit the store') ?></button>
    </form>
  </div>
    <?php elseif ($hasLog): ?>
  <p class="meta"><?= t('Store clean — everything is committed.') ?></p>
    <?php endif;
    return ob_get_clean();
}

// Fragment: the commit list.
function render_gitlog(array $log): string {
    ob_start(); ?>
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
<?php
    return ob_get_clean();
}

/* AJAX: one commit's diff */
if (isset($_GET['diff'])) {
    header('Content-Type: text/plain; charset=utf-8');
    $d = git_store_diff(trim($_GET['diff']));
    echo $d ?? t('(diff unavailable)');
    exit;
}

/* AJAX: live poll (hold the session only long enough to grab the CSRF token) */
if (isset($_GET['poll'])) {
    session_start();
    $csrf = csrf_token();
    session_write_close();
    header('Content-Type: application/json; charset=utf-8');
    header('Cache-Control: no-store');
    $ver = git_page_version();
    if (($_GET['ver'] ?? '') === $ver) {
        echo json_encode(['changed' => false, 'ver' => $ver]);
        exit;
    }
    $dirty = git_dirty_files();
    $log = git_store_log(40);
    echo json_encode([
        'changed' => ($_GET['ver'] ?? '') !== '',
        'ver' => $ver,
        'count' => count($log),
        'dirty_html' => render_dirty_card($dirty, $csrf, $log !== []),
        'log_html' => render_gitlog($log),
    ], JSON_UNESCAPED_UNICODE);
    exit;
}

session_start();

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
<?= render_topbar('git') ?>

<main>
<div class="layout">
<div class="content">
  <div class="crumb"><a href="index.php"><?= t('Dashboard') ?></a> / <?= t('Git history') ?></div>
  <h2><?= t('Git history') ?> <span class="count"><?= t('the store timeline · last') ?> <span id="gcount"><?= count($log) ?></span> <?= t('commits') ?></span></h2>

  <?php if ($flash): ?><div class="flash flash-<?= h($flash[1]) ?>"><?= h($flash[0]) ?></div><?php endif; ?>

  <?php if ($log === [] && $dirty === []): ?>
    <div class="empty"><?= t('Git unavailable (not a repo, or exec is disabled in PHP).') ?></div>
  <?php endif; ?>

  <div id="dirty-area"><?= render_dirty_card($dirty, csrf_token(), $log !== []) ?></div>
  <div id="gitlog-area"><?= render_gitlog($log) ?></div>

  <p class="foot"><?= t('Only commits touching') ?> <code>store/</code> <?= t('(the memory). Code has its own history in the same repo.') ?> <?= t('The page updates itself.') ?></p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Git history') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('git.help') : 'Every commit on <code>store/</code> is one step in the memory\'s evolution: what was learned, what got superseded, when. Checkpoints happen automatically at session end, one commit per scope.' ?></p>
  <h4><?= t('Diff') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('git.help.diff') : 'Click a commit → its diff (store files only). Green = added, red = removed.' ?></p>
  <h4><?= t('Commit from the UI') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('git.help.commit') : 'The button commits ONLY <code>store/</code>, authored as <code>mem0ry4ai web</code>, no signing. Optional — the automatic checkpoint comes at session end anyway.' ?></p>
  <h4><?= t('Live') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('git.help.live') : 'The list updates itself when new commits or changes appear (4s poll). Open diffs stay open.' ?></p>
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

/* live poll: the list + dirty card update in place; open diffs are reopened */
var pollVer = '';
var pollBusy = false;
function pollGit(){
  if (pollBusy || document.hidden) return;
  pollBusy = true;
  fetch('git.php?poll=1&ver=' + encodeURIComponent(pollVer))
    .then(function(r){ return r.json(); })
    .then(function(j){
      pollBusy = false;
      if (!j.ver) return;
      if (pollVer === '') { pollVer = j.ver; return; }
      if (!j.changed) return;
      pollVer = j.ver;
      var open = [];
      document.querySelectorAll('.gcommit').forEach(function(c){
        var d = c.querySelector('.gdiff');
        if (d && d.style.display !== 'none') open.push(c.dataset.hash);
      });
      document.getElementById('dirty-area').innerHTML = j.dirty_html;
      var ga = document.getElementById('gitlog-area');
      ga.innerHTML = j.log_html;
      var gc = document.getElementById('gcount'); if (gc) gc.textContent = j.count;
      flashEl(ga);
      open.forEach(function(h){
        var c = document.querySelector('.gcommit[data-hash="' + h + '"] .ghead');
        if (c) toggleDiff(c);
      });
    })
    .catch(function(){ pollBusy = false; });
}
function flashEl(el){ el.style.transition = 'none'; el.style.background = 'rgba(0,111,255,.07)';
  setTimeout(function(){ el.style.transition = 'background 1.2s'; el.style.background = ''; }, 60); }
setInterval(pollGit, 4000);
pollGit();
</script>
</body>
</html>
