#!/usr/bin/env python3
"""
l3 — A tag-based knowledge graph layer over SQLite.

Tags + Relations + Graph traversal for any SQLite-backed knowledge base.
Drop-in layer: creates l3_tags, l3_node_tags, l3_relations tables alongside
your existing data, leaving it untouched.

Usage:
  # Tag a node (any URI)
  l3 tag add memory://my-project/concept "gwas"
  l3 tag remove memory://my-project/concept "gwas"

  # Relate two nodes
  l3 relate memory://source memory://target informs
  l3 unrelate memory://source memory://target

  # Search by tag
  l3 search tag gwas
  l3 search all

  # Graph traversal from a node
  l3 trace memory://my-project/concept

  # Stats
  l3 stats
"""

import sqlite3
import sys
import os
from datetime import datetime

DEFAULT_DB = os.environ.get("L3_DB_PATH", "l3.db")


SQL_CREATE = """
CREATE TABLE IF NOT EXISTS l3_tags (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT UNIQUE NOT NULL,
    created_at TEXT DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS l3_node_tags (
    node_uri TEXT NOT NULL,
    tag_id INTEGER NOT NULL REFERENCES l3_tags(id),
    created_at TEXT DEFAULT (datetime('now')),
    PRIMARY KEY (node_uri, tag_id)
);

CREATE INDEX IF NOT EXISTS idx_l3_node_tags_tag ON l3_node_tags(tag_id);
CREATE INDEX IF NOT EXISTS idx_l3_node_tags_node ON l3_node_tags(node_uri);

CREATE TABLE IF NOT EXISTS l3_relations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_uri TEXT NOT NULL,
    target_uri TEXT NOT NULL,
    relation_type TEXT NOT NULL DEFAULT 'related_to',
    weight REAL DEFAULT 1.0,
    created_at TEXT DEFAULT (datetime('now')),
    UNIQUE(source_uri, target_uri, relation_type)
);

CREATE INDEX IF NOT EXISTS idx_l3_relations_source ON l3_relations(source_uri);
CREATE INDEX IF NOT EXISTS idx_l3_relations_target ON l3_relations(target_uri);
CREATE INDEX IF NOT EXISTS idx_l3_relations_type ON l3_relations(relation_type);
"""


def get_db(path: str):
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.executescript(SQL_CREATE)
    db.commit()
    return db


def cmd_tag_add(db, uri: str, tag: str):
    tag = tag.strip().lower().replace(" ", "-")
    db.execute("INSERT OR IGNORE INTO l3_tags (name) VALUES (?)", (tag,))
    tag_id = db.execute("SELECT id FROM l3_tags WHERE name = ?", (tag,)).fetchone()["id"]
    db.execute("INSERT OR IGNORE INTO l3_node_tags (node_uri, tag_id) VALUES (?, ?)", (uri, tag_id))
    db.commit()
    print(f"✓ Tag '{tag}' added to {uri}")


def cmd_tag_remove(db, uri: str, tag: str):
    tag = tag.strip().lower().replace(" ", "-")
    tag_row = db.execute("SELECT id FROM l3_tags WHERE name = ?", (tag,)).fetchone()
    if tag_row:
        db.execute("DELETE FROM l3_node_tags WHERE node_uri = ? AND tag_id = ?", (uri, tag_row["id"]))
        db.commit()
        print(f"✓ Tag '{tag}' removed from {uri}")
    else:
        print(f"! Tag '{tag}' not found")


def cmd_relate(db, source: str, target: str, rel_type: str = "related_to"):
    db.execute(
        "INSERT OR REPLACE INTO l3_relations (source_uri, target_uri, relation_type) VALUES (?, ?, ?)",
        (source, target, rel_type),
    )
    db.commit()
    print(f"✓ {source} --[{rel_type}]--> {target}")


def cmd_unrelate(db, source: str, target: str, rel_type: str = None):
    if rel_type:
        db.execute(
            "DELETE FROM l3_relations WHERE source_uri = ? AND target_uri = ? AND relation_type = ?",
            (source, target, rel_type),
        )
    else:
        db.execute(
            "DELETE FROM l3_relations WHERE source_uri = ? AND target_uri = ?",
            (source, target),
        )
    db.commit()
    print(f"✓ Relation removed")


def cmd_search_tag(db, tag: str):
    tag = tag.strip().lower().replace(" ", "-")
    rows = db.execute(
        """SELECT nt.node_uri, t.name as tag
           FROM l3_node_tags nt
           JOIN l3_tags t ON nt.tag_id = t.id
           WHERE t.name LIKE ?
           ORDER BY nt.node_uri""",
        (f"%{tag}%",),
    ).fetchall()
    if rows:
        print(f"\nNodes tagged with '{tag}':")
        for r in rows:
            print(f"  {r['node_uri']}  [{r['tag']}]")
    else:
        print(f"  No nodes found with tag '{tag}'")
    print(f"  Total: {len(rows)}")


def cmd_search_tag_all(db):
    rows = db.execute(
        """SELECT t.name, COUNT(nt.node_uri) as cnt
           FROM l3_tags t
           LEFT JOIN l3_node_tags nt ON t.id = nt.tag_id
           GROUP BY t.id, t.name
           ORDER BY cnt DESC"""
    ).fetchall()
    if rows:
        print("\nAll tags:")
        for r in rows:
            print(f"  {r['name']}: {r['cnt']} nodes")
    print(f"  Total: {len(rows)} tags")


def cmd_trace(db, uri: str, depth: int = 2):
    """Graph traversal from a URI — shows what it relates to and what relates to it."""
    visited = set()
    _trace_recursive(db, uri, 0, depth, visited)


def _trace_recursive(db, uri: str, current_depth: int, max_depth: int, visited: set):
    if current_depth > max_depth or uri in visited:
        return
    visited.add(uri)
    indent = "  " * current_depth
    prefix = "→" if current_depth > 0 else "●"
    tags = db.execute(
        """SELECT t.name FROM l3_node_tags nt JOIN l3_tags t ON nt.tag_id = t.id WHERE nt.node_uri = ?""",
        (uri,),
    ).fetchall()
    tag_str = f"  [{', '.join(r['name'] for r in tags)}]" if tags else ""
    print(f"{indent}{prefix} {uri}{tag_str}")

    outgoing = db.execute(
        "SELECT target_uri, relation_type FROM l3_relations WHERE source_uri = ?",
        (uri,),
    ).fetchall()
    for r in outgoing:
        _trace_recursive(db, r["target_uri"], current_depth + 1, max_depth, visited)
        if current_depth + 1 <= max_depth:
            print(f"{indent}  └─[{r['relation_type']}]→")

    incoming = db.execute(
        "SELECT source_uri, relation_type FROM l3_relations WHERE target_uri = ?",
        (uri,),
    ).fetchall()
    for r in incoming:
        _trace_recursive(db, r["source_uri"], current_depth + 1, max_depth, visited)
        if current_depth + 1 <= max_depth:
            print(f"{indent}  ←[{r['relation_type']}]─")


def cmd_stats(db):
    tags = db.execute("SELECT COUNT(*) as c FROM l3_tags").fetchone()["c"]
    node_tags = db.execute("SELECT COUNT(*) as c FROM l3_node_tags").fetchone()["c"]
    relations = db.execute("SELECT COUNT(*) as c FROM l3_relations").fetchone()["c"]

    top_tags = db.execute(
        """SELECT t.name, COUNT(nt.node_uri) as cnt
           FROM l3_tags t JOIN l3_node_tags nt ON t.id = nt.tag_id
           GROUP BY t.id ORDER BY cnt DESC LIMIT 10"""
    ).fetchall()

    rel_types = db.execute(
        """SELECT relation_type, COUNT(*) as cnt
           FROM l3_relations GROUP BY relation_type ORDER BY cnt DESC"""
    ).fetchall()

    print(f"\nL3 Statistics")
    print(f"  Tags:        {tags}")
    print(f"  Node-Tag:    {node_tags}")
    print(f"  Relations:   {relations}")
    if top_tags:
        print(f"\n  Top tags:")
        for r in top_tags:
            print(f"    {r['name']}: {r['cnt']}")
    if rel_types:
        print(f"\n  Relation types:")
        for r in rel_types:
            print(f"    {r['relation_type']}: {r['cnt']}")


def main():
    import argparse
    parser = argparse.ArgumentParser(description="L3 — Tag-based knowledge graph layer over SQLite")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path (default: L3_DB_PATH env or ./l3.db)")

    sub = parser.add_subparsers(dest="command")

    # tag
    p_tag = sub.add_parser("tag", help="Add/remove tags on a node")
    p_tag.add_argument("action", choices=["add", "remove"])
    p_tag.add_argument("uri", help="Node URI to tag")
    p_tag.add_argument("tag", help="Tag name")

    # relate
    p_rel = sub.add_parser("relate", help="Create a relation between two nodes")
    p_rel.add_argument("source", help="Source URI")
    p_rel.add_argument("target", help="Target URI")
    p_rel.add_argument("relation", nargs="?", default="related_to", help="Relation type (default: related_to)")

    # unrelate
    p_unrel = sub.add_parser("unrelate", help="Remove a relation")
    p_unrel.add_argument("source", help="Source URI")
    p_unrel.add_argument("target", help="Target URI")
    p_unrel.add_argument("relation", nargs="?", default=None, help="Relation type (optional)")

    # search
    p_search = sub.add_parser("search", help="Search by tag or list all")
    p_search.add_argument("mode", choices=["tag", "all"])
    p_search.add_argument("query", nargs="?", default="", help="Tag name to search (required for mode=tag)")

    # trace
    p_trace = sub.add_parser("trace", help="Graph traversal from a node")
    p_trace.add_argument("uri", help="Node URI to start traversal from")
    p_trace.add_argument("--depth", type=int, default=2, help="Traversal depth (default: 2)")

    # stats
    sub.add_parser("stats", help="Show database statistics")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        return

    db = get_db(args.db)

    if args.command == "tag":
        if args.action == "add":
            cmd_tag_add(db, args.uri, args.tag)
        elif args.action == "remove":
            cmd_tag_remove(db, args.uri, args.tag)

    elif args.command == "relate":
        cmd_relate(db, args.source, args.target, args.relation)

    elif args.command == "unrelate":
        cmd_unrelate(db, args.source, args.target, args.relation)

    elif args.command == "search":
        if args.mode == "tag":
            if not args.query:
                print("Error: query is required for 'search tag'")
                return
            cmd_search_tag(db, args.query)
        elif args.mode == "all":
            cmd_search_tag_all(db)

    elif args.command == "trace":
        cmd_trace(db, args.uri, args.depth)

    elif args.command == "stats":
        cmd_stats(db)

    db.close()


if __name__ == "__main__":
    main()
