#!/usr/bin/env python3
"""
l3-graph — Generate an interactive HTML knowledge graph from an L3 database.

Reads tags and relations from any L3-enabled SQLite database and renders
a D3.js force-directed graph in the browser. Nodes are color-coded by URI
type, edges by relation type. Click a node to see its tags and connections.
Type to filter.

Usage:
  l3-graph output.html                    # uses L3_DB_PATH env or ./l3.db
  l3-graph output.html --db path/to/db    # explicit database path
"""

import sqlite3
import json
import os
import sys
import argparse

DEFAULT_DB = os.environ.get("L3_DB_PATH", "l3.db")

HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>L3 Knowledge Graph</title>
<style>
  * { margin:0; padding:0; box-sizing:border-box; }
  body { font-family:-apple-system,sans-serif; background:#1a1a2e; color:#eee; overflow:hidden; height:100vh; }
  #container { display:flex; height:100vh; }
  #graph { flex:1; position:relative; }
  #sidebar { width:320px; background:#16213e; padding:16px; overflow-y:auto; border-left:1px solid #0f3460; }
  #sidebar h2 { font-size:14px; color:#e94560; margin-bottom:8px; }
  #sidebar p { font-size:12px; color:#889; margin-bottom:12px; }
  #info { font-size:13px; line-height:1.6; }
  #info .key { color:#e94560; font-weight:bold; }
  #info .tags { color:#0f3460; }
  .legend { margin-top:16px; padding:12px; background:#0f3460; border-radius:8px; font-size:12px; }
  .legend div { margin:4px 0; }
  .legend .dot { display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:6px; }
  .stats { margin-top:12px; font-size:12px; color:#667; }
  .controls { margin-bottom:12px; }
  .controls input { width:100%; padding:8px; background:#0f3460; border:1px solid #1a1a4e; color:#eee; border-radius:6px; font-size:13px; }
</style>
</head>
<body>
<div id="container">
  <div id="graph"></div>
  <div id="sidebar">
    <h2>🔗 L3 Knowledge Graph</h2>
    <p>Tag + Relation graph visualization</p>
    <div class="controls">
      <input id="search" type="text" placeholder="Filter nodes..." oninput="filterGraph(this.value)">
    </div>
    <div id="info">
      <div class="key" id="selected-name">Click a node</div>
      <div id="selected-uri" style="font-size:11px;color:#667;word-break:break-all;"></div>
      <div id="selected-tags" style="margin-top:6px;"></div>
      <div id="selected-relations" style="margin-top:8px;font-size:12px;"></div>
    </div>
    <div class="legend">
      <div><span class="dot" style="background:#e94560"></span> Memory</div>
      <div><span class="dot" style="background:#0f3460"></span> Resource</div>
      <div><span class="dot" style="background:#e94560"></span> informs</div>
      <div><span class="dot" style="background:#533483"></span> supports</div>
      <div><span class="dot" style="background:#e9c46a"></span> contradicts</div>
      <div><span class="dot" style="background:#2a9d8f"></span> extends</div>
      <div><span class="dot" style="background:#4ecdc4"></span> precedes</div>
      <div><span class="dot" style="background:#ff6b6b"></span> follows</div>
      <div><span class="dot" style="background:#ffe66d"></span> contemporaneous</div>
      <div><span class="dot" style="background:#888"></span> related_to</div>
    </div>
    <div class="stats" id="stats"></div>
  </div>
</div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const DATA = __DATA__;

const width = document.getElementById('graph').clientWidth;
const height = window.innerHeight;

const svg = d3.select('#graph').append('svg')
  .attr('width', width).attr('height', height);

const g = svg.append('g');

// Zoom
svg.call(d3.zoom().scaleExtent([0.1, 4]).on('zoom', (e) => g.attr('transform', e.transform)));

// Arrow markers
const defs = svg.append('defs');
const colors = {'informs':'#e94560','supports':'#533483','contradicts':'#e9c46a','extends':'#2a9d8f','related_to':'#888','precedes':'#4ecdc4','follows':'#ff6b6b','contemporaneous':'#ffe66d'};
for (const [t, c] of Object.entries(colors)) {
  defs.append('marker').attr('id', 'arrow-'+t).attr('viewBox','0 -5 10 10').attr('refX',20).attr('refY',0).attr('markerWidth',6).attr('markerHeight',6).attr('orient','auto')
    .append('path').attr('d','M0,-5L10,0L0,5').attr('fill',c);
}

const simulation = d3.forceSimulation(DATA.nodes)
  .force('link', d3.forceLink(DATA.links).id(d => d.id).distance(100))
  .force('charge', d3.forceManyBody().strength(-200))
  .force('center', d3.forceCenter(width/2, height/2))
  .force('collision', d3.forceCollide(20));

const link = g.append('g').selectAll('line').data(DATA.links).join('line')
  .attr('stroke', d => colors[d.type] || '#888')
  .attr('stroke-width', 1.5)
  .attr('stroke-opacity', 0.6)
  .attr('marker-end', d => 'url(#arrow-'+d.type+')');

const node = g.append('g').selectAll('circle').data(DATA.nodes).join('circle')
  .attr('r', d => Math.max(6, Math.min(16, 8 + (d.tags || []).length * 2)))
  .attr('fill', d => d.id && d.id.includes('user') ? '#e94560' : '#0f3460')
  .attr('stroke', '#fff')
  .attr('stroke-width', 1)
  .style('cursor', 'pointer')
  .on('click', (e, d) => showInfo(d))
  .call(d3.drag()
    .on('start', (e, d) => { if (!e.active) simulation.alphaTarget(0.3).restart(); d.fx = d.x; d.fy = d.y; })
    .on('drag', (e, d) => { d.fx = e.x; d.fy = e.y; })
    .on('end', (e, d) => { if (!e.active) simulation.alphaTarget(0); d.fx = null; d.fy = null; }));

const label = g.append('g').selectAll('text').data(DATA.nodes).join('text')
  .text(d => d.name || d.id.split('/').pop())
  .attr('font-size', '10px')
  .attr('dx', 12)
  .attr('dy', 4)
  .attr('fill', '#aaa');

simulation.on('tick', () => {
  link.attr('x1', d => d.source.x).attr('y1', d => d.source.y)
      .attr('x2', d => d.target.x).attr('y2', d => d.target.y);
  node.attr('cx', d => d.x).attr('cy', d => d.y);
  label.attr('x', d => d.x).attr('y', d => d.y);
});

function showInfo(d) {
  document.getElementById('selected-name').textContent = d.name || d.id;
  document.getElementById('selected-uri').textContent = d.id;
  const tagsDiv = document.getElementById('selected-tags');
  tagsDiv.innerHTML = (d.tags || []).map(t => `<span style="background:#0f3460;padding:2px 6px;border-radius:4px;margin:2px;display:inline-block;font-size:11px">${t}</span>`).join('');
  
  const rels = DATA.links.filter(l => l.source.id === d.id || l.target.id === d.id);
  const relsDiv = document.getElementById('selected-relations');
  relsDiv.innerHTML = '<div style="color:#e94560;margin-bottom:4px">Relations:</div>' + 
    rels.map(l => {
      const other = l.source.id === d.id ? l.target : l.source;
      const dir = l.source.id === d.id ? '→' : '←';
      return `<div style="margin:2px 0">${dir} <b>${l.type}</b> → ${other.name || other.id}</div>`;
    }).join('');
}

function filterGraph(query) {
  const q = query.toLowerCase();
  node.attr('opacity', d => !q || (d.name || '').toLowerCase().includes(q) || (d.tags || []).some(t => t.includes(q)) ? 1 : 0.1);
  link.attr('opacity', d => {
    const s = (d.source.name || '').toLowerCase().includes(q) || (d.source.tags || []).some(t => t.includes(q));
    const t = (d.target.name || '').toLowerCase().includes(q) || (d.target.tags || []).some(t => t.includes(q));
    return !q || s || t ? 0.6 : 0.02;
  });
  label.attr('opacity', d => !q || (d.name || '').toLowerCase().includes(q) ? 1 : 0.1);
}

document.getElementById('stats').innerHTML = 
  `Nodes: ${DATA.nodes.length} | Links: ${DATA.links.length} | Tags: ${new Set(DATA.nodes.flatMap(n => n.tags || [])).size}`;
</script>
</body>
</html>"""


def generate_graph(db_path: str):
    db = sqlite3.connect(db_path)
    db.row_factory = sqlite3.Row

    nodes = {}
    links = []

    # 1. All URIs with tags
    rows = db.execute("""
        SELECT nt.node_uri, t.name as tag
        FROM l3_node_tags nt
        JOIN l3_tags t ON nt.tag_id = t.id
        ORDER BY nt.node_uri
    """).fetchall()

    for r in rows:
        uri = r["node_uri"]
        if uri not in nodes:
            nodes[uri] = {"id": uri, "name": uri.split("/")[-1], "tags": []}
        nodes[uri]["tags"].append(r["tag"])

    # 2. Relations
    rows = db.execute("""
        SELECT source_uri, target_uri, relation_type
        FROM l3_relations
    """).fetchall()

    for r in rows:
        s, t = r["source_uri"], r["target_uri"]
        if s not in nodes:
            nodes[s] = {"id": s, "name": s.split("/")[-1], "tags": []}
        if t not in nodes:
            nodes[t] = {"id": t, "name": t.split("/")[-1], "tags": []}
        links.append({"source": s, "target": t, "type": r["relation_type"]})

    # 3. Include nodes from the nodes table that have L3 tags
    rows = db.execute("""
        SELECT n.uri, n.name
        FROM nodes n
        JOIN l3_node_tags nt ON n.uri = nt.node_uri
        GROUP BY n.uri
    """).fetchall()
    for r in rows:
        uri = r["uri"]
        if uri not in nodes:
            tags = [t["tag"] for t in db.execute(
                "SELECT t.name as tag FROM l3_node_tags nt JOIN l3_tags t ON nt.tag_id = t.id WHERE nt.node_uri = ?",
                (uri,)
            ).fetchall()]
            nodes[uri] = {"id": uri, "name": r["name"] or uri.split("/")[-1], "tags": tags}

    db.close()

    data = {
        "nodes": list(nodes.values()),
        "links": links,
    }

    return data


def render_html(data: dict) -> str:
    return HTML_TEMPLATE.replace("__DATA__", json.dumps(data))


def main():
    parser = argparse.ArgumentParser(description="Generate interactive HTML knowledge graph from L3 database")
    parser.add_argument("output", help="Output HTML file path")
    parser.add_argument("--db", default=DEFAULT_DB, help="SQLite database path (default: L3_DB_PATH env or ./l3.db)")

    args = parser.parse_args()

    data = generate_graph(args.db)
    html = render_html(data)

    os.makedirs(os.path.dirname(os.path.abspath(args.output)) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        f.write(html)

    print(f"✓ Graph saved to {args.output}")
    print(f"  Nodes: {len(data['nodes'])} | Links: {len(data['links'])}")
    print(f"  {os.path.getsize(args.output) / 1024:.1f} KB")
    print(f"  Open: file://{os.path.abspath(args.output)}")


if __name__ == "__main__":
    main()
