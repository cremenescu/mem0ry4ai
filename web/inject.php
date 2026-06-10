<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — "What Claude sees": runs the REAL SessionStart hook and shows the exact injection.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$scopes = array_values(array_filter(known_scopes(), fn($s) => $s !== 'global'));
$sel = trim($_GET['scope'] ?? 'root');   // 'root' or 'project:<slug>'
$repoRoot = dirname(proj_root());

if ($sel === 'root') {
    $cwd = $repoRoot;
    $label = t('monorepo root (all projects)');
} elseif (str_starts_with($sel, 'project:')) {
    $slug = scope_label($sel);
    $cwd = $repoRoot . '/' . $slug;
    $label = "cwd = $slug/";
} else {
    $sel = 'root'; $cwd = $repoRoot; $label = t('monorepo root (all projects)');
}

// run the real hook (exact fidelity with what Claude receives)
$output = null; $err = null;
$hook = proj_root() . '/hooks/session_start.py';
// python3: MEM_PYTHON env override, otherwise PATH (inherited from the shell that started the server)
$py = getenv('MEM_PYTHON') ?: 'python3';

if (!function_exists('proc_open')) {
    $err = 'proc_open is disabled in PHP — cannot run the hook';
} else {
    $stdin = json_encode(['cwd' => $cwd, 'hook_event_name' => 'SessionStart', 'source' => 'preview']);
    $proc = proc_open([$py, $hook], [0 => ['pipe', 'r'], 1 => ['pipe', 'w'], 2 => ['pipe', 'w']], $pipes);
    if (is_resource($proc)) {
        fwrite($pipes[0], $stdin);
        fclose($pipes[0]);
        $output = stream_get_contents($pipes[1]);
        $stderr = stream_get_contents($pipes[2]);
        fclose($pipes[1]); fclose($pipes[2]);
        $code = proc_close($proc);
        if ($code !== 0) $err = "hook exit $code: " . trim($stderr);
    } else {
        $err = 'could not start python3 (set MEM_PYTHON=/path/to/python3)';
    }
}

$bytes = $output !== null ? strlen($output) : 0;
$tokens = (int)round($bytes / 4);   // rough estimate: ~4 chars/token
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= t('What Claude sees') ?> — mem0ry4ai</title>
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
  <h2><?= t('What Claude sees at SessionStart') ?></h2>

  <div class="toolbar">
    <form class="filters" method="get">
      <select name="scope" onchange="this.form.submit()">
        <option value="root"<?= $sel === 'root' ? ' selected' : '' ?>><?= t('monorepo root (all projects)') ?></option>
        <?php foreach ($scopes as $s): ?>
        <option value="<?= h($s) ?>"<?= $sel === $s ? ' selected' : '' ?>>cwd = <?= h(scope_label($s)) ?>/</option>
        <?php endforeach; ?>
      </select>
      <noscript><button class="btn" type="submit">OK</button></noscript>
    </form>
  </div>

  <?php if ($err !== null): ?>
    <div class="flash flash-error"><?= t('Preview unavailable:') ?> <?= h($err) ?>. <?= t('Run from the CLI:') ?>
      <code>echo '{"cwd":"<?= h($cwd) ?>","hook_event_name":"SessionStart"}' | python3 hooks/session_start.py</code></div>
  <?php elseif (trim((string)$output) === ''): ?>
    <div class="empty"><?= t('The hook injects nothing for') ?> <?= h($label) ?> <?= t('(no relevant memories).') ?></div>
  <?php else: ?>
    <p class="inject-meta"><?= t('Injection for') ?> <b><?= h($label) ?></b>: <b><?= number_format($bytes) ?> <?= t('bytes') ?></b> ≈ ~<?= number_format($tokens) ?> <?= t('tokens (approx.)') ?>.
      <?= ui_lang() === 'ro' ? t('inject.exact') : 'This is the exact output of the real hook (<code>hooks/session_start.py</code>), not an approximation.' ?></p>
    <pre class="inject"><?= h($output) ?></pre>
  <?php endif; ?>

  <p class="foot"><?= ui_lang() === 'ro' ? t('inject.foot') : 'The hook runs at every Claude Code session start (startup/resume/clear/compact).' ?></p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('What this page is for') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('inject.help') : 'Transparency: see exactly what context Claude receives automatically at session start, and what it costs (bytes/tokens).' ?></p>
  <h4><?= t('Modes') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('inject.help.root') : '<b>Root</b> = full global + a capped index of all projects (status/todo first, max 10 per project, projects untouched for 30+ days collapsed).' ?></p>
  <p><?= ui_lang() === 'ro' ? t('inject.help.project') : '<b>Sub-project</b> = global + ALL of that project\'s memories.' ?></p>
  <h4><?= t('If it looks bloated') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('inject.help.bloat') : 'Supersede/delete stale memories from the main list — the injection shrinks immediately.' ?></p>
</aside>
</div><!-- /layout -->
</main>
</body>
</html>
