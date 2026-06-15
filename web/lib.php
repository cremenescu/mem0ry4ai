<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai web UI — lib: parser for store/*.md (mirrors mem.py) + CRUD + helpers + i18n.
// The source of truth is the markdown files; the DB index is derived. We work on .md directly.

declare(strict_types=1);

const TYPES = ['gotcha', 'fact', 'decision', 'command', 'procedural', 'preference', 'todo', 'status'];

function proj_root(): string { return dirname(__DIR__); }

// Data dir (store/, staging/, git repo). Same resolution as mem.py: MEM_DATA_DIR
// override > plugin-safe default (~/.mem0ry4ai) > next to the code.
function data_root(): string {
    static $d = null;
    if ($d !== null) return $d;
    $env = getenv('MEM_DATA_DIR');
    if ($env !== false && $env !== '') return $d = rtrim($env, '/');
    if (str_contains(proj_root() . '/', '/.claude/plugins/')) {
        return $d = rtrim((string)getenv('HOME'), '/') . '/.mem0ry4ai';
    }
    return $d = proj_root();
}
function store_dir(): string { return data_root() . '/store'; }
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
    // default language: cookie > MEM_UI_LANG env (a local instance can default to 'ro') > 'en'
    $envdef = (getenv('MEM_UI_LANG') === 'ro') ? 'ro' : 'en';
    $lang = ($c === 'ro' || $c === 'en') ? $c : $envdef;
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

// Shared top bar — identical on every page; $active highlights the current section.
function render_topbar(string $active = ''): string {
    $nq = count(queue_pending());
    $nav = [
        'dashboard' => ['index.php',    t('Dashboard')],
        'memories'  => ['memories.php', t('Memories')],
        'projects'  => ['projects.php', t('Projects')],
        'links'     => ['links.php',    t('Links')],
        'git'       => ['git.php',      t('Git history')],
        'inject'    => ['inject.php',   t('What Claude sees')],
    ];
    ob_start(); ?>
<div class="topbar">
  <a class="brand" href="index.php">mem0ry4ai <small><?= t('local memory') ?></small></a>
  <div class="right">
    <?php foreach ($nav as $key => [$href, $label]): ?><a<?= $active === $key ? ' class="nav-on"' : '' ?> href="<?= $href ?>"><?= h($label) ?></a> <?php endforeach; ?>
    <?php if ($nq > 0): ?><a class="review-tag" href="queue.php"><?= $nq ?> <?= t('to review') ?></a> <?php endif; ?>
    <?= lang_switch_html() ?>
  </div>
</div>
<?php return ob_get_clean();
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
        'Dashboard' => 'Panou',
        'Projects' => 'Proiecte',
        'Links' => 'Legaturi',
        'Suggested links' => 'Sugestii de legaturi',
        'Link' => 'Leaga',
        'Dismiss' => 'Respinge',
        'No projects yet.' => 'Niciun proiect inca.',
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
        'help.procedural' => 'O secventa multi-pas / runbook (release, deploy, ritual).',
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
        'ready' => 'ready',
        'blocked' => 'blocate',
        'blocked by' => 'blocat de',
        'related' => 'inrudite',
        'files' => 'fisiere',
        'invalidated' => 'invalidat',
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
        'uncommitted — auto-checkpoint at session end' => 'necomise — checkpoint automat la final de sesiune',
        'Store writable' => 'Store writable',
        'Staging writable' => 'Staging writable',
        'FTS index' => 'Index FTS',
        'Review queue (health)' => 'Coada review',
        'Claude Code hooks' => 'Hooks Claude Code',
        'SessionStart injection' => 'Injectare SessionStart',
        'budget' => 'buget',
        'critical rules' => 'reguli critice',
        'critical rule: always injected, first, at SessionStart' => 'regula critica: injectata garantat, prima, la SessionStart',
        'critical rules are always in; the rest is trimmed deterministically.' => 'regulile critice intra garantat; restul se taie controlat.',
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
        'git.help.diff' => 'Click pe un commit → diff-ul lui (doar fisierele din store). Verde = adaugat, rosu = scos.',
        'Commit from the UI' => 'Comite din UI',
        'git.help.commit' => 'Butonul comite DOAR store/, cu autor mem0ry4ai web si fara signing. Optional — checkpoint-ul automat vine oricum la finalul sesiunii.',
        'The page updates itself.' => 'Pagina se actualizeaza singura.',
        'Live' => 'Live',
        'git.help.live' => 'Lista se actualizeaza singura cand apar commit-uri sau modificari noi (poll la 4s). Diff-urile deschise raman deschise.',
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
    if (!empty($r['priority'])) $lines[] = "- priority: {$r['priority']}";
    if (!empty($r['files'])) $lines[] = "- files: {$r['files']}";
    if (!empty($r['related-to'])) $lines[] = "- related-to: {$r['related-to']}";
    if (!empty($r['blocked-by'])) $lines[] = "- blocked-by: {$r['blocked-by']}";
    if (!empty($r['superseded-by'])) $lines[] = "- superseded-by: {$r['superseded-by']}";
    if (!empty($r['invalidated'])) $lines[] = "- invalidated: {$r['invalidated']}";
    if (!empty($r['invalid-reason'])) $lines[] = "- invalid-reason: {$r['invalid-reason']}";
    $lines[] = "- confidence: {$r['confidence']}";
    $lines[] = "- source: {$r['source']}";
    $lines[] = "";
    $lines[] = trim($r['body']);
    $lines[] = "<!-- mem:end -->";
    return implode("\n", $lines) . "\n";
}

/* ---------- relations: related-to (any record) + blocked-by (todos) ---------- */

// The comma-separated id list stored in a meta field (related-to / blocked-by).
function rec_ids(array $r, string $key): array {
    $v = $r['meta'][$key] ?? '';
    return array_values(array_filter(array_map('trim', explode(',', $v)), fn($x) => $x !== ''));
}

// Blockers of a todo that are still OPEN (= an active todo). Resolved = superseded/missing.
function open_blockers(array $r, array $byId): array {
    $out = [];
    foreach (rec_ids($r, 'blocked-by') as $bid) {
        $b = $byId[$bid] ?? null;
        if ($b && ($b['meta']['status'] ?? 'active') === 'active' && ($b['meta']['type'] ?? '') === 'todo') $out[] = $bid;
    }
    return $out;
}

// Map id => record, for relation lookups.
function records_by_id(): array {
    $by = [];
    foreach (all_records() as $r) $by[$r['id']] = $r;
    return $by;
}

// Reverse index id => [ids that declare related-to it], so links show on both ends.
function related_in_index(): array {
    $in = [];
    foreach (all_records() as $r) foreach (rec_ids($r, 'related-to') as $t) $in[$t][] = $r['id'];
    return $in;
}

// A clickable id chip linking to the record, titled with its summary.
function id_chip(string $id, array $byId): string {
    $sum = isset($byId[$id]) ? rec_summary($byId[$id]) : '';
    return '<a class="idchip" href="memories.php?id=' . h($id) . '" title="' . h($sum) . '">' . h(substr($id, -6)) . '</a>';
}

// "↔ a, b" line of related links (outgoing + incoming), or '' if none.
function related_html(array $r, array $relIn, array $byId): string {
    $ids = array_values(array_unique(array_merge(rec_ids($r, 'related-to'), $relIn[$r['id']] ?? [])));
    if (!$ids) return '';
    return '<div class="rel">↔ ' . implode(' ', array_map(fn($i) => id_chip($i, $byId), $ids)) . '</div>';
}

/* ---------- B: link suggestions (semantic, human-confirmed) ---------- */

function pair_key(string $a, string $b): string { return $a < $b ? "$a|$b" : "$b|$a"; }
function dismiss_file(): string { return data_root() . '/staging/link-dismissed.jsonl'; }

function dismissed_pairs(): array {
    $p = dismiss_file();
    if (!is_file($p)) return [];
    $out = [];
    foreach (file($p, FILE_IGNORE_NEW_LINES | FILE_SKIP_EMPTY_LINES) as $l) {
        $d = json_decode($l, true);
        if (isset($d['pair'])) $out[$d['pair']] = true;
    }
    return $out;
}

function dismiss_pair(string $a, string $b): bool {
    $dir = dirname(dismiss_file());
    if (!is_dir($dir)) @mkdir($dir, 0777, true);
    return @file_put_contents(dismiss_file(), json_encode(['pair' => pair_key($a, $b)]) . "\n",
                              FILE_APPEND | LOCK_EX) !== false;
}

// All fields render_record expects, rebuilt from a parsed record (so callbacks can tweak one).
function rec_data(array $r): array {
    $m = $r['meta'];
    $d = [
        'id' => $r['id'], 'type' => $m['type'] ?? 'fact', 'scope' => $m['scope'] ?? 'global',
        'summary' => rec_summary($r), 'created' => $m['created'] ?? date('Y-m-d H:i:s'),
        'updated' => $m['updated'] ?? date('Y-m-d H:i:s'), 'status' => $m['status'] ?? 'active',
        'confidence' => $m['confidence'] ?? '1.0', 'source' => $m['source'] ?? 'web', 'body' => $r['body'],
    ];
    foreach (['priority', 'files', 'related-to', 'blocked-by', 'superseded-by', 'invalidated', 'invalid-reason'] as $k) {
        if (!empty($m[$k])) $d[$k] = $m[$k];
    }
    return $d;
}

// Add a related-to edge a->b (idempotent). Same effect as `mem.py link`.
function link_records(string $a, string $b): bool {
    if ($a === '' || $b === '' || $a === $b) return false;
    return rewrite_record($a, function (array $r) use ($b) {
        $d = rec_data($r);
        $cur = array_values(array_filter(array_map('trim', explode(',', $d['related-to'] ?? ''))));
        if (!in_array($b, $cur, true)) $cur[] = $b;
        $d['related-to'] = implode(', ', $cur);
        $d['updated'] = date('Y-m-d H:i:s');
        return render_record($d);
    });
}

// Closest UNLINKED pairs (semantic), excluding existing edges + dismissed. [] if no embeddings.
function suggested_links(int $limit = 12, float $threshold = 0.62): array {
    $emb = load_embeddings();
    if (count($emb) < 2) return [];
    $byId = records_by_id();
    $emb = array_filter($emb, fn($v, $id) => isset($byId[$id]) && ($byId[$id]['meta']['status'] ?? 'active') === 'active',
                        ARRAY_FILTER_USE_BOTH);
    $exist = dismissed_pairs();
    foreach ($byId as $r) {
        foreach (array_merge(rec_ids($r, 'related-to'), rec_ids($r, 'blocked-by')) as $to) {
            $exist[pair_key($r['id'], $to)] = true;
        }
    }
    $ids = array_keys($emb);
    if (count($ids) > 1500) $ids = array_slice($ids, 0, 1500);  // safety cap on the O(n^2) scan
    $pairs = [];
    for ($i = 0; $i < count($ids); $i++) {
        for ($j = $i + 1; $j < count($ids); $j++) {
            $k = pair_key($ids[$i], $ids[$j]);
            if (isset($exist[$k])) continue;
            $s = cosine_sim($emb[$ids[$i]], $emb[$ids[$j]]);
            if ($s >= $threshold) $pairs[] = ['a' => $ids[$i], 'b' => $ids[$j], 'sim' => $s];
        }
    }
    usort($pairs, fn($x, $y) => $y['sim'] <=> $x['sim']);
    return array_slice($pairs, 0, $limit);
}

// All relation edges across the store: related-to (undirected) + blocked-by (directed).
// Each edge: ['kind'=>'related'|'blocked', 'a'=>record, 'b'=>record]. For 'blocked', a is the todo, b the blocker.
function all_links(): array {
    $by = records_by_id();
    $seen = []; $edges = [];
    foreach (all_records() as $r) {
        foreach (rec_ids($r, 'related-to') as $to) {
            if (!isset($by[$to])) continue;
            $key = ($r['id'] < $to) ? "$r[id]|$to" : "$to|$r[id]";
            if (isset($seen[$key])) continue;
            $seen[$key] = true;
            $edges[] = ['kind' => 'related', 'a' => $r, 'b' => $by[$to]];
        }
        foreach (rec_ids($r, 'blocked-by') as $to) {
            if (!isset($by[$to])) continue;
            $edges[] = ['kind' => 'blocked', 'a' => $r, 'b' => $by[$to]];
        }
    }
    return $edges;
}

// Files chips + (when superseded) the invalidated date/reason, for a record body.
function rec_extras_html(array $r): string {
    $out = '';
    $files = rec_ids($r, 'files');
    if ($files) {
        $out .= '<div class="rec-files">' . h(t('files')) . ': '
              . implode(' ', array_map(fn($f) => '<code>' . h($f) . '</code>', $files)) . '</div>';
    }
    $inv = $r['meta']['invalidated'] ?? '';
    if ($inv !== '') {
        $reason = $r['meta']['invalid-reason'] ?? '';
        $out .= '<div class="inval-note">' . h(t('invalidated')) . ' ' . h(mb_substr($inv, 0, 16))
              . ($reason !== '' ? ' — ' . h($reason) : '') . '</div>';
    }
    return $out;
}

// Combined relations line for a record body: related-to + (for todos) blocked-by.
function relations_block(array $r, array $relIn, array $byId): string {
    $out = related_html($r, $relIn, $byId);
    if (($r['meta']['type'] ?? '') === 'todo') {
        $bb = rec_ids($r, 'blocked-by');
        if ($bb) {
            $open = open_blockers($r, $byId);
            $cls = $open ? 'blockedby' : 'rel';
            $out .= '<div class="' . $cls . '" style="margin-top:4px;display:inline-block">'
                  . t('blocked by') . ' ' . implode(' ', array_map(fn($i) => id_chip($i, $byId), $bb)) . '</div>';
        }
    }
    return $out;
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
        if (!empty($m['priority'])) $data['priority'] = $m['priority'];
        if (!empty($m['files'])) $data['files'] = $m['files'];
        if (!empty($m['related-to'])) $data['related-to'] = $m['related-to'];
        if (!empty($m['blocked-by'])) $data['blocked-by'] = $m['blocked-by'];
        if (!empty($m['superseded-by'])) $data['superseded-by'] = $m['superseded-by'];
        if (!empty($m['invalidated'])) $data['invalidated'] = $m['invalidated'];
        if (!empty($m['invalid-reason'])) $data['invalid-reason'] = $m['invalid-reason'];
        return render_record($data);
    });
}

function supersede_record(string $id, string $by = '', string $reason = ''): bool {
    return rewrite_record($id, function (array $r) use ($by, $reason) {
        $m = $r['meta'];
        $data = [
            'id' => $r['id'], 'type' => $m['type'] ?? 'fact', 'scope' => $m['scope'] ?? 'global',
            'summary' => trim(explode('·', $r['title'] . '··')[2] ?? ''),
            'created' => $m['created'] ?? date('Y-m-d H:i:s'), 'updated' => date('Y-m-d H:i:s'),
            'status' => 'superseded', 'confidence' => $m['confidence'] ?? '1.0',
            'source' => $m['source'] ?? 'web', 'body' => $r['body'],
        ];
        if (!empty($m['priority'])) $data['priority'] = $m['priority'];
        if (!empty($m['files'])) $data['files'] = $m['files'];
        if (!empty($m['related-to'])) $data['related-to'] = $m['related-to'];
        if (!empty($m['blocked-by'])) $data['blocked-by'] = $m['blocked-by'];
        if ($by !== '') $data['superseded-by'] = $by;
        // bi-temporal: record WHEN it stopped being valid (= when we learned) + WHY
        $data['invalidated'] = date('Y-m-d H:i:s');
        if ($reason !== '') $data['invalid-reason'] = $reason;
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
    $nproj = count(array_filter(array_keys($stats['by_scope']), fn($s) => strncmp($s, 'project:', 8) === 0));
    ob_start(); ?>
      <a class="card-stat" href="memories.php?status=all"><div class="num"><?= $stats['total'] ?></div><div class="lbl"><?= t('Memories') ?></div></a>
      <a class="card-stat" href="memories.php?status=active"><div class="num"><?= $stats['active'] ?></div><div class="lbl"><?= t('active') ?></div></a>
      <a class="card-stat" href="memories.php?status=superseded"><div class="num"><?= $stats['superseded'] ?></div><div class="lbl"><?= t('superseded') ?></div></a>
      <a class="card-stat <?= $stats['todos'] > 0 ? 'warn' : '' ?>" href="memories.php?type=todo&status=active"><div class="num"><?= $stats['todos'] ?></div><div class="lbl"><?= t('open todos') ?></div></a>
      <a class="card-stat" href="projects.php"><div class="num"><?= $nproj ?></div><div class="lbl"><?= t('Projects') ?></div></a>
      <a class="card-stat" href="links.php"><div class="num"><?= $stats['links'] ?? 0 ?></div><div class="lbl"><?= t('Links') ?></div></a>
      <?php $shown = 0; foreach ($stats['by_type'] as $ty => $n): if ($ty === 'todo') continue; if (++$shown > 3) break; ?>
      <a class="card-stat" href="memories.php?type=<?= h($ty) ?>&status=active"><div class="num"><?= $n ?></div><div class="lbl"><?= h($ty) ?></div></a>
      <?php endforeach;
    return ob_get_clean();
}

// Per-project overview for projects.php: active count, open todos, newest status summary.
function projects_overview(): array {
    $p = [];
    foreach (all_records() as $r) {
        if (($r['meta']['status'] ?? 'active') !== 'active') continue;
        $sc = $r['meta']['scope'] ?? '';
        if (strncmp($sc, 'project:', 8) !== 0) continue;
        $slug = substr($sc, 8);
        if (!isset($p[$slug])) $p[$slug] = ['n' => 0, 'todo' => 0, 'status' => '', 'sdate' => ''];
        $p[$slug]['n']++;
        $t = $r['meta']['type'] ?? '';
        if ($t === 'todo') $p[$slug]['todo']++;
        if ($t === 'status') {
            $d = $r['meta']['created'] ?? '';
            if ($d > $p[$slug]['sdate']) { $p[$slug]['sdate'] = $d; $p[$slug]['status'] = rec_summary($r); }
        }
    }
    ksort($p);
    return $p;
}

function render_recent_list(array $stats): string {
    ob_start();
    foreach ($stats['recent'] as $r) { $m = $r['meta']; ?>
          <li><?= type_badge($m['type'] ?? '?') ?> <a href="memories.php?id=<?= h($r['id']) ?>"><?= h(mb_substr(rec_summary($r), 0, 52)) ?></a>
              <span class="src"><?= h($m['source'] ?? '') ?></span>
              <span class="when"><?= h(mb_substr($m['created'] ?? '', 5, 11)) ?></span></li>
    <?php }
    return ob_get_clean();
}

/* ---------- stats + health (dashboard) ---------- */

function store_stats(): array {
    $recs = all_records();
    $s = ['total' => count($recs), 'active' => 0, 'superseded' => 0, 'links' => 0,
          'by_type' => [], 'by_scope' => [], 'by_scope_todos' => [], 'todos' => 0, 'recent' => []];
    $active = [];
    foreach ($recs as $r) {
        $s['links'] += count(rec_ids($r, 'related-to')) + count(rec_ids($r, 'blocked-by'));
        $st = $r['meta']['status'] ?? 'active';
        if ($st === 'active') {
            $s['active']++;
            $t = $r['meta']['type'] ?? '?';
            $sc = $r['meta']['scope'] ?? '?';
            $s['by_type'][$t] = ($s['by_type'][$t] ?? 0) + 1;
            $s['by_scope'][$sc] = ($s['by_scope'][$sc] ?? 0) + 1;
            if ($t === 'todo') { $s['todos']++; $s['by_scope_todos'][$sc] = ($s['by_scope_todos'][$sc] ?? 0) + 1; }
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
    $stg = data_root() . '/staging';
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
        $sess = data_root() . '/staging/sessions.jsonl';
        if (is_file($sess) && filesize($sess) > 0) {
            $hooks = true;
            $hdet = t('active — last capture') . ' ' . date('Y-m-d H:i', filemtime($sess));
        } else {
            $hdet = t('no evidence yet (no captures in staging)');
        }
    }
    $out[] = [t('Claude Code hooks'), $hooks, $hdet];
    // the injection must fit its own budget (below the harness persist/truncation threshold)
    [$isz, $ibud, $ncrit] = injection_stats();
    if ($isz !== null) {
        $out[] = [t('SessionStart injection'), $isz <= $ibud,
                  round($isz / 1024, 1) . ' KB / ' . t('budget') . ' ' . round($ibud / 1024, 1) . " KB · $ncrit " . t('critical rules')];
    }
    $dirty = git_dirty();
    // dirty is not an error: the SessionEnd hook auto-commits the store -> informative gray
    $out[] = [t('Git store'), $dirty === null ? null : ($dirty ? null : true),
              $dirty === null ? t('unknown') : ($dirty ? t('uncommitted — auto-checkpoint at session end') : t('clean'))];
    return $out;
}

// Real size of the injection (root mode = the maximal case) vs its budget + critical rule count.
function injection_stats(): array {
    $py = getenv('MEM_PYTHON') ?: 'python3';
    $hook = proj_root() . '/hooks/session_start.py';
    if (!is_file($hook) || !function_exists('exec')) return [null, 0, 0];
    $bud = (int)(getenv('MEM_INJECT_BUDGET') ?: 8000);
    $root = dirname(proj_root());
    $payload = json_encode(['cwd' => $root, 'source' => 'health']);
    $cmd = 'echo ' . escapeshellarg($payload) . ' | ' . escapeshellarg($py) . ' ' . escapeshellarg($hook) . ' 2>/dev/null';
    $lines = []; $code = 1;
    @exec($cmd, $lines, $code);
    if ($code !== 0) return [null, $bud, 0];
    $outtxt = implode("\n", $lines);
    $ncrit = 0;
    foreach (all_records() as $r) {
        if (($r['meta']['priority'] ?? '') === 'critical' && ($r['meta']['status'] ?? 'active') === 'active') $ncrit++;
    }
    return [strlen($outtxt), $bud, $ncrit];
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
        foreach (all_records() as $r) {
            $files = $r['meta']['files'] ?? '';
            $body = $r['body'] . ($files !== '' ? "\nfiles: " . $files : '');  // file paths searchable (same as mem.py)
            $st->execute([$r['id'], rec_summary($r), $body]);
        }
        $pdo = null;
        rename($tmp, store_dir() . '/.index.db');
        @chmod(store_dir() . '/.index.db', 0666);
        return true;
    } catch (Throwable $e) {
        @unlink($tmp);
        return false;
    }
}

/* ---------- optional semantic layer (mirrors mem.py): embeddings in .embed.db ---------- */

// Embed the query via Ollama (retrieval only). null if Ollama/model unavailable -> keyword-only.
function embed_query(string $q): ?array {
    $q = trim($q);
    if ($q === '') return null;
    $url = (getenv('OLLAMA_URL') ?: 'http://localhost:11434') . '/api/embeddings';
    $model = getenv('MEM_EMBED_MODEL') ?: 'all-minilm';
    $ctx = stream_context_create(['http' => [
        'method' => 'POST', 'header' => "Content-Type: application/json\r\n",
        'content' => json_encode(['model' => $model, 'prompt' => $q]),
        'timeout' => 5, 'ignore_errors' => true,
    ]]);
    $resp = @file_get_contents($url, false, $ctx);
    if ($resp === false) return null;
    $d = json_decode($resp, true);
    $v = $d['embedding'] ?? null;
    return (is_array($v) && $v) ? array_values($v) : null;
}

// id -> vector, from the .embed.db the CLI builds (little-endian float32; {} if none).
function load_embeddings(): array {
    $p = store_dir() . '/.embed.db';
    if (!is_file($p)) return [];
    try {
        $pdo = new PDO('sqlite:' . $p);
        $pdo->setAttribute(PDO::ATTR_ERRMODE, PDO::ERRMODE_EXCEPTION);
        $out = [];
        foreach ($pdo->query('SELECT id, vec FROM emb') as $row) {
            $out[$row['id']] = array_values(unpack('g*', $row['vec']));
        }
        return $out;
    } catch (Throwable $e) {
        return [];
    }
}

function cosine_sim(array $a, array $b): float {
    $n = min(count($a), count($b));
    if (!$n) return 0.0;
    $dot = 0.0; $na = 0.0; $nb = 0.0;
    for ($i = 0; $i < $n; $i++) { $dot += $a[$i] * $b[$i]; $na += $a[$i] * $a[$i]; $nb += $b[$i] * $b[$i]; }
    return ($na && $nb) ? $dot / (sqrt($na) * sqrt($nb)) : 0.0;
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
        $st = $pdo->prepare('SELECT id, bm25(mem) AS s FROM mem WHERE mem MATCH ?');
        $st->execute([$match]);
        $rows = $st->fetchAll(PDO::FETCH_ASSOC);
        if (!$rows) return [];
        // recency nudge (mirrors mem.py): newer wins near-ties, never overrides a stronger match
        $w = (float)(getenv('MEM_RECENCY_WEIGHT') ?: 1.5);
        $ord = [];
        foreach (all_records() as $r) {
            $c = substr($r['meta']['created'] ?? '', 0, 10);
            $ord[$r['id']] = $c !== '' ? strtotime($c) : null;
        }
        $vals = array_filter(array_map(fn($x) => $ord[$x['id']] ?? null, $rows));
        $lo = $vals ? min($vals) : 0; $hi = $vals ? max($vals) : 0; $span = ($hi - $lo) ?: 1;
        usort($rows, function ($a, $b) use ($ord, $lo, $span, $w) {
            $ra = $ord[$a['id']] ? ($ord[$a['id']] - $lo) / $span : 0.0;
            $rb = $ord[$b['id']] ? ($ord[$b['id']] - $lo) / $span : 0.0;
            return ($a['s'] - $w * $ra) <=> ($b['s'] - $w * $rb);
        });
        $ids = array_map(fn($x) => $x['id'], $rows);
        // semantic fusion when an embedder is available (mirrors mem.py hybrid_search); else keyword-only
        $emb = load_embeddings();
        $qv = $emb ? embed_query($q) : null;
        if (!$emb || $qv === null) return $ids;
        $sims = [];
        foreach ($emb as $id => $v) $sims[$id] = cosine_sim($qv, $v);
        $nkw = max(1, count($ids));
        $kw = [];
        foreach ($ids as $n => $id) $kw[$id] = 1.0 - $n / $nkw;
        arsort($sims);
        $cand = array_values(array_unique(array_merge($ids, array_slice(array_keys($sims), 0, 25))));
        usort($cand, fn($a, $b) =>
            (0.5 * ($sims[$b] ?? 0) + 0.5 * ($kw[$b] ?? 0)) <=> (0.5 * ($sims[$a] ?? 0) + 0.5 * ($kw[$a] ?? 0)));
        return $cand;
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

function queue_file(): string { return data_root() . '/staging/queue.jsonl'; }

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
    $root = data_root();
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
    $root = data_root();
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
    $root = data_root();
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
