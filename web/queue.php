<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — review queue: candidates extracted by the local LLM await human approval.
declare(strict_types=1);
session_start();
require __DIR__ . '/lib.php';

/* ---------- POST (AJAX) ---------- */
if ($_SERVER['REQUEST_METHOD'] === 'POST') {
    csrf_check();
    $action = $_POST['action'] ?? '';
    $qid = trim($_POST['qid'] ?? '');
    $ok = false; $err = null;
    try {
        if ($action === 'approve') {
            $over = [];
            foreach (['type', 'scope', 'summary', 'body', 'confidence'] as $k) {
                if (isset($_POST[$k]) && $_POST[$k] !== '') $over[$k] = trim((string)$_POST[$k]);
            }
            $ok = queue_approve($qid, $over);
        } elseif ($action === 'reject') {
            queue_remove($qid); $ok = true;
        } else {
            $err = 'unknown action';
        }
    } catch (Throwable $e) { $err = $e->getMessage(); }

    header('Content-Type: application/json; charset=utf-8');
    echo json_encode($err !== null ? ['ok' => false, 'error' => $err]
                                   : ['ok' => $ok, 'qid' => $qid, 'remaining' => count(queue_pending())]);
    exit;
}

$pending = queue_pending();
// sort by confidence, highest first
usort($pending, fn($a, $b) => ($b['confidence'] ?? 0) <=> ($a['confidence'] ?? 0));
$scopes = known_scopes();
?>
<!doctype html>
<html lang="<?= ui_lang() ?>">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title><?= t('Review queue') ?> — mem0ry4ai</title>
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
  <h2><?= t('Review queue') ?> <span class="count"><?= count($pending) ?> <?= t('candidates') ?></span></h2>
  <p class="meta" style="margin-top:0"><?= ui_lang() === 'ro' ? t('queue.intro') : 'Candidates extracted automatically by the local LLM from session transcripts. <b>Nothing enters memory until you approve it.</b> The model is a draft generator — fix type/scope/summary before approving.' ?></p>

  <div id="cards">
  <?php if (!$pending): ?>
    <div class="empty"><?= ui_lang() === 'ro' ? t('queue.empty') : 'The queue is empty. Run <code>python3 consolidate.py --write</code> to extract candidates from captured sessions.' ?></div>
  <?php else: foreach ($pending as $r): ?>
    <div class="qcard" data-qid="<?= h($r['qid']) ?>"
         data-type="<?= h($r['type'] ?? 'fact') ?>" data-scope="<?= h($r['scope'] ?? 'global') ?>"
         data-summary="<?= h($r['summary'] ?? '') ?>" data-body="<?= h($r['body'] ?? '') ?>"
         data-confidence="<?= h((string)($r['confidence'] ?? '')) ?>">
      <div class="qhead">
        <?= type_badge($r['type'] ?? '?') ?>
        <span class="scope-tag"><?= h(scope_label($r['scope'] ?? 'global')) ?></span>
        <span class="conf">conf <?= h((string)($r['confidence'] ?? '?')) ?></span>
        <span class="qsrc"><?= h($r['source'] ?? '') ?></span>
      </div>
      <div class="qsum"><?= h($r['summary'] ?? '') ?></div>
      <div class="qbody"><?= render_body($r['body'] ?? '') ?></div>
      <div class="qactions">
        <button type="button" class="btn btn-primary q-approve"><?= t('Approve') ?></button>
        <button type="button" class="btn q-edit"><?= t('Edit & approve') ?></button>
        <button type="button" class="btn btn-danger q-reject"><?= t('Reject') ?></button>
      </div>
    </div>
  <?php endforeach; endif; ?>
  </div>

  <p class="foot"><?= t('Source of truth:') ?> <code>store/*.md</code>. <?= t('Queue:') ?> <code>staging/queue.jsonl</code>.</p>
</div><!-- /content -->

<aside class="help">
  <h3><?= t('How review works') ?></h3>
  <p><?= ui_lang() === 'ro' ? t('queue.help') : 'The local LLM (Ollama) reads session transcripts and proposes candidates. It is noisy and over-confident (~1.0 confidence on everything), so <b>you decide</b>.' ?></p>
  <h4><?= t('Actions') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('queue.help.actions') : '<b>Approve</b> = write to the store as-is. <b>Edit &amp; approve</b> = fix it first. <b>Reject</b> = drop it.' ?></p>
  <h4><?= t('Tip') ?></h4>
  <p><?= ui_lang() === 'ro' ? t('queue.help.tip') : 'Double-check <b>type</b> and <b>scope</b>. Ephemeral tasks ("did X") = Reject.' ?></p>
</aside>
</div><!-- /layout -->
</main>

<!-- edit & approve modal -->
<div class="modal-overlay" id="modal">
  <div class="modal">
    <div class="modal-head"><h3 id="modal-title"><?= t('Edit & approve') ?></h3>
      <button type="button" class="modal-close" data-close>&times;</button></div>
    <form id="mem-form">
      <input type="hidden" name="qid" id="m-qid">
      <div class="form-row">
        <label><?= t('Type') ?> <select name="type" id="m-type"><?php foreach (TYPES as $ty): ?><option><?= h($ty) ?></option><?php endforeach; ?></select></label>
        <label><?= t('Scope') ?> <small><?= t('global or project:slug') ?></small><input type="text" name="scope" id="m-scope" list="scopes"></label>
        <label>Confidence <input type="text" name="confidence" id="m-confidence"></label>
      </div>
      <label>Summary <input type="text" name="summary" id="m-summary" required></label>
      <label>Body <textarea name="body" id="m-body" required></textarea></label>
      <div class="modal-err" id="m-err" style="display:none"></div>
      <div class="form-actions"><button class="btn btn-primary" type="submit"><?= t('Approve') ?></button>
        <button type="button" class="btn btn-ghost" data-close><?= t('Cancel') ?></button></div>
    </form>
  </div>
</div>
<datalist id="scopes"><?php foreach ($scopes as $s): ?><option value="<?= h($s) ?>"><?php endforeach; ?></datalist>

<script>
var CSRF = <?= json_encode(csrf_token()) ?>;
var TXT = {
  rejectQ: <?= json_encode(t('Reject this candidate? (it is dropped from the queue)')) ?>,
  error: <?= json_encode(t('Error')) ?>,
  candidates: <?= json_encode(t('candidates')) ?>,
  emptyDone: <?= json_encode(ui_lang() === 'ro' ? t('queue.empty.done') : 'The queue is empty. All candidates have been processed.') ?>
};
var modal = document.getElementById('modal'), form = document.getElementById('mem-form');
function closeModal(){ modal.classList.remove('open'); }
document.querySelectorAll('[data-close]').forEach(b => b.addEventListener('click', closeModal));
modal.addEventListener('click', e => { if (e.target === modal) closeModal(); });
document.addEventListener('keydown', e => { if (e.key === 'Escape') closeModal(); });

function post(data){
  var fd = new FormData(); fd.set('csrf', CSRF);
  Object.keys(data).forEach(k => fd.set(k, data[k]));
  return fetch('queue.php', { method:'POST', headers:{'X-Requested-With':'XMLHttpRequest'}, body: fd }).then(r => r.json());
}
function removeCard(qid){ var c = document.querySelector('.qcard[data-qid="'+qid+'"]'); if (c) c.remove();
  var left = document.querySelectorAll('.qcard').length;
  document.querySelector('.count').textContent = left + ' ' + TXT.candidates;
  if (!left) document.getElementById('cards').innerHTML = '<div class="empty">' + TXT.emptyDone + '</div>';
}

document.getElementById('cards').addEventListener('click', function(e){
  var card = e.target.closest('.qcard'); if (!card) return;
  var qid = card.dataset.qid;
  if (e.target.classList.contains('q-approve')){
    post({ action:'approve', qid: qid }).then(j => { if (j.ok) removeCard(qid); else alert(j.error||TXT.error); });
  } else if (e.target.classList.contains('q-reject')){
    if (!confirm(TXT.rejectQ)) return;
    post({ action:'reject', qid: qid }).then(j => { if (j.ok) removeCard(qid); else alert(j.error||TXT.error); });
  } else if (e.target.classList.contains('q-edit')){
    document.getElementById('m-qid').value = qid;
    document.getElementById('m-type').value = card.dataset.type;
    document.getElementById('m-scope').value = card.dataset.scope;
    document.getElementById('m-confidence').value = card.dataset.confidence;
    document.getElementById('m-summary').value = card.dataset.summary;
    document.getElementById('m-body').value = card.dataset.body;
    document.getElementById('m-err').style.display = 'none';
    modal.classList.add('open');
  }
});
form.addEventListener('submit', function(e){
  e.preventDefault();
  var data = { action:'approve', qid: document.getElementById('m-qid').value,
    type: document.getElementById('m-type').value, scope: document.getElementById('m-scope').value,
    confidence: document.getElementById('m-confidence').value, summary: document.getElementById('m-summary').value,
    body: document.getElementById('m-body').value };
  post(data).then(j => { if (j.ok){ removeCard(data.qid); closeModal(); }
    else { var er = document.getElementById('m-err'); er.textContent = j.error||TXT.error; er.style.display='block'; } });
});
</script>
</body>
</html>
