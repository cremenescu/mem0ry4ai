<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai web UI — lib: parser for store/*.md (mirrors mem.py) + CRUD + helpers + i18n.
// The source of truth is the markdown files; the DB index is derived. We work on .md directly.

declare(strict_types=1);

const TYPES = ['gotcha', 'fact', 'decision', 'command', 'preference', 'todo', 'status'];

function proj_root(): string { return dirname(__DIR__); }
function store_dir(): string { return proj_root() . '/store'; }
function global_file(): string { return store_dir() . '/global.md'; }
function proj_dir(): string { return store_dir() . '/projects'; }

function h($s): string { return htmlspecialchars((string)$s, ENT_QUOTES, 'UTF-8'); }

/* ---------- i18n: English strings inline are the keys; one Romanian map ---------- */

function ui_lang(): string {
    static $lang = null;
    if ($lang !== null) return $lang;
    $q = $_GET['lang'] ?? '';
    if ($q === 'en' || $q === 'ro') {
        $lang = $q;
        @setcookie('mem_lang', $q, time() + 86400 * 365, '/');
        return $lang;
    }
    $c = $_COOKIE['mem_lang'] ?? '';
    $lang = ($c === 'ro') ? 'ro' : 'en';
    return $lang;
}

function t(string $s): string {
    if (ui_lang() !== 'ro') return $s;
    static $ro = null;
    if ($ro === null) $ro = ro_strings();
    return $ro[$s] ?? $s;
}

function lang_switch_html(): string {
    $qs = $_GET;
    $cur = ui_lang();
    $out = '<span class="lang-switch">';
    foreach (['en', 'ro'] as $l) {
        $qs['lang'] = $l;
        $url = h(basename($_SERVER['PHP_SELF']) . '?' . http_build_query($qs));
        $cls = $l === $cur ? ' class="on"' : '';
        $out .= "<a$cls href=\"$url\">" . strtoupper($l) . '</a>';
    }
    return $out . '</span>';
}

function ro_strings(): array {
    return [
        'local memory' => 'memorie locala',
        'What Claude sees' => 'Ce vede Claude',
        'What Claude sees here' => 'Ce vede Claude aici',
        'to review' => 'de revizuit',
        'System status' => 'Status sistem',
        'active' => 'active',
        'superseded' => 'superseded',
        'open todos' => 'todo deschise',
        'scopes' => 'scope-uri',
        'Health' => 'Health',
        'Recent activity' => 'Activitate recenta',
        'Memories' => 'Memorii',
        'of' => 'din',
        'ranked search (FTS)' => 'cautare ranked (FTS)',
        'Supersede chain:' => 'Lant de supersedare:',
        'close' => 'inchide',
        'back' => 'inapoi',
        '(no chain)' => '(fara lant)',
        '+ Add memory' => '+ Adauga memorie',
        'search (FTS ranked)...' => 'cauta (FTS ranked)...',
        'all types' => 'toate tipurile',
        'all scopes' => 'toate scope-urile',
        'Search' => 'Cauta',
        'all' => 'toate',
        'No memories match the current filter.' => 'Nicio memorie pentru filtrul curent.',
        'Type' => 'Type', 'Scope' => 'Scope', 'Memory' => 'Memorie', 'Added' => 'Adaugat',
        'Global' => 'Global',
        'Project:' => 'Proiect:',
        'memories' => 'memorii',
        'project page →' => 'pagina proiectului →',
        'select all' => 'selecteaza tot',
        'Source of truth:' => 'Sursa de adevar:',
        '(markdown + git). CLI:' => '(markdown + git). CLI:',
        'selected' => 'selectate',
        'Re-scope' => 'Re-scope',
        'Supersede' => 'Supersede',
        'Delete' => 'Sterge',
        'Clear selection' => 'Deselecteaza',
        'Edit' => 'Editeaza',
        'View chain' => 'Vezi lantul',
        'Add memory' => 'Adauga memorie',
        'global or project:slug' => 'global sau project:slug',
        'one-line summary' => 'rezumat pe un rand',
        'memory details (simple markdown, inline `code`)' => 'detaliile memoriei (markdown simplu, `cod` inline)',
        'Save' => 'Salveaza',
        'Cancel' => 'Renunta',
        'Add' => 'Adauga',
        'The store changed in the meantime.' => 'Store-ul s-a schimbat intre timp.',
        'Refresh list' => 'Actualizeaza lista',
        'later' => 'mai tarziu',
        'Mark as superseded?' => 'Marchezi ca superseded?',
        'Delete permanently? (it stays in git history)' => 'Stergi definitiv? (ramane in git history)',
        'New scope (global or project:slug):' => 'Scope nou (global sau project:slug):',
        'Operation failed' => 'Operatie esuata',
        'Network error' => 'Eroare de retea',
        'Error' => 'Eroare',
        'Quick guide' => 'Ghid rapid',
        'help.intro' => 'O memorie = un fapt scurt pe care vrei sa-l tii minte intre sesiuni. Sursa de adevar e markdown + git; web UI-ul si mem.py sunt doua ferestre spre acelasi store.',
        'Types' => 'Tipuri',
        'help.gotcha' => 'Capcana / lectie: „X crapa din cauza Y, fa Z".',
        'help.fact' => 'Fapt stabil: IP, port, cale, target de deploy.',
        'help.decision' => 'Decizie de arhitectura + motivul ei.',
        'help.command' => 'O comanda utila.',
        'help.preference' => 'O preferinta a ta.',
        'help.todo' => 'Ce ramane de facut. Terminat = superseded.',
        'help.status' => 'Unde e proiectul / unde am ramas.',
        'Navigation' => 'Navigare',
        'help.nav' => 'Click pe scope → pagina proiectului (status + todos sus). Click pe id → lantul de supersedare. Click pe rand → body. Header de grup → pliaza.',
        'Bulk' => 'Bulk',
        'help.bulk' => 'Bifeaza randuri → bara de jos: Supersede / Re-scope / Sterge pe toate odata.',
        'help.search' => 'Camp + Enter = FTS ranked (acelasi index ca mem.py search). Tastarea filtreaza si live ce e pe ecran.',
        // project page
        'project' => 'proiect',
        'No active memories for' => 'Nicio memorie activa pentru',
        'To do' => 'De facut',
        'Project page' => 'Pagina proiectului',
        'help.project' => 'Echivalentul vizual al injectarii la SessionStart: status (unde am ramas) si todo (ce urmeaza) sus, apoi cunostintele grupate pe tip.',
        'help.project2' => 'Deschide-o cand reiei proiectul dupa o pauza.',
        'Actions' => 'Actiuni',
        'help.project.actions' => 'Click pe un rand → body-ul complet. Editare / supersede / re-scope: din lista principala.',
        'Edit / bulk:' => 'Editare/bulk:',
        'see the main list' => 'vezi in lista principala',
        // queue
        'Review queue' => 'Coada de review',
        'candidates' => 'candidati',
        'queue.intro' => 'Candidati extrasi automat de LLM-ul local din sesiuni. Nimic nu intra in memorie pana nu aprobi tu. Modelul e un generator de ciorne — corecteaza type/scope/summary inainte de aprobare.',
        'queue.empty' => 'Coada e goala. Ruleaza python3 consolidate.py --write ca sa extragi candidati din sesiunile capturate.',
        'queue.empty.done' => 'Coada e goala. Toti candidatii au fost procesati.',
        'Approve' => 'Aproba',
        'Edit & approve' => 'Editeaza si aproba',
        'Reject' => 'Respinge',
        'Reject this candidate? (it is dropped from the queue)' => 'Respingi candidatul? (se arunca din coada)',
        'How review works' => 'Cum merge review-ul',
        'queue.help' => 'LLM-ul local (Ollama) citeste transcripturile si propune candidati. E zgomotos si supra-increzator (confidence ~1 la tot), deci tu decizi.',
        'queue.help.actions' => 'Aproba = scrie in store ca atare. Editeaza si aproba = corectezi intai. Respinge = il arunci.',
        'Tip' => 'Sfat',
        'queue.help.tip' => 'Verifica mai ales type si scope. Sarcinile efemere („am facut X") = Respinge.',
        'Queue:' => 'Coada:',
        // inject
        'What Claude sees at SessionStart' => 'Ce vede Claude la SessionStart',
        'monorepo root (all projects)' => 'radacina monorepo (toate proiectele)',
        'Preview unavailable:' => 'Preview indisponibil:',
        'Run from the CLI:' => 'Ruleaza din CLI:',
        'The hook injects nothing for' => 'Hook-ul nu injecteaza nimic pentru',
        '(no relevant memories).' => '(nicio memorie relevanta).',
        'Injection for' => 'Injectare pentru',
        'bytes' => 'bytes',
        'tokens (approx.)' => 'tokens (aprox.)',
        'inject.exact' => 'Exact output-ul hook-ului real (hooks/session_start.py), nu o aproximare.',
        'inject.foot' => 'Hook-ul ruleaza la fiecare pornire de sesiune Claude Code (startup/resume/clear/compact).',
        'What this page is for' => 'La ce e buna pagina',
        'inject.help' => 'Transparenta: vezi exact ce context primeste Claude automat la pornirea unei sesiuni si cat costa (bytes/tokens).',
        'Modes' => 'Moduri',
        'inject.help.root' => 'Radacina = global complet + index plafonat pe toate proiectele (status/todo intai, max 10/proiect, proiectele neatinse 30+ zile colapsate).',
        'inject.help.project' => 'Sub-proiect = global + TOATE memoriile proiectului.',
        'If it looks bloated' => 'Daca pare umflat',
        'inject.help.bloat' => 'Supersedeaza/sterge memorii vechi din lista principala — injectarea scade imediat.',
        // health details
        'write OK' => 'scriere OK',
        'queue functional' => 'coada functionala',
        'not created yet' => 'inexistent inca',
        'up to date' => 'la zi',
        'rebuilt just now' => 'reconstruit acum',
        'FTS5 unavailable — search falls back to substring' => 'FTS5 indisponibil — search cade pe substring',
        'empty' => 'goala',
        'candidates to review' => 'candidati de revizuit',
        'registered in settings.json' => 'inregistrate in settings.json',
        'NOT in settings.json' => 'NU sunt in settings.json',
        'active — last capture' => 'active — ultima captura',
        'no evidence yet (no captures in staging)' => 'fara dovezi inca (nicio captura in staging)',
        'unknown' => 'necunoscut',
        'clean' => 'curat',
        'uncommitted changes' => 'modificari necomise',
        'Store writable' => 'Store writable',
        'Staging writable' => 'Staging writable',
        'FTS index' => 'Index FTS',
        'Review queue (health)' => 'Coada review',
        'Claude Code hooks' => 'Hooks Claude Code',
        'Git store' => 'Git store',
        // git history page
        'Git history' => 'Istoric git',
        'the store timeline · last' => 'timeline-ul store-ului · ultimele',
        'commits' => 'commit-uri',
        'Git unavailable (not a repo, or exec is disabled in PHP).' => 'Git indisponibil (nu e repo, sau exec e dezactivat in PHP).',
        'Uncommitted changes' => 'Modificari necomise',
        'commit message (empty = default message)' => 'mesaj de commit (gol = mesaj implicit)',
        'Commit the store' => 'Comite store-ul',
        'Store clean — everything is committed.' => 'Store curat — toate schimbarile sunt comise.',
        'Only commits touching' => 'Doar commit-urile care ating',
        '(the memory). Code has its own history in the same repo.' => '(memoria). Codul are istoricul lui in acelasi repo.',
        'Committed:' => 'Comis:',
        'Loading...' => 'Se incarca...',
        'Failed to load the diff.' => 'Eroare la incarcarea diff-ului.',
        '(diff unavailable)' => '(diff indisponibil)',
        'git.help' => 'Fiecare commit pe store/ = un pas in evolutia memoriei: ce s-a invatat, ce s-a supersedat, cand.',
        'Diff' => 'Diff',
        'git.help.diff' => 'Click pe un commit → diff-ul lui (doar fisierele din store). Verde = adaugat, rosu = scos. Cel mai recent se deschide automat.',
        'Commit from the UI' => 'Comite din UI',
        'git.help.commit' => 'Butonul comite DOAR store/, cu autor mem0ry4ai web si fara signing. Functioneaza cand serverul ruleaza ca userul tau (server_web.sh).',
    ];
}

/* ---------- scope ---------- */

function scope_file(string $scope): string {
    if ($scope === 'global') return global_file();
    if (str_starts_with($scope, 'project:')) {
        $slug = trim(substr($scope, 8));
        if ($slug === '' || str_contains($slug, '/') || str_contains($slug, '..')) {
            throw new RuntimeException("invalid scope: $scope");
        }
        return proj_dir() . "/$slug.md";
    }
    throw new RuntimeException("unknown scope: $scope");
}

function scope_label(string $scope): string {
    return $scope === 'global' ? 'global' : substr($scope, 8);
}

/* ---------- reading ---------- */

function store_files(): array {
    $files = [];
    if (is_file(global_file())) $files[] = global_file();
    if (is_dir(proj_dir())) {
        foreach (glob(proj_dir() . '/*.md') ?: [] as $f) $files[] = $f;
    }
    sort($files);
    return $files;
}

// Parse one file into records: ['id','meta'=>[],'title','body','start','end','file'].
function parse_file(string $path): array {
    if (!is_file($path)) return [];
    $lines = file($path, FILE_IGNORE_NEW_LINES);
    $records = []; $i = 0; $n = count($lines);
    while ($i < $n) {
        if (!preg_match('/^<!-- mem:start id=([0-9a-z-]+) -->\s*$/', $lines[$i], $m)) { $i++; continue; }
        $rec = ['id' => $m[1], 'meta' => [], 'title' => '', 'body' => '',
                'start' => $i, 'end' => null, 'file' => $path];
        $j = $i + 1; $bodyLines = []; $seenBlank = false;
        while ($j < $n && trim($lines[$j]) !== '<!-- mem:end -->') {
            $line = $lines[$j];
            if (preg_match('/^###\s+(.+)$/', $line, $tm) && $rec['title'] === '') {
                $rec['title'] = trim($tm[1]);
            } elseif (!$seenBlank && preg_match('/^- ([a-z-]+):\s*(.*)$/', $line, $mm)) {
                $rec['meta'][$mm[1]] = trim($mm[2]);
            } elseif (!$seenBlank && trim($line) === '' && $rec['meta']) {
                $seenBlank = true;
            } elseif ($seenBlank) {
                $bodyLines[] = $line;
            }
            $j++;
        }
        $rec['end'] = $j;
        $rec['body'] = trim(implode("\n", $bodyLines));   // trim both ends (like .strip() in mem.py)
        $records[] = $rec;
        $i = $j + 1;
    }
    return $records;
}

function all_records(): array {
    $out = [];
    foreach (store_files() as $f) $out = array_merge($out, parse_file($f));
    return $out;
}

function find_record(string $id): ?array {
    foreach (store_files() as $f) {
        foreach (parse_file($f) as $r) if ($r['id'] === $id) return $r;
    }
    return null;
}

// Known scopes (for the datalist in forms).
function known_scopes(): array {
    $set = ['global' => true];
    foreach (all_records() as $r) {
        if (!empty($r['meta']['scope'])) $set[$r['meta']['scope']] = true;
    }
    return array_keys($set);
}

/* ---------- writing ---------- */

function gen_id(): string {
    return date('Ymd') . '-' . substr(bin2hex(random_bytes(4)), 0, 6);
}

function atomic_write(string $path, string $content): void {
    $dir = dirname($path);
    if (!is_dir($dir)) { @mkdir($dir, 0777, true); @chmod($dir, 0777); }
    $tmp = $path . '.tmp.' . getmypid();
    file_put_contents($tmp, $content, LOCK_EX);
    rename($tmp, $path);
    @chmod($path, 0666);   // cross-writable: web server user + CLI user
}

function ensure_header(string $path, string $scope): void {
    if (is_file($path)) return;
    $title = $scope === 'global' ? 'Global memories' : 'Memories — ' . scope_label($scope);
    atomic_write($path, "# $title\n\n_mem0ry4ai store. Format: see `store/FORMAT.md`._\n\n");
}

function render_record(array $r): string {
    $lines = [
        "<!-- mem:start id={$r['id']} -->",
        "### {$r['type']} · " . scope_label($r['scope']) . " · {$r['summary']}",
        "- type: {$r['type']}",
        "- scope: {$r['scope']}",
        "- created: {$r['created']}",
        "- updated: {$r['updated']}",
        "- status: {$r['status']}",
    ];
    if (!empty($r['superseded-by'])) $lines[] = "- superseded-by: {$r['superseded-by']}";
    $lines[] = "- confidence: {$r['confidence']}";
    $lines[] = "- source: {$r['source']}";
    $lines[] = "";
    $lines[] = trim($r['body']);
    $lines[] = "<!-- mem:end -->";
    return implode("\n", $lines) . "\n";
}

function add_record(string $type, string $scope, string $summary, string $body,
                    string $confidence = '1.0', string $source = 'web'): string {
    if (!in_array($type, TYPES, true)) throw new RuntimeException("invalid type: $type");
    $body = trim($body);
    if ($body === '') throw new RuntimeException('empty body');
    $path = scope_file($scope);
    ensure_header($path, $scope);
    $id = gen_id();
    $block = render_record([
        'id' => $id, 'type' => $type, 'scope' => $scope, 'summary' => $summary,
        'created' => date('Y-m-d H:i:s'), 'updated' => date('Y-m-d H:i:s'), 'status' => 'active',
        'confidence' => $confidence, 'source' => $source, 'body' => $body,
    ]);
    file_put_contents($path, "\n" . $block, FILE_APPEND | LOCK_EX);
    return $id;
}

// Rewrite a record's block in place (edit/supersede/delete).
function rewrite_record(string $id, ?callable $transform): bool {
    foreach (store_files() as $path) {
        $recs = parse_file($path);
        foreach ($recs as $r) {
            if ($r['id'] !== $id) continue;
            $lines = file($path, FILE_IGNORE_NEW_LINES);
            $newBlock = $transform ? $transform($r) : null;   // null => delete
            $replacement = [];
            if ($newBlock !== null) {
                $replacement = explode("\n", rtrim($newBlock, "\n"));
            } else {
                // also drop a separating blank line above the block
                if ($r['start'] > 0 && trim($lines[$r['start'] - 1]) === '') {
                    array_splice($lines, $r['start'] - 1, 1);
                    $r['start']--; $r['end']--;
                }
            }
            array_splice($lines, $r['start'], $r['end'] - $r['start'] + 1, $replacement);
            atomic_write($path, implode("\n", $lines) . "\n");
            return true;
        }
    }
    return false;
}

function update_record(string $id, array $fields): bool {
    return rewrite_record($id, function (array $r) use ($fields) {
        $m = $r['meta'];
        $data = [
            'id' => $r['id'],
            'type' => $fields['type'] ?? ($m['type'] ?? 'fact'),
            'scope' => $fields['scope'] ?? ($m['scope'] ?? 'global'),
            'summary' => $fields['summary'] ?? trim(explode('·', $r['title'] . '··')[2] ?? ''),
            'created' => $m['created'] ?? date('Y-m-d H:i:s'),
            'updated' => date('Y-m-d H:i:s'),
            'status' => $m['status'] ?? 'active',
            'confidence' => $fields['confidence'] ?? ($m['confidence'] ?? '1.0'),
            'source' => $m['source'] ?? 'web',
            'body' => $fields['body'] ?? $r['body'],
        ];
        if (!empty($m['superseded-by'])) $data['superseded-by'] = $m['superseded-by'];
        return render_record($data);
    });
}

function supersede_record(string $id, string $by = ''): bool {
    return rewrite_record($id, function (array $r) use ($by) {
        $m = $r['meta'];
        $data = [
            'id' => $r['id'], 'type' => $m['type'] ?? 'fact', 'scope' => $m['scope'] ?? 'global',
            'summary' => trim(explode('·', $r['title'] . '··')[2] ?? ''),
            'created' => $m['created'] ?? date('Y-m-d H:i:s'), 'updated' => date('Y-m-d H:i:s'),
            'status' => 'superseded', 'confidence' => $m['confidence'] ?? '1.0',
            'source' => $m['source'] ?? 'web', 'body' => $r['body'],
        ];
        if ($by !== '') $data['superseded-by'] = $by;
        return render_record($data);
    });
}

function delete_record(string $id): bool {
    return rewrite_record($id, null);
}

/* ---------- shared helpers ---------- */

function rec_summary(array $r): string {
    $parts = array_map('trim', explode('·', $r['title']));
    return ($parts[2] ?? '') !== '' ? $parts[2] : $r['title'];
}

function type_badge(string $t): string {
    return '<span class="badge t-' . h($t) . '">' . h($t) . '</span>';
}

// Store version = cheap fingerprint (stat only, no parsing). Changes on any write.
function store_version(): string {
    $parts = [];
    foreach (store_files() as $f) $parts[] = $f . ':' . filemtime($f) . ':' . filesize($f);
    $q = queue_file();
    if (is_file($q)) $parts[] = 'q:' . filemtime($q) . ':' . filesize($q);
    return md5(implode('|', $parts));
}

/* ---------- dashboard fragments (shared between index.php and poll.php) ---------- */

function render_dash_cards(array $stats): string {
    ob_start(); ?>
      <a class="card-stat" href="index.php?status=active"><div class="num"><?= $stats['active'] ?></div><div class="lbl"><?= t('active') ?></div></a>
      <a class="card-stat" href="index.php?status=superseded"><div class="num"><?= $stats['superseded'] ?></div><div class="lbl"><?= t('superseded') ?></div></a>
      <a class="card-stat <?= $stats['todos'] > 0 ? 'warn' : '' ?>" href="index.php?type=todo&status=active"><div class="num"><?= $stats['todos'] ?></div><div class="lbl"><?= t('open todos') ?></div></a>
      <a class="card-stat" href="index.php?status=active"><div class="num"><?= count($stats['by_scope']) ?></div><div class="lbl"><?= t('scopes') ?></div></a>
      <?php $shown = 0; foreach ($stats['by_type'] as $ty => $n): if ($ty === 'todo') continue; if (++$shown > 3) break; ?>
      <a class="card-stat" href="index.php?type=<?= h($ty) ?>&status=active"><div class="num"><?= $n ?></div><div class="lbl"><?= h($ty) ?></div></a>
      <?php endforeach;
    return ob_get_clean();
}

function render_recent_list(array $stats): string {
    ob_start();
    foreach ($stats['recent'] as $r) { $m = $r['meta']; ?>
          <li><?= type_badge($m['type'] ?? '?') ?> <a href="index.php?id=<?= h($r['id']) ?>"><?= h(mb_substr(rec_summary($r), 0, 52)) ?></a>
              <span class="src"><?= h($m['source'] ?? '') ?></span>
              <span class="when"><?= h(mb_substr($m['created'] ?? '', 5, 11)) ?></span></li>
    <?php }
    return ob_get_clean();
}

/* ---------- stats + health (dashboard) ---------- */

function store_stats(): array {
    $recs = all_records();
    $s = ['total' => count($recs), 'active' => 0, 'superseded' => 0,
          'by_type' => [], 'by_scope' => [], 'todos' => 0, 'recent' => []];
    $active = [];
    foreach ($recs as $r) {
        $st = $r['meta']['status'] ?? 'active';
        if ($st === 'active') {
            $s['active']++;
            $t = $r['meta']['type'] ?? '?';
            $sc = $r['meta']['scope'] ?? '?';
            $s['by_type'][$t] = ($s['by_type'][$t] ?? 0) + 1;
            $s['by_scope'][$sc] = ($s['by_scope'][$sc] ?? 0) + 1;
            if ($t === 'todo') $s['todos']++;
            $active[] = $r;
        } else {
            $s['superseded']++;
        }
    }
    usort($active, fn($a, $b) => strcmp($b['meta']['created'] ?? '', $a['meta']['created'] ?? ''));
    $s['recent'] = array_slice($active, 0, 8);
    arsort($s['by_type']);
    arsort($s['by_scope']);
    return $s;
}

function health_checks(): array {
    $out = [];
    $out[] = [t('Store writable'), is_writable(store_dir()) && is_writable(global_file()),
              is_writable(global_file()) ? t('write OK') : 'chmod store + *.md'];
    $stg = proj_root() . '/staging';
    $out[] = [t('Staging writable'), is_dir($stg) ? is_writable($stg) : null,
              is_dir($stg) ? (is_writable($stg) ? t('queue functional') : 'chmod staging') : t('not created yet')];
    // FTS index: stale is not an error — rebuild on the spot (self-healing, cheap)
    if (fts_index_fresh()) {
        $out[] = [t('FTS index'), true, t('up to date')];
    } elseif (fts_rebuild()) {
        $out[] = [t('FTS index'), true, t('rebuilt just now')];
    } else {
        $out[] = [t('FTS index'), false, t('FTS5 unavailable — search falls back to substring')];
    }
    $nq = count(queue_pending());
    $out[] = [t('Review queue (health)'), $nq === 0, $nq === 0 ? t('empty') : "$nq " . t('candidates to review')];
    // hooks installed: settings.json first (authoritative), then empirical evidence = staged captures
    $cands = array_filter([getenv('HOME') ? getenv('HOME') . '/.claude/settings.json' : null]);
    $hooks = null; $hdet = '';
    foreach ($cands as $c) {
        $raw = @file_get_contents($c);
        if ($raw !== false) {
            $hooks = str_contains($raw, dirname(__DIR__) . '/hooks/');
            $hdet = $hooks ? t('registered in settings.json') : t('NOT in settings.json');
            break;
        }
    }
    if ($hooks === null) {
        // settings.json unreadable (different user) -> living proof: the capture hook writes sessions.jsonl
        $sess = proj_root() . '/staging/sessions.jsonl';
        if (is_file($sess) && filesize($sess) > 0) {
            $hooks = true;
            $hdet = t('active — last capture') . ' ' . date('Y-m-d H:i', filemtime($sess));
        } else {
            $hdet = t('no evidence yet (no captures in staging)');
        }
    }
    $out[] = [t('Claude Code hooks'), $hooks, $hdet];
    $dirty = git_dirty();
    $out[] = [t('Git store'), $dirty === null ? null : !$dirty,
              $dirty === null ? t('unknown') : ($dirty ? t('uncommitted changes') : t('clean'))];
    return $out;
}

/* ---------- server-side FTS5 search (same index as mem.py) ---------- */

function fts_index_fresh(): bool {
    $idx = store_dir() . '/.index.db';
    if (!is_file($idx)) return false;
    $imt = filemtime($idx);
    foreach (store_files() as $f) if (filemtime($f) > $imt) return false;
    return true;
}

function fts_rebuild(): bool {
    $tmp = store_dir() . '/.index.db.phpbuild';
    @unlink($tmp);
    try {
        $pdo = new PDO('sqlite:' . $tmp);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $pdo->exec('CREATE VIRTUAL TABLE mem USING fts5(id UNINDEXED, summary, body)');
        $st = $pdo->prepare('INSERT INTO mem (id, summary, body) VALUES (?, ?, ?)');
        foreach (all_records() as $r) $st->execute([$r['id'], rec_summary($r), $r['body']]);
        $pdo = null;
        rename($tmp, store_dir() . '/.index.db');
        @chmod(store_dir() . '/.index.db', 0666);
        return true;
    } catch (Throwable $e) {
        @unlink($tmp);
        return false;
    }
}

// Ids ordered by relevance (bm25). null = FTS unavailable (substring fallback).
function fts_query(string $q): ?array {
    if (!fts_index_fresh() && !fts_rebuild()) return null;
    preg_match_all('/\w+/u', $q, $m);
    $terms = $m[0];
    if (!$terms) return [];
    $match = implode(' OR ', array_map(fn($t) => '"' . str_replace('"', '', $t) . '"*', $terms));
    try {
        $pdo = new PDO('sqlite:' . store_dir() . '/.index.db');
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $st = $pdo->prepare('SELECT id FROM mem WHERE mem MATCH ? ORDER BY bm25(mem)');
        $st->execute([$match]);
        return $st->fetchAll(PDO::FETCH_COLUMN);
    } catch (Throwable $e) {
        return null;
    }
}

/* ---------- re-scope (move a record between scope files/projects) ---------- */

function rescope_record(string $id, string $newScope): bool {
    $r = find_record($id);
    if (!$r) return false;
    $m = $r['meta'];
    if (($m['scope'] ?? '') === $newScope) return true;
    $data = [
        'id' => $r['id'], 'type' => $m['type'] ?? 'fact', 'scope' => $newScope,
        'summary' => rec_summary($r),
        'created' => $m['created'] ?? date('Y-m-d H:i:s'), 'updated' => date('Y-m-d H:i:s'),
        'status' => $m['status'] ?? 'active', 'confidence' => $m['confidence'] ?? '1.0',
        'source' => $m['source'] ?? 'web', 'body' => $r['body'],
    ];
    if (!empty($m['superseded-by'])) $data['superseded-by'] = $m['superseded-by'];
    $path = scope_file($newScope);          // validate the scope BEFORE deleting
    rewrite_record($id, null);              // remove from the old file
    ensure_header($path, $newScope);
    file_put_contents($path, "\n" . render_record($data), FILE_APPEND | LOCK_EX);
    @chmod($path, 0666);
    return true;
}

/* ---------- supersede chain ---------- */

function supersede_chain(string $id): array {
    $all = all_records();
    $by_id = [];
    foreach ($all as $r) $by_id[$r['id']] = $r;
    // backwards: who was replaced by me (recursively)
    $back = []; $cur = $id;
    do {
        $prev = null;
        foreach ($all as $r) if (($r['meta']['superseded-by'] ?? '') === $cur) { $prev = $r['id']; break; }
        if ($prev) { $back[] = $prev; $cur = $prev; }
    } while ($prev);
    // forwards: whom mine is replaced by
    $fwd = []; $cur = $id;
    while (!empty($by_id[$cur]['meta']['superseded-by'])) {
        $cur = $by_id[$cur]['meta']['superseded-by'];
        if (!isset($by_id[$cur]) || in_array($cur, $fwd, true)) break;
        $fwd[] = $cur;
    }
    return array_merge(array_reverse($back), [$id], $fwd);
}

/* ---------- review queue (LLM candidates awaiting human approval) ---------- */

function queue_file(): string { return proj_root() . '/staging/queue.jsonl'; }

function queue_load(): array {
    $f = queue_file();
    if (!is_file($f)) return [];
    $out = [];
    foreach (file($f, FILE_IGNORE_NEW_LINES) as $line) {
        $line = trim($line);
        if ($line === '') continue;
        $r = json_decode($line, true);
        if (is_array($r)) $out[] = $r;
    }
    return $out;
}

function queue_save(array $records): void {
    $lines = array_map(fn($r) => json_encode($r, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES), $records);
    atomic_write(queue_file(), $lines ? implode("\n", $lines) . "\n" : "");
}

function queue_pending(): array {
    return array_values(array_filter(queue_load(), fn($r) => ($r['status'] ?? 'pending') === 'pending'));
}

function queue_get(string $qid): ?array {
    foreach (queue_load() as $r) if (($r['qid'] ?? '') === $qid) return $r;
    return null;
}

function queue_remove(string $qid): void {
    queue_save(array_values(array_filter(queue_load(), fn($r) => ($r['qid'] ?? '') !== $qid)));
}

// Approve a candidate: write it to the store (with optional corrections) and drop it from the queue.
function queue_approve(string $qid, array $over = []): bool {
    $r = queue_get($qid);
    if (!$r) return false;
    add_record(
        $over['type'] ?? ($r['type'] ?? 'fact'),
        $over['scope'] ?? ($r['scope'] ?? 'global'),
        $over['summary'] ?? ($r['summary'] ?? ''),
        (string)($over['body'] ?? ($r['body'] ?? '')),
        (string)($over['confidence'] ?? ($r['confidence'] ?? '0.8')),
        $r['source'] ?? 'llm'
    );
    queue_remove($qid);
    return true;
}

/* ---------- git status ---------- */

function git_dirty(): ?bool {
    $root = proj_root();
    if (!is_dir("$root/.git")) return null;
    if (!function_exists('exec')) return null;
    // PITFALL: shell_exec returns null BOTH on error AND on empty output (= clean repo) — use exec + exit code.
    // safe.directory: under a different user (e.g. a webserver daemon) git refuses repos owned by someone else.
    $cmd = 'git -C ' . escapeshellarg($root) . ' -c safe.directory=' . escapeshellarg($root)
         . ' status --porcelain store 2>/dev/null';
    $lines = []; $code = 1;
    @exec($cmd, $lines, $code);
    if ($code !== 0) return null;
    return count(array_filter($lines, fn($l) => trim($l) !== '')) > 0;
}


/* ---------- git history (git.php page) ---------- */

function git_run(array $args): ?array {
    $root = proj_root();
    if (!is_dir("$root/.git") || !function_exists('exec')) return null;
    $cmd = 'git -C ' . escapeshellarg($root) . ' -c safe.directory=' . escapeshellarg($root);
    foreach ($args as $a) $cmd .= ' ' . escapeshellarg($a);
    $cmd .= ' 2>&1';
    $lines = []; $code = 1;
    @exec($cmd, $lines, $code);
    return ['code' => $code, 'lines' => $lines];
}

// Commits touching store/ (the memory timeline): hash, date, subject.
function git_store_log(int $n = 30): array {
    $r = git_run(['log', "-$n", '--date=format:%Y-%m-%d %H:%M', '--pretty=format:%h|%ad|%s', '--', 'store']);
    if ($r === null || $r['code'] !== 0) return [];
    $out = [];
    foreach ($r['lines'] as $l) {
        $p = explode('|', $l, 3);
        if (count($p) === 3) $out[] = ['hash' => $p[0], 'date' => $p[1], 'subject' => $p[2]];
    }
    return $out;
}

function git_store_diff(string $hash): ?string {
    if (!preg_match('/^[0-9a-f]{7,40}$/', $hash)) return null;
    $r = git_run(['show', $hash, '--stat', '--patch', '--no-color', '--', 'store']);
    if ($r === null || $r['code'] !== 0) return null;
    return implode("\n", $r['lines']);
}

function git_dirty_files(): array {
    $r = git_run(['status', '--porcelain', 'store']);
    if ($r === null || $r['code'] !== 0) return [];
    return array_values(array_filter(array_map('trim', $r['lines'])));
}

// Commit store/ changes (runs as the server user; signing off so a locked keychain cannot block it).
function git_commit_store(string $msg): array {
    $msg = trim($msg) !== '' ? trim($msg) : 'store: updates from the web UI';
    $r = git_run(['add', 'store']);
    if ($r === null || $r['code'] !== 0) return [false, 'git add failed: ' . implode(' ', $r['lines'] ?? [])];
    $root = proj_root();
    $cmd = 'git -C ' . escapeshellarg($root) . ' -c safe.directory=' . escapeshellarg($root)
         . ' -c commit.gpgsign=false -c user.name=' . escapeshellarg('mem0ry4ai web')
         . ' -c user.email=' . escapeshellarg('web@mem0ry4ai.local')
         . ' commit -m ' . escapeshellarg($msg) . ' -- store 2>&1';
    $lines = []; $code = 1;
    @exec($cmd, $lines, $code);
    if ($code !== 0) return [false, implode(' ', array_slice($lines, 0, 3))];
    return [true, $lines[0] ?? 'committed'];
}

/* ---------- csrf + flash + render ---------- */

function csrf_token(): string {
    if (empty($_SESSION['csrf'])) $_SESSION['csrf'] = bin2hex(random_bytes(16));
    return $_SESSION['csrf'];
}
function csrf_check(): void {
    if (($_POST['csrf'] ?? '') !== ($_SESSION['csrf'] ?? '_')) { http_response_code(400); exit('CSRF'); }
}
function flash(string $msg, string $kind = 'info'): void { $_SESSION['flash'] = [$msg, $kind]; }
function take_flash(): ?array { $f = $_SESSION['flash'] ?? null; unset($_SESSION['flash']); return $f; }
function redirect(string $url): void { header("Location: $url"); exit; }

// Minimal body rendering: escape + inline `code` + keep line breaks.
function render_body(string $body): string {
    $esc = h($body);
    $esc = preg_replace('/`([^`]+)`/', '<code>$1</code>', $esc);
    return nl2br($esc);
}

function asset(string $rel): string {
    $abs = __DIR__ . '/' . $rel;
    $v = is_file($abs) ? filemtime($abs) : '0';
    return $rel . '?v=' . $v;
}
