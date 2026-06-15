<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — relations overview: every related-to / blocked-by edge, grouped by project.
// A text precursor to the graph view: see at a glance how memories connect.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

$edges = all_links();

// group by the scope of the "a" record; cross-scope edges noted inline
$groups = [];
foreach ($edges as $e) {
    $sc = $e['a']['meta']['scope'] ?? 'global';
    $groups[$sc][] = $e;
}
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

  <?php if (!$edges): ?>
    <div class="empty"><?= ui_lang() === 'ro'
        ? 'Nicio legatura inca. Leaga memorii inrudite cu <code>mem.py link &lt;id&gt; &lt;alt&gt;</code> sau <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.'
        : 'No links yet. Connect related memories with <code>mem.py link &lt;id&gt; &lt;other&gt;</code> or <code>mem.py block &lt;todo&gt; &lt;blocker&gt;</code>.' ?></div>
  <?php endif; ?>

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
</div><!-- /content -->

<aside class="help">
  <h3><?= t('Links') ?></h3>
  <p><?= ui_lang() === 'ro'
        ? 'Toate legaturile dintre memorii: <b>↔</b> = inrudite (related-to), <b>⟂</b> = un todo blocat de altceva. Click pe un capat → memoria lui. Pana la graficul vizual, asta e privirea de ansamblu.'
        : 'Every link between memories: <b>↔</b> = related (related-to), <b>⟂</b> = a todo blocked by something. Click an endpoint → its memory. Until the visual graph, this is the bird&rsquo;s-eye view.' ?></p>
  <h4><?= t('Navigation') ?></h4>
  <p><?= ui_lang() === 'ro'
        ? 'Le legi cu <code>mem.py link</code> / <code>mem.py block</code>, sau editand o memorie. Eu le leg deliberat cand scriu memorii evident inrudite.'
        : 'Create them with <code>mem.py link</code> / <code>mem.py block</code>, or by editing a memory. The agent links them deliberately when writing clearly-related memories.' ?></p>
</aside>
</div><!-- /layout -->
</main>
</body>
</html>
