#!/usr/bin/env python3
"""
weathermap_to_custom_map.py  —  Convert PHP Weathermap .conf files to LibreNMS Custom Maps.

USAGE
  Single map:
    weathermap_to_custom_map.py  <config.conf>  [options]

  Batch (entire configs directory, sub-map links resolved automatically):
    weathermap_to_custom_map.py  <configs_dir>  [options]

OPTIONS
  --output sql|direct|both
      sql     Write SQL INSERT statements to stdout or --sql-file (default).
      direct  Insert directly into the LibreNMS database.
      both    Do both.

  --sql-file FILE
      Write SQL to FILE instead of stdout.

  --reset-table-ids
      Batch mode only.  Before inserting, TRUNCATE custom_maps,
      custom_map_nodes and custom_map_edges, resetting all auto-increment
      counters to 1.  In SQL output mode this prepends TRUNCATE statements.

  --skip-circular-deps
      Batch mode only.  When circular map dependencies are detected (map A
      links to map B and map B links back to map A), break the cycle by
      nulling out the back-link rather than aborting.  A warning is printed
      for each link that is dropped.  Nodes affected will still appear on
      the map but will not navigate to their target map when clicked.

  --force
      Batch mode.  Implies --reset-table-ids and --skip-circular-deps.
      Prints a warning banner and proceeds regardless of dependency errors.
      Use when you just want everything imported and will fix links manually.

  --librenms-path PATH
      LibreNMS root directory containing config.php.  Auto-detected from
      the script location or the conf file location if omitted.

  --db-host HOST | --db-user USER | --db-pass PASS | --db-name DB
      Override individual database credentials read from config.php.

  --map-name NAME
      Override the map name (single-map mode only; default: TITLE in conf).

  --menu-group NAME
      Assign all converted maps to this menu group.

  --no-icons
      Use plain labelled box nodes instead of device-icon image nodes.

  --node-label-offset
      Include the label_offset_y column in node INSERT statements.  This
      column is used to vertically reposition node labels (e.g. above the
      node icon) and requires the corresponding schema change to
      custom_map_nodes (ALTER TABLE ADD COLUMN label_offset_y INT NULL).
      Omit this flag when importing into a stock upstream LibreNMS instance
      that does not yet have that column.

      NOTE: once the schema change is accepted upstream, change the default
      for the node_label_offset parameter in generate_sql() and
      _insert_one_map() from False to True so that this feature is enabled
      automatically without needing the flag.

BATCH MODE
  When the positional argument is a directory the script scans it for *.conf
  files, parses them all, detects cross-map links by matching each node's
  INFOURL against the basenames of all discovered conf files, then generates
  or inserts all maps in dependency order so that linked_custom_map_id foreign
  keys are populated correctly.

  Cross-map link detection
    A node whose INFOURL ends with  /SomeMap.html  will be linked to
    SomeMap if SomeMap.conf exists in the same directory.  The node is
    styled as circularImage automatically.

  --reset-table-ids (batch mode)
    Truncates all three custom-map tables and resets their auto-increment
    counters before inserting.  Useful for a clean full re-import.
    In SQL mode this prepends TRUNCATE statements to the output file.

  --skip-circular-deps (batch mode)
    Breaks circular map-link dependencies by nulling out the back-edge
    rather than aborting.  Affected nodes get linked_custom_map_id = NULL.

  --force (batch mode)
    Implies --reset-table-ids + --skip-circular-deps.  Prints a warning
    and proceeds regardless of dependency errors.

EXAMPLES
  # Single map → SQL on stdout
  weathermap_to_custom_map.py LreaCore.conf

  # Single map → insert into database
  weathermap_to_custom_map.py LreaCore.conf --output direct

  # Batch: convert all maps in the Weathermap configs directory
  weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \\
      --output direct

  # Batch: wipe existing custom maps first, then re-import everything
  weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \\
      --output direct --reset-table-ids

  # Batch: reset tables + ignore circular dependencies + other errors
  weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \\
      --output direct --force

  # Batch: generate SQL to review before applying
  weathermap_to_custom_map.py /opt/librenms/html/plugins/Weathermap/configs \\
      --sql-file all_maps.sql
  mysql -u librenms -p librenms < all_maps.sql
"""

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Dict, List, Optional


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class WMNode:
    name: str
    x_pos: int = 0
    y_pos: int = 0
    device_id: Optional[int] = None
    label: Optional[str] = None   # None means "use node name"
    icon: Optional[str] = None    # icon filename from Weathermap conf
    infourl: Optional[str] = None          # raw INFOURL directive value
    linked_map_name: Optional[str] = None  # resolved stem of linked conf (batch mode)


@dataclass
class WMLink:
    name: str
    node1: str = ''
    node2: str = ''
    port_id: Optional[int] = None
    bandwidth: Optional[str] = None
    incomment: Optional[str] = None
    width: Optional[float] = None   # link line width from WIDTH directive


@dataclass
class WMScale:
    low: float
    high: float
    r1: int
    g1: int
    b1: int
    r2: Optional[int] = None
    g2: Optional[int] = None
    b2: Optional[int] = None


@dataclass
class WMConfig:
    title: str = 'Converted Map'
    width: int = 1800
    height: int = 800
    scales: list = field(default_factory=list)
    nodes: dict = field(default_factory=dict)   # name -> WMNode
    links: list = field(default_factory=list)
    keypos_x: int = -1    # legend x position (-1 = hidden)
    keypos_y: int = -1    # legend y position
    default_link_width: Optional[float] = None  # from LINK DEFAULT WIDTH


# ---------------------------------------------------------------------------
# Parser
# ---------------------------------------------------------------------------

def parse_node_name(token: str) -> str:
    """Strip compass/pixel offsets from a NODES token, e.g. 'node1:N' -> 'node1'."""
    return token.split(':')[0]


def parse_weathermap_conf(path: str) -> WMConfig:
    cfg = WMConfig()
    current_section = 'global'
    current_node: Optional[WMNode] = None
    current_link: Optional[WMLink] = None

    with open(path, 'r') as f:
        for raw_line in f:
            # Strip comments and whitespace
            line = raw_line.split('#')[0].strip()
            if not line:
                continue

            parts = line.split(None, 1)
            if not parts:
                continue
            keyword = parts[0].upper()
            rest = parts[1] if len(parts) > 1 else ''

            # --- Section headers ---
            if keyword == 'NODE':
                if current_node and current_node.name != 'DEFAULT':
                    cfg.nodes[current_node.name] = current_node
                name = rest.strip()
                if name == 'DEFAULT':
                    current_node = None
                    current_section = 'node_default'
                else:
                    current_node = WMNode(name=name)
                    current_section = 'node'
                current_link = None
                continue

            if keyword == 'LINK':
                if current_node and current_node.name != 'DEFAULT':
                    cfg.nodes[current_node.name] = current_node
                current_node = None
                if current_link:
                    cfg.links.append(current_link)
                name = rest.strip()
                if name == 'DEFAULT':
                    current_link = None
                    current_section = 'link_default'
                else:
                    current_link = WMLink(name=name)
                    current_section = 'link'
                continue

            # --- Global directives ---
            if current_section == 'global':
                if keyword == 'TITLE':
                    cfg.title = rest.strip()
                elif keyword == 'WIDTH':
                    try:
                        cfg.width = int(rest.strip())
                    except ValueError:
                        pass
                elif keyword == 'HEIGHT':
                    try:
                        cfg.height = int(rest.strip())
                    except ValueError:
                        pass
                elif keyword == 'SCALE':
                    _parse_scale(rest, cfg)
                elif keyword == 'KEYPOS':
                    # KEYPOS DEFAULT x y [Title]  or  KEYPOS x y [Title]
                    kparts = rest.split()
                    start = 0
                    if kparts and not kparts[0].lstrip('-').isdigit():
                        start = 1  # skip named scale (e.g. DEFAULT)
                    if len(kparts) - start >= 2:
                        try:
                            cfg.keypos_x = int(kparts[start])
                            cfg.keypos_y = int(kparts[start + 1])
                        except ValueError:
                            pass

            # --- NODE DEFAULT (parse default icon for reference) ---
            elif current_section == 'node_default':
                pass  # Nothing needed from NODE DEFAULT for our purposes

            # --- NODE ---
            elif current_section == 'node' and current_node:
                if keyword == 'POSITION':
                    coords = rest.split()
                    if len(coords) >= 2:
                        try:
                            current_node.x_pos = int(coords[0])
                            current_node.y_pos = int(coords[1])
                        except ValueError:
                            pass
                elif keyword == 'LABEL':
                    current_node.label = rest.strip()
                elif keyword == 'ICON':
                    # ICON 100 35 images/foo.png  or  ICON images/foo.png
                    icon_parts = rest.strip().split()
                    current_node.icon = icon_parts[-1]
                elif keyword == 'INFOURL':
                    current_node.infourl = rest.strip()
                elif keyword == 'SET':
                    set_parts = rest.split(None, 1)
                    if len(set_parts) == 2 and set_parts[0].lower() == 'device_id':
                        try:
                            current_node.device_id = int(set_parts[1].strip())
                        except ValueError:
                            pass

            # --- LINK DEFAULT ---
            elif current_section == 'link_default':
                if keyword == 'WIDTH':
                    try:
                        cfg.default_link_width = float(rest.strip())
                    except ValueError:
                        pass

            # --- LINK ---
            elif current_section == 'link' and current_link:
                if keyword == 'NODES':
                    node_tokens = rest.split()
                    if len(node_tokens) >= 2:
                        current_link.node1 = parse_node_name(node_tokens[0])
                        current_link.node2 = parse_node_name(node_tokens[1])
                elif keyword == 'TARGET':
                    # Extract port_id from e.g. ./host.router/port-id1917.rrd:INOCTETS:OUTOCTETS
                    m = re.search(r'port-id(\d+)\.rrd', rest, re.IGNORECASE)
                    if m:
                        current_link.port_id = int(m.group(1))
                elif keyword == 'BANDWIDTH':
                    current_link.bandwidth = rest.strip()
                elif keyword == 'INCOMMENT':
                    current_link.incomment = rest.strip()
                elif keyword == 'WIDTH':
                    try:
                        current_link.width = float(rest.strip())
                    except ValueError:
                        pass

    # Flush last items
    if current_node and current_node.name != 'DEFAULT':
        cfg.nodes[current_node.name] = current_node
    if current_link:
        cfg.links.append(current_link)

    # Apply default label: use node name (same as Weathermap's {node:this:name} template)
    for node in cfg.nodes.values():
        if node.label is None or '{node:this:' in node.label:
            node.label = node.name

    return cfg


def _parse_scale(rest: str, cfg: WMConfig) -> None:
    """Parse a SCALE line (after the SCALE keyword and optional scale name)."""
    parts = rest.split()
    idx = 0
    if parts and not re.match(r'^[\d.]+$', parts[0]):
        idx = 1  # skip scale name (e.g. DEFAULT)
    if len(parts) - idx < 5:
        return
    try:
        low = float(parts[idx])
        high = float(parts[idx + 1])
        r1, g1, b1 = int(parts[idx + 2]), int(parts[idx + 3]), int(parts[idx + 4])
    except (ValueError, IndexError):
        return

    scale = WMScale(low=low, high=high, r1=r1, g1=g1, b1=b1)
    if len(parts) - idx >= 8:
        try:
            scale.r2 = int(parts[idx + 5])
            scale.g2 = int(parts[idx + 6])
            scale.b2 = int(parts[idx + 7])
        except ValueError:
            pass
    cfg.scales.append(scale)


# ---------------------------------------------------------------------------
# Legend colour mapping
# ---------------------------------------------------------------------------

def rgb_to_hex(r: int, g: int, b: int) -> str:
    return '#{:02X}{:02X}{:02X}'.format(r, g, b)


def build_legend_colours(scales: list) -> dict:
    """
    Convert Weathermap SCALE entries to legend_colours dict for Custom Maps.

    Custom Maps' fixedColour() picks the colour whose threshold is <= utilisation
    percentage. Weathermap scales define colour gradients per band (low→high).

    Strategy: use the END colour (r2,g2,b2) of each band at the LOW threshold.
    This means a band's representative colour is shown throughout that band,
    avoiding the common case where the 0% threshold is white (the gradient start)
    which makes low-utilisation links invisible.

    Example (LreaCore.conf):
      SCALE 0  33 255 255 255  0 255 0   → at 0%:  green (#00FF00) — end of band
      SCALE 33 66   0 255   0  0   0 255 → at 33%: blue  (#0000FF) — end of band
      SCALE 66 100  0   0 255 255   0  0 → at 66%: red   (#FF0000) — end of band
    """
    colours = {
        '-2': '#8B0000',   # device down
        '-1': '#000000',   # link down / invalid
    }

    for scale in scales:
        low_key = str(int(scale.low)) if scale.low == int(scale.low) else str(scale.low)

        # Use the END colour (r2,g2,b2) at the low threshold when available.
        # This makes all utilisation in the band show as the band's "final" colour
        # rather than the gradient start, which may be white or very pale.
        if scale.r2 is not None:
            hex_colour = rgb_to_hex(scale.r2, scale.g2, scale.b2)
        else:
            hex_colour = rgb_to_hex(scale.r1, scale.g1, scale.b1)

        colours[low_key] = hex_colour

    if '0' not in colours:
        colours['0'] = '#00FF00'

    return colours


# ---------------------------------------------------------------------------
# Node style helper
# ---------------------------------------------------------------------------

def node_style_and_image(node: WMNode, use_icons: bool) -> tuple:
    """
    Return (vis.js style, image filename) for a node.

    - device nodes (device_id set): 'image' style — JS auto-loads device->icon
    - non-device nodes:             'circularImage' + 'gtm.svg' — map/external node
    - use_icons=False:              'box' with no image (plain labelled box)

    Note: linked_custom_map_id is not resolved here (the linked map may not
    exist yet); the node is styled as a map node but the link is left NULL.

    The Custom Map JS automatically sets font.background = "#FFFFFF" for all
    non-box/ellipse node types (including circularImage), so the label text
    is already drawn on a white background for readability.
    """
    if not use_icons:
        return 'box', None
    if node.device_id is not None:
        return 'image', None           # device icon comes from device->icon at runtime
    return 'circularImage', 'gtm.svg'  # map/external node placeholder


def node_size(style: str) -> int:
    """Return vis.js node size for a given style."""
    if style == 'circularImage':
        return 10   # gtm.svg map nodes are small markers
    return 25       # default for image (device icon) and box


def node_colours(style: str) -> tuple:
    """Return (colour_bg, colour_bdr) for a node style.

    circularImage (map/external nodes): beige/yellow so the label background
    set by custom-js.blade.php is visible on white or light canvases.
    """
    if style == 'circularImage':
        return '#FFFFCC', '#CC9900'   # light yellow bg, gold border
    return '#D2E5FF', '#2B7CE9'       # LibreNMS default light blue


def node_label_stroke_colour(style: str) -> Optional[str]:
    """Return label_stroke_colour (text halo) for a node style, or None for box nodes.

    A white stroke around the label text makes it legible on any canvas colour.
    Box/text nodes have their own shape background, so no stroke is needed.
    """
    if style in ('image', 'circularImage'):
        return '#FFFFFF'   # white halo — readable on any map background
    return None            # box/other: label is inside node shape, no halo needed


# ---------------------------------------------------------------------------
# Default JSON values (matching CustomMapController defaults)
# ---------------------------------------------------------------------------

DEFAULT_OPTIONS = {
    'interaction': {
        'dragNodes': False,
        'dragView': False,
        'zoomView': False,
    },
    'manipulation': {
        'enabled': False,
    },
    'physics': {
        'enabled': False,
    },
}

DEFAULT_NEWNODECONFIG = {
    'borderWidth': 1,
    'color': {
        'border': '#2B7CE9',
        'background': '#D2E5FF',
    },
    'font': {
        'color': '#343434',
        'size': 14,
        'face': 'arial',
    },
    'icon': {},
    'label': True,
    'shape': 'box',
    'size': 25,
}

DEFAULT_NEWEDGECONFIG = {
    'arrows': {
        'to': {
            'enabled': True,
        },
    },
    'smooth': {
        'type': 'dynamic',
    },
    'font': {
        'color': '#343434',
        'size': 12,
        'face': 'arial',
        'align': 'horizontal',
    },
    'label': True,
}


# ---------------------------------------------------------------------------
# SQL generation
# ---------------------------------------------------------------------------

def sql_escape(value) -> str:
    """Minimal SQL string escaping."""
    if value is None:
        return 'NULL'
    s = str(value)
    s = s.replace('\\', '\\\\').replace("'", "\\'")
    return "'" + s + "'"


def _link_fixed_width(link: WMLink, cfg: WMConfig) -> str:
    """Return the fixed_width SQL value for an edge (NULL lets speedWidth() take over)."""
    w = link.width if link.width is not None else cfg.default_link_width
    if w is not None:
        return str(float(w))
    return 'NULL'


def _node_var(node_name: str) -> str:
    """Return the SQL user variable name for a node."""
    return '@node_' + re.sub(r'[^a-zA-Z0-9_]', '_', node_name)


def _map_var_for_stem(stem: str) -> str:
    """Return the SQL user variable name for a map identified by its conf stem."""
    return '@map_{}_id'.format(re.sub(r'[^a-zA-Z0-9_]', '_', stem))


def generate_sql(cfg: WMConfig, map_name: str, use_icons: bool = True,
                 menu_group: Optional[str] = None,
                 map_var: str = '@map_id',
                 all_map_vars: Optional[Dict[str, str]] = None,
                 node_label_offset: bool = False) -> str:
    """
    Generate SQL INSERT statements for one map.

    map_var       — SQL variable to SET after the map INSERT (default '@map_id').
                    In batch mode pass a unique variable per map, e.g. '@map_LreaCore_id'.
    all_map_vars  — {stem: sql_var} dict for resolving linked_custom_map_id on nodes.
                    Only needed in batch mode.
    """
    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    legend_colours = build_legend_colours(cfg.scales)
    # legend_steps = count of non-negative numeric keys (shown as colour swatches)
    legend_steps = sum(1 for k in legend_colours if k.lstrip('-').isdigit() and int(k) >= 0)

    menu_group_sql = sql_escape(menu_group) if menu_group else 'NULL'

    lines = [
        '-- Generated by weathermap_to_custom_map.py',
        '-- Source map: {}'.format(map_name),
        '',
        'SET NAMES utf8mb4;',
        '',
        '-- ----------------------------------------------------------------',
        '-- custom_maps',
        '-- ----------------------------------------------------------------',
        'INSERT INTO `custom_maps` (',
        '  `name`, `width`, `height`, `menu_group`,',
        '  `node_align`, `reverse_arrows`, `edge_separation`,',
        '  `legend_x`, `legend_y`, `legend_steps`, `legend_font_size`,',
        '  `legend_hide_invalid`, `legend_hide_overspeed`, `legend_colours`,',
        '  `background_type`, `background_data`,',
        '  `options`, `newnodeconfig`, `newedgeconfig`,',
        '  `created_at`, `updated_at`',
        ') VALUES (',
        '  {name}, {width}, {height}, {menu_group},'.format(
            name=sql_escape(map_name),
            width=sql_escape('{}px'.format(cfg.width)),
            height=sql_escape('{}px'.format(cfg.height)),
            menu_group=menu_group_sql,
        ),
        '  10, 0, 0,',
        '  {lx}, {ly}, {steps}, 14,'.format(
            lx=cfg.keypos_x, ly=cfg.keypos_y, steps=legend_steps,
        ),
        '  0, 0, {},'.format(sql_escape(json.dumps(legend_colours))),
        "  'none', NULL,",
        '  {options}, {newnodeconfig}, {newedgeconfig},'.format(
            options=sql_escape(json.dumps(DEFAULT_OPTIONS)),
            newnodeconfig=sql_escape(json.dumps(DEFAULT_NEWNODECONFIG)),
            newedgeconfig=sql_escape(json.dumps(DEFAULT_NEWEDGECONFIG)),
        ),
        '  {now}, {now}'.format(now=sql_escape(now)),
        ');',
        'SET {} = LAST_INSERT_ID();'.format(map_var),
        '',
        '-- ----------------------------------------------------------------',
        '-- custom_map_nodes',
        '-- ----------------------------------------------------------------',
    ]

    for node in cfg.nodes.values():
        var_name = _node_var(node.name)
        # Use a scalar subquery so an unknown device_id stores NULL rather than
        # failing the FK constraint (same pattern as port_id on edges).
        if node.device_id is not None:
            device_id_sql = '(SELECT `device_id` FROM `devices` WHERE `device_id` = {} LIMIT 1)'.format(node.device_id)
        else:
            device_id_sql = 'NULL'
        label = (node.label or node.name)[:50]
        style, image = node_style_and_image(node, use_icons)

        lhighlight = node_label_stroke_colour(style)

        # Resolve linked_custom_map_id for map-link nodes (batch mode)
        linked_id_sql = 'NULL'
        if node.linked_map_name and all_map_vars and node.linked_map_name in all_map_vars:
            linked_id_sql = all_map_vars[node.linked_map_name]

        lines += [
            '-- Node: {}'.format(node.name),
            'INSERT INTO `custom_map_nodes` (',
            '  `custom_map_id`, `device_id`, `linked_custom_map_id`, `label`, `style`, `icon`, `image`,',
            '  `size`, `border_width`, `text_face`, `text_size`, `text_colour`,',
            '  `label_stroke_colour`{},'.format(', `label_offset_y`' if node_label_offset else ''),
            '  `colour_bg`, `colour_bdr`, `x_pos`, `y_pos`,',
            '  `created_at`, `updated_at`',
            ') VALUES (',
            '  {map_var}, {device_id}, {linked_id}, {label}, {style}, NULL, {image},'.format(
                map_var=map_var,
                device_id=device_id_sql,
                linked_id=linked_id_sql,
                label=sql_escape(label),
                style=sql_escape(style),
                image=sql_escape(image or ''),
            ),
            '  {size}, 1, {face}, 14, {tc},'.format(
                size=node_size(style),
                face=sql_escape('arial'),
                tc=sql_escape('#343434'),
            ),
            '  {lhighlight}{offset},'.format(
                lhighlight=sql_escape(lhighlight) if lhighlight else 'NULL',
                offset=', NULL' if node_label_offset else '',
            ),
            '  {bg}, {bdr}, {x}, {y},'.format(
                bg=sql_escape(node_colours(style)[0]),
                bdr=sql_escape(node_colours(style)[1]),
                x=node.x_pos, y=node.y_pos,
            ),
            '  {now}, {now}'.format(now=sql_escape(now)),
            ');',
            'SET {} = LAST_INSERT_ID();'.format(var_name),
            '',
        ]

    lines += [
        '-- ----------------------------------------------------------------',
        '-- custom_map_edges',
        '-- ----------------------------------------------------------------',
    ]

    warnings = []
    for link in cfg.links:
        if link.node1 not in cfg.nodes:
            warnings.append('WARNING: Link {} references unknown node {}'.format(link.name, link.node1))
        if link.node2 not in cfg.nodes:
            warnings.append('WARNING: Link {} references unknown node {}'.format(link.name, link.node2))

        var1 = _node_var(link.node1)
        var2 = _node_var(link.node2)
        # Use a scalar subquery so the INSERT gracefully stores NULL when the
        # port doesn't exist in this DB, rather than failing the FK constraint.
        if link.port_id is not None:
            port_id_sql = '(SELECT `port_id` FROM `ports` WHERE `port_id` = {} LIMIT 1)'.format(link.port_id)
        else:
            port_id_sql = 'NULL'
        fixed_width_sql = _link_fixed_width(link, cfg)

        n1 = cfg.nodes.get(link.node1)
        n2 = cfg.nodes.get(link.node2)
        if n1 and n2:
            mid_x = (n1.x_pos + n2.x_pos) // 2
            mid_y = (n1.y_pos + n2.y_pos) // 2
        else:
            mid_x = 0
            mid_y = 0

        edge_label = link.incomment or link.bandwidth or ''

        lines += [
            '-- Link: {}'.format(link.name),
            'INSERT INTO `custom_map_edges` (',
            '  `custom_map_id`, `custom_map_node1_id`, `custom_map_node2_id`,',
            '  `port_id`, `reverse`, `style`, `showpct`, `showbps`, `label`,',
            '  `fixed_width`,',
            '  `text_face`, `text_size`, `text_colour`, `label_stroke_colour`, `mid_x`, `mid_y`,',
            '  `created_at`, `updated_at`',
            ') VALUES (',
            '  {map_var}, {v1}, {v2},'.format(map_var=map_var, v1=var1, v2=var2),
            '  {port_id}, 0, {style}, 0, 1, {label},'.format(
                port_id=port_id_sql,
                style=sql_escape('dynamic'),
                label=sql_escape(edge_label),
            ),
            '  {},'.format(fixed_width_sql),
            '  {face}, 12, {tc}, NULL, {mid_x}, {mid_y},'.format(
                face=sql_escape('arial'),
                tc=sql_escape('#343434'),
                mid_x=mid_x, mid_y=mid_y,
            ),
            '  {now}, {now}'.format(now=sql_escape(now)),
            ');',
            '',
        ]

    if warnings:
        lines = ['-- ' + w for w in warnings] + [''] + lines

    return '\n'.join(lines) + '\n'


# ---------------------------------------------------------------------------
# config.php reader
# ---------------------------------------------------------------------------

def read_librenms_config(librenms_path: str) -> dict:
    """
    Parse DB credentials from LibreNMS config.php.
    Looks for: $config['db_host'], $config['db_user'], $config['db_pass'],
               $config['db_name'], $config['db_port']
    """
    config_path = os.path.join(librenms_path, 'config.php')
    if not os.path.isfile(config_path):
        raise FileNotFoundError('config.php not found at {}'.format(config_path))

    result = {}
    key_map = {
        'db_host': 'host',
        'db_user': 'user',
        'db_pass': 'password',
        'db_name': 'database',
        'db_port': 'port',
    }

    with open(config_path, 'r') as f:
        content = f.read()

    for php_key, py_key in key_map.items():
        m = re.search(
            r"""\$config\s*\[\s*['"]{key}['"]\s*\]\s*=\s*['"](.*?)['"];""".format(key=re.escape(php_key)),
            content,
        )
        if m:
            result[py_key] = m.group(1)

    return result


def find_librenms_path(start: str) -> Optional[str]:
    """Walk up from start looking for a directory containing config.php."""
    path = os.path.abspath(start)
    for _ in range(6):
        if os.path.isfile(os.path.join(path, 'config.php')):
            return path
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return None


# ---------------------------------------------------------------------------
# DB connection helper
# ---------------------------------------------------------------------------

def _connect(db_conf: dict):
    """Open and return a database connection using pymysql or mysql.connector."""
    try:
        import pymysql as mysql_driver
    except ImportError:
        try:
            import mysql.connector as mysql_driver  # type: ignore
        except ImportError:
            print('ERROR: Neither pymysql nor mysql-connector-python is installed.', file=sys.stderr)
            print('Install with: pip3 install pymysql', file=sys.stderr)
            sys.exit(1)

    connect_kwargs = dict(
        host=db_conf.get('host', 'localhost'),
        user=db_conf.get('user', 'librenms'),
        password=db_conf.get('password', ''),
        database=db_conf.get('database', 'librenms'),
        charset='utf8mb4',
    )
    if 'port' in db_conf:
        connect_kwargs['port'] = int(db_conf['port'])

    return mysql_driver.connect(**connect_kwargs)


# ---------------------------------------------------------------------------
# Direct DB insert (single map)
# ---------------------------------------------------------------------------

def _insert_one_map(cursor, cfg: WMConfig, map_name: str,
                    use_icons: bool = True, menu_group: Optional[str] = None,
                    map_id_map: Optional[Dict[str, int]] = None,
                    now: Optional[str] = None,
                    node_label_offset: bool = False) -> int:
    """
    Insert one map (nodes + edges) using an existing cursor.  Returns map_id.

    map_id_map  — {stem: map_id} of already-inserted maps, used to resolve
                  linked_custom_map_id for map-link nodes (batch mode).
    """
    if now is None:
        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')

    legend_colours = build_legend_colours(cfg.scales)
    legend_steps = sum(1 for k in legend_colours if k.lstrip('-').isdigit() and int(k) >= 0)

    cursor.execute(
        """INSERT INTO `custom_maps`
               (`name`, `width`, `height`, `menu_group`,
                `node_align`, `reverse_arrows`, `edge_separation`,
                `legend_x`, `legend_y`, `legend_steps`, `legend_font_size`,
                `legend_hide_invalid`, `legend_hide_overspeed`, `legend_colours`,
                `background_type`, `background_data`,
                `options`, `newnodeconfig`, `newedgeconfig`,
                `created_at`, `updated_at`)
               VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
        (
            map_name,
            '{}px'.format(cfg.width),
            '{}px'.format(cfg.height),
            menu_group,
            10, 0, 0,
            cfg.keypos_x, cfg.keypos_y, legend_steps, 14,
            0, 0, json.dumps(legend_colours),
            'none', None,
            json.dumps(DEFAULT_OPTIONS),
            json.dumps(DEFAULT_NEWNODECONFIG),
            json.dumps(DEFAULT_NEWEDGECONFIG),
            now, now,
        )
    )
    map_id = cursor.lastrowid
    print('Created custom map id={} name={!r}'.format(map_id, map_name))

    node_ids = {}
    for node in cfg.nodes.values():
        label = (node.label or node.name)[:50]
        style, image = node_style_and_image(node, use_icons)

        linked_map_id = None
        if node.linked_map_name and map_id_map:
            linked_map_id = map_id_map.get(node.linked_map_name)

        # Validate device_id: set to None if it doesn't exist in this DB
        device_id = node.device_id
        if device_id is not None:
            cursor.execute('SELECT `device_id` FROM `devices` WHERE `device_id` = %s LIMIT 1', (device_id,))
            if cursor.fetchone() is None:
                print('  WARNING: device_id={} not found in DB for node {!r}, storing NULL'.format(
                    device_id, node.name), file=sys.stderr)
                device_id = None

        if node_label_offset:
            col_extra = ', `label_offset_y`'
            val_extra = ', %s'
            val_args_extra = (None,)
        else:
            col_extra = ''
            val_extra = ''
            val_args_extra = ()
        cursor.execute(
            """INSERT INTO `custom_map_nodes`
                   (`custom_map_id`, `device_id`, `linked_custom_map_id`, `label`, `style`, `icon`, `image`,
                    `size`, `border_width`, `text_face`, `text_size`, `text_colour`,
                    `label_stroke_colour`{col_extra},
                    `colour_bg`, `colour_bdr`, `x_pos`, `y_pos`,
                    `created_at`, `updated_at`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s{val_extra}, %s, %s, %s, %s, %s, %s)""".format(
                col_extra=col_extra, val_extra=val_extra),
            (
                map_id,
                device_id,
                linked_map_id,
                label,
                style,
                None,
                image or '',
                node_size(style), 1, 'arial', 14, '#343434',
                node_label_stroke_colour(style),
            ) + val_args_extra + (
                node_colours(style)[0], node_colours(style)[1],
                node.x_pos, node.y_pos,
                now, now,
            )
        )
        node_ids[node.name] = cursor.lastrowid
        print('  Node {!r} id={} device_id={} style={!r} linked_map_id={}'.format(
            node.name, node_ids[node.name], device_id, style, linked_map_id))

    for link in cfg.links:
        n1_id = node_ids.get(link.node1)
        n2_id = node_ids.get(link.node2)
        if n1_id is None or n2_id is None:
            print('WARNING: skipping link {!r}: node not found ({!r}, {!r})'.format(
                link.name, link.node1, link.node2), file=sys.stderr)
            continue

        n1 = cfg.nodes.get(link.node1)
        n2 = cfg.nodes.get(link.node2)
        mid_x = ((n1.x_pos + n2.x_pos) // 2) if (n1 and n2) else 0
        mid_y = ((n1.y_pos + n2.y_pos) // 2) if (n1 and n2) else 0

        edge_label = link.incomment or link.bandwidth or ''
        w = link.width if link.width is not None else cfg.default_link_width

        # Validate port_id: set to None if the port doesn't exist in this DB
        port_id = link.port_id
        if port_id is not None:
            cursor.execute('SELECT `port_id` FROM `ports` WHERE `port_id` = %s LIMIT 1', (port_id,))
            if cursor.fetchone() is None:
                print('  WARNING: port_id={} not found in DB for link {!r}, storing NULL'.format(
                    port_id, link.name), file=sys.stderr)
                port_id = None

        cursor.execute(
            """INSERT INTO `custom_map_edges`
                   (`custom_map_id`, `custom_map_node1_id`, `custom_map_node2_id`,
                    `port_id`, `reverse`, `style`, `showpct`, `showbps`, `label`,
                    `fixed_width`,
                    `text_face`, `text_size`, `text_colour`, `label_stroke_colour`, `mid_x`, `mid_y`,
                    `created_at`, `updated_at`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
            (
                map_id, n1_id, n2_id,
                port_id, 0, 'dynamic', 0, 1, edge_label,
                w,
                'arial', 12, '#343434', None, mid_x, mid_y,
                now, now,
            )
        )
        print('  Edge {!r} port_id={} width={}'.format(link.name, port_id, w))

    return map_id


def insert_direct(cfg: WMConfig, map_name: str, db_conf: dict,
                  use_icons: bool = True, menu_group: Optional[str] = None,
                  map_id_map: Optional[Dict[str, int]] = None,
                  node_label_offset: bool = False) -> int:
    """Insert a single map into the DB. Returns the new map_id."""
    conn = _connect(db_conf)
    cursor = conn.cursor()
    try:
        map_id = _insert_one_map(cursor, cfg, map_name, use_icons, menu_group, map_id_map,
                                 node_label_offset=node_label_offset)
        conn.commit()
        print('Done.')
        return map_id
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Batch mode: config discovery and dependency resolution
# ---------------------------------------------------------------------------

def discover_configs(configs_dir: str) -> List[str]:
    """Return sorted list of .conf file stems found in configs_dir."""
    stems = []
    try:
        entries = sorted(os.scandir(configs_dir), key=lambda e: e.name.lower())
    except OSError as e:
        print('ERROR: Cannot scan {}: {}'.format(configs_dir, e), file=sys.stderr)
        sys.exit(1)
    for entry in entries:
        if entry.is_file() and entry.name.lower().endswith('.conf'):
            stems.append(os.path.splitext(entry.name)[0])
    return stems


def parse_infourl_map_ref(infourl: str) -> Optional[str]:
    """
    Extract the conf file stem from an INFOURL value.
      '/weathermap/output/SomeMap.html'  ->  'SomeMap'
      'http://host/page/SomeMap.html'    ->  'SomeMap'
    Returns None if no .html reference is found.
    """
    m = re.search(r'/([^/]+)\.html\b', infourl, re.IGNORECASE)
    if m:
        return m.group(1)
    return None


def resolve_map_links(cfgs: Dict[str, WMConfig]) -> None:
    """
    For each node with an INFOURL that references a known conf stem, set
    node.linked_map_name to that stem so the batch inserter can populate
    linked_custom_map_id.
    """
    known_stems = set(cfgs.keys())
    for cfg in cfgs.values():
        for node in cfg.nodes.values():
            if node.infourl and node.linked_map_name is None:
                ref = parse_infourl_map_ref(node.infourl)
                if ref and ref in known_stems:
                    node.linked_map_name = ref


def build_dependency_graph(cfgs: Dict[str, WMConfig]) -> Dict[str, set]:
    """
    Return {stem: set_of_stems_it_depends_on}.
    Map A depends on map B if any of A's nodes links to B (so B must be
    inserted before A to satisfy the linked_custom_map_id FK).
    """
    graph: Dict[str, set] = {stem: set() for stem in cfgs}
    for stem, cfg in cfgs.items():
        for node in cfg.nodes.values():
            dep = node.linked_map_name
            if dep and dep in graph and dep != stem:
                graph[stem].add(dep)
    return graph


def topological_sort(graph: Dict[str, set]) -> List[str]:
    """
    Kahn's algorithm. graph[node] = set of nodes that must be inserted BEFORE node.
    Returns an ordered list where all dependencies precede their dependents.
    Raises ValueError on circular dependencies.
    """
    in_degree = {n: len(deps) for n, deps in graph.items()}
    # Queue initialised with nodes that have no dependencies (stable sort by name)
    queue = sorted(n for n, d in in_degree.items() if d == 0)
    result: List[str] = []

    while queue:
        node = queue.pop(0)
        result.append(node)
        # Reduce in-degree for all nodes that depended on this one
        for n, deps in graph.items():
            if node in deps:
                in_degree[n] -= 1
                if in_degree[n] == 0:
                    queue.append(n)
                    queue.sort()

    if len(result) != len(graph):
        cycle = [n for n in graph if n not in result]
        raise ValueError('Circular dependency detected among maps: {}'.format(', '.join(sorted(cycle))))

    return result


def break_dependency_cycles(graph: Dict[str, set], cfgs: Dict[str, WMConfig]) -> List[tuple]:
    """
    Iteratively detect and break cycles in the dependency graph by removing
    one back-edge per cycle until no cycles remain.

    For each removed edge (stem_a depends_on stem_b), the corresponding
    node.linked_map_name is cleared to None in cfgs[stem_a] so the node
    gets linked_custom_map_id = NULL in the output.

    Returns list of (from_stem, to_stem) pairs that were removed.
    Modifies graph and cfgs in place.
    """
    removed = []

    while True:
        # Kahn's pass to find which nodes are stuck in cycles
        in_degree = {n: len(deps) for n, deps in graph.items()}
        queue = sorted(n for n, d in in_degree.items() if d == 0)
        processed = []

        while queue:
            node = queue.pop(0)
            processed.append(node)
            for n, deps in graph.items():
                if node in deps:
                    in_degree[n] -= 1
                    if in_degree[n] == 0:
                        queue.append(n)
                        queue.sort()

        if len(processed) == len(graph):
            break  # No more cycles

        # Nodes not yet processed are in cycles
        cycle_nodes = sorted(n for n in graph if n not in processed)

        # Pick the first cycle node and remove its first cycle-internal dependency
        node = cycle_nodes[0]
        cycle_deps = sorted(dep for dep in graph[node] if dep in cycle_nodes)
        if not cycle_deps:
            cycle_deps = sorted(graph[node])  # fallback: shouldn't happen

        dep = cycle_deps[0]
        graph[node].discard(dep)
        removed.append((node, dep))

        # Null out the matching node link in the WMConfig
        if node in cfgs and dep in cfgs:
            for wm_node in cfgs[node].nodes.values():
                if wm_node.linked_map_name == dep:
                    wm_node.linked_map_name = None

    return removed


# ---------------------------------------------------------------------------
# Batch SQL generation
# ---------------------------------------------------------------------------

_TRUNCATE_SQL = textwrap.dedent("""\
    -- Force: truncate all custom map tables and reset auto-increment
    SET FOREIGN_KEY_CHECKS=0;
    TRUNCATE TABLE `custom_map_edges`;
    TRUNCATE TABLE `custom_map_nodes`;
    TRUNCATE TABLE `custom_maps`;
    SET FOREIGN_KEY_CHECKS=1;

""")


def batch_generate_sql(ordered_stems: List[str], cfgs: Dict[str, WMConfig],
                       map_names: Dict[str, str], use_icons: bool = True,
                       menu_group: Optional[str] = None,
                       reset_table_ids: bool = False,
                       node_label_offset: bool = False) -> str:
    """
    Generate a single SQL script that inserts all maps in dependency order.
    Each map gets a unique SQL user variable for its ID so that later maps can
    reference it via linked_custom_map_id.
    """
    # Build {stem: '@map_<stem>_id'} for cross-map variable references
    all_map_vars: Dict[str, str] = {stem: _map_var_for_stem(stem) for stem in ordered_stems}

    parts = []
    if reset_table_ids:
        parts.append(_TRUNCATE_SQL)

    for stem in ordered_stems:
        cfg = cfgs[stem]
        map_name = map_names[stem]
        sql = generate_sql(
            cfg, map_name,
            use_icons=use_icons,
            menu_group=menu_group,
            map_var=all_map_vars[stem],
            all_map_vars=all_map_vars,
            node_label_offset=node_label_offset,
        )
        parts.append(sql)

    return '\n'.join(parts)


# ---------------------------------------------------------------------------
# Batch direct insert
# ---------------------------------------------------------------------------

def batch_insert_direct(ordered_stems: List[str], cfgs: Dict[str, WMConfig],
                        map_names: Dict[str, str], db_conf: dict,
                        use_icons: bool = True, menu_group: Optional[str] = None,
                        reset_table_ids: bool = False,
                        node_label_offset: bool = False) -> None:
    """Insert all maps in dependency order using a single DB connection."""
    conn = _connect(db_conf)
    cursor = conn.cursor()
    try:
        if reset_table_ids:
            print('Truncating custom map tables...', file=sys.stderr)
            cursor.execute('SET FOREIGN_KEY_CHECKS=0')
            cursor.execute('TRUNCATE TABLE `custom_map_edges`')
            cursor.execute('TRUNCATE TABLE `custom_map_nodes`')
            cursor.execute('TRUNCATE TABLE `custom_maps`')
            cursor.execute('SET FOREIGN_KEY_CHECKS=1')

        now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
        map_id_map: Dict[str, int] = {}   # stem -> inserted map_id

        for stem in ordered_stems:
            cfg = cfgs[stem]
            map_name = map_names[stem]
            map_id = _insert_one_map(cursor, cfg, map_name, use_icons, menu_group, map_id_map, now,
                                     node_label_offset=node_label_offset)
            map_id_map[stem] = map_id

        conn.commit()
        print('Done. Inserted {} map(s).'.format(len(ordered_stems)))
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# DB credential helpers
# ---------------------------------------------------------------------------

def _resolve_db_conf(args, search_path: str) -> dict:
    """Read DB credentials from config.php and apply any CLI overrides."""
    librenms_path = args.librenms_path
    if librenms_path is None:
        librenms_path = find_librenms_path(os.path.dirname(os.path.abspath(__file__)))
        if librenms_path is None:
            librenms_path = find_librenms_path(os.path.dirname(os.path.abspath(search_path)))
    if librenms_path is None:
        print('ERROR: Could not find LibreNMS root (config.php). Use --librenms-path.', file=sys.stderr)
        sys.exit(1)

    print('Reading DB config from {}/config.php'.format(librenms_path), file=sys.stderr)
    db_conf = read_librenms_config(librenms_path)

    if args.db_host:
        db_conf['host'] = args.db_host
    if args.db_user:
        db_conf['user'] = args.db_user
    if args.db_pass:
        db_conf['password'] = args.db_pass
    if args.db_name:
        db_conf['database'] = args.db_name

    if not db_conf.get('host'):
        db_conf['host'] = 'localhost'

    return db_conf


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    # Brief usage when invoked with no arguments
    if len(sys.argv) == 1:
        print('Usage: weathermap_to_custom_map.py <config.conf | configs_dir> [options]')
        print('       weathermap_to_custom_map.py --help   for full documentation')
        sys.exit(0)

    parser = argparse.ArgumentParser(
        description='Convert PHP Weathermap .conf file(s) to LibreNMS Custom Maps.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    parser.add_argument(
        'path', nargs='?',
        help='Path to a single Weathermap .conf file, or a directory of .conf files (batch mode)',
    )
    parser.add_argument(
        '--output', choices=['sql', 'direct', 'both'], default='sql',
        help='Output mode: sql (default), direct (DB insert), both',
    )
    parser.add_argument('--sql-file', default=None, help='Write SQL to this file (default: stdout)')
    parser.add_argument('--reset-table-ids', action='store_true',
                        help='Batch: TRUNCATE custom map tables before inserting')
    parser.add_argument('--skip-circular-deps', action='store_true',
                        help='Batch: break circular map-link dependencies (null the back-edge) instead of aborting')
    parser.add_argument('--force', action='store_true',
                        help='Batch: implies --reset-table-ids + --skip-circular-deps; prints a warning and proceeds regardless of dependency errors')
    parser.add_argument('--librenms-path', default=None,
                        help='LibreNMS root directory (contains config.php)')
    parser.add_argument('--db-host', default=None)
    parser.add_argument('--db-user', default=None)
    parser.add_argument('--db-pass', default=None)
    parser.add_argument('--db-name', default=None)
    parser.add_argument('--map-name', default=None,
                        help='Override map name (single-map mode only)')
    parser.add_argument('--menu-group', default=None,
                        help='Assign maps to this menu group')
    parser.add_argument('--no-icons', action='store_true',
                        help='Use plain box nodes instead of device image icons')
    parser.add_argument('--node-label-offset', action='store_true',
                        help='Include label_offset_y column in node INSERTs (requires the upstream schema change)')
    args = parser.parse_args()

    if args.path is None:
        parser.print_usage()
        sys.exit(1)

    use_icons = not args.no_icons
    node_label_offset = args.node_label_offset

    # -----------------------------------------------------------------------
    # Batch mode: directory of .conf files
    # -----------------------------------------------------------------------
    if os.path.isdir(args.path):
        configs_dir = args.path

        # --force implies --reset-table-ids + --skip-circular-deps
        reset_table_ids = args.reset_table_ids or args.force
        skip_circular_deps = args.skip_circular_deps or args.force

        if args.force:
            print(
                'WARNING: --force mode active.  Tables will be truncated and circular '
                'dependencies will be broken by nulling back-links.  Check the output '
                'for "DROPPED CYCLE EDGE" warnings to see which map links were lost.',
                file=sys.stderr,
            )

        stems = discover_configs(configs_dir)
        if not stems:
            print('ERROR: No .conf files found in {}'.format(configs_dir), file=sys.stderr)
            sys.exit(1)

        print('Found {} .conf file(s) in {}'.format(len(stems), configs_dir), file=sys.stderr)

        # Parse all configs
        cfgs: Dict[str, WMConfig] = {}
        map_names: Dict[str, str] = {}
        for stem in stems:
            path = os.path.join(configs_dir, stem + '.conf')
            try:
                cfg = parse_weathermap_conf(path)
            except Exception as e:
                if args.force:
                    print('WARNING: skipping {}: {}'.format(path, e), file=sys.stderr)
                    continue
                print('ERROR parsing {}: {}'.format(path, e), file=sys.stderr)
                sys.exit(1)
            cfgs[stem] = cfg
            map_names[stem] = cfg.title
            print('  Parsed {!r}: {} node(s), {} link(s)'.format(
                stem, len(cfg.nodes), len(cfg.links)), file=sys.stderr)

        # Resolve cross-map node links via INFOURL
        resolve_map_links(cfgs)
        linked_count = sum(
            1 for cfg in cfgs.values()
            for node in cfg.nodes.values()
            if node.linked_map_name
        )
        if linked_count:
            print('{} cross-map node link(s) resolved.'.format(linked_count), file=sys.stderr)

        # Topological sort — with optional cycle breaking
        graph = build_dependency_graph(cfgs)

        if skip_circular_deps:
            removed_edges = break_dependency_cycles(graph, cfgs)
            for (from_stem, to_stem) in removed_edges:
                print(
                    'WARNING: DROPPED CYCLE EDGE {!r} -> {!r}: '
                    'linked_custom_map_id will be NULL for affected node(s).'.format(
                        from_stem, to_stem),
                    file=sys.stderr,
                )

        try:
            ordered_stems = topological_sort(graph)
        except ValueError as e:
            print('ERROR: {}'.format(e), file=sys.stderr)
            print('Hint: use --skip-circular-deps (or --force) to break cycles automatically.',
                  file=sys.stderr)
            sys.exit(1)

        print('Insertion order: {}'.format(' -> '.join(ordered_stems)), file=sys.stderr)

        if args.output in ('sql', 'both'):
            sql = batch_generate_sql(
                ordered_stems, cfgs, map_names,
                use_icons=use_icons,
                menu_group=args.menu_group,
                reset_table_ids=reset_table_ids,
                node_label_offset=node_label_offset,
            )
            if args.sql_file:
                with open(args.sql_file, 'w') as f:
                    f.write(sql)
                print('SQL written to {}'.format(args.sql_file), file=sys.stderr)
            else:
                print(sql)

        if args.output in ('direct', 'both'):
            db_conf = _resolve_db_conf(args, configs_dir)
            batch_insert_direct(
                ordered_stems, cfgs, map_names, db_conf,
                use_icons=use_icons,
                menu_group=args.menu_group,
                reset_table_ids=reset_table_ids,
                node_label_offset=node_label_offset,
            )

    # -----------------------------------------------------------------------
    # Single-map mode: one .conf file
    # -----------------------------------------------------------------------
    elif os.path.isfile(args.path):
        conf_file = args.path

        if args.force:
            print('WARNING: --force is only meaningful in batch (directory) mode; ignored.', file=sys.stderr)

        cfg = parse_weathermap_conf(conf_file)
        map_name = args.map_name or cfg.title

        print('Parsed {} node(s), {} link(s) from {}'.format(
            len(cfg.nodes), len(cfg.links), conf_file), file=sys.stderr)
        print('  Legend position: ({}, {})'.format(cfg.keypos_x, cfg.keypos_y), file=sys.stderr)
        print('  Default link width: {}'.format(cfg.default_link_width), file=sys.stderr)
        for node in cfg.nodes.values():
            style, image = node_style_and_image(node, use_icons)
            print('  Node {!r}: device_id={}, pos=({},{}), style={!r}, image={!r}, infourl={!r}'.format(
                node.name, node.device_id, node.x_pos, node.y_pos, style, image, node.infourl),
                file=sys.stderr)
        for link in cfg.links:
            print('  Link {!r}: {} -> {}, port_id={}, width={}'.format(
                link.name, link.node1, link.node2, link.port_id,
                link.width or cfg.default_link_width), file=sys.stderr)

        if args.output in ('sql', 'both'):
            sql = generate_sql(cfg, map_name, use_icons=use_icons, menu_group=args.menu_group,
                               node_label_offset=node_label_offset)
            if args.sql_file:
                with open(args.sql_file, 'w') as f:
                    f.write(sql)
                print('SQL written to {}'.format(args.sql_file), file=sys.stderr)
            else:
                print(sql)

        if args.output in ('direct', 'both'):
            db_conf = _resolve_db_conf(args, conf_file)
            insert_direct(cfg, map_name, db_conf, use_icons=use_icons, menu_group=args.menu_group,
                          node_label_offset=node_label_offset)

    else:
        print('ERROR: {} is not a file or directory'.format(args.path), file=sys.stderr)
        sys.exit(1)


if __name__ == '__main__':
    main()
