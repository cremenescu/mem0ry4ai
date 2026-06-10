<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// mem0ry4ai — live poll: the client asks "did the store change?" every few seconds.
// Fast answer (file stat only) when nothing changed; HTML fragments when it did.
// NO session (read-only; must not hold the session lock of other requests).
declare(strict_types=1);
require __DIR__ . '/lib.php';

header('Content-Type: application/json; charset=utf-8');
header('Cache-Control: no-store');

$clientVer = trim($_GET['ver'] ?? '');
$ver = store_version();

if ($clientVer !== '' && $clientVer === $ver) {
    echo json_encode(['changed' => false, 'ver' => $ver]);
    exit;
}

// changed (or first call): send the dashboard fragments + counters
$stats = store_stats();
echo json_encode([
    'changed'    => $clientVer !== '',          // first call (no ver) just sets the version
    'ver'        => $ver,
    'active'     => $stats['active'],
    'queue'      => count(queue_pending()),
    'cards_html' => render_dash_cards($stats),
    'recent_html'=> render_recent_list($stats),
], JSON_UNESCAPED_UNICODE);
