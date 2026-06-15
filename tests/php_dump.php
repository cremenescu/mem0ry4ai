<?php
// SPDX-License-Identifier: GPL-2.0-or-later
// Conformance helper: dump store/*.md as JSON in the SAME shape as `mem.py list --status all --json`.
// Used by tests/conformance.py to assert the PHP parser (web/lib.php) matches the Python one (mem.py).
require __DIR__ . '/../web/lib.php';

$out = [];
foreach (all_records() as $r) {
    $m = $r['meta'];
    $parts = array_map('trim', explode('·', $r['title']));
    $summary = $parts[2] ?? $r['title'];
    $out[] = [
        'id'            => $r['id'],
        'type'          => $m['type'] ?? null,
        'scope'         => $m['scope'] ?? null,
        'summary'       => $summary,
        'status'        => $m['status'] ?? 'active',
        'confidence'    => $m['confidence'] ?? null,
        'source'        => $m['source'] ?? null,
        'created'       => $m['created'] ?? null,
        'updated'       => $m['updated'] ?? null,
        'superseded_by' => $m['superseded-by'] ?? null,
        'priority'      => $m['priority'] ?? null,
        'related_to'    => $m['related-to'] ?? null,
        'blocked_by'    => $m['blocked-by'] ?? null,
        'files'         => $m['files'] ?? null,
        'invalidated'   => $m['invalidated'] ?? null,
        'invalid_reason'=> $m['invalid-reason'] ?? null,
        'body'          => $r['body'],
    ];
}
echo json_encode($out, JSON_UNESCAPED_UNICODE | JSON_UNESCAPED_SLASHES);
