#!/usr/bin/env python3
"""
weathermap_to_custom_map.py

Convert a PHP Weathermap .conf file to a LibreNMS Custom Map.

Outputs SQL INSERT statements and/or inserts directly into the LibreNMS
MariaDB/MySQL database.

Usage:
    python3 weathermap_to_custom_map.py <config.conf> [options]

Options:
    --output sql|direct|both   Output mode (default: sql)
    --sql-file FILE            Write SQL to FILE instead of stdout
    --librenms-path PATH       Path to LibreNMS root (to find config.php)
    --db-host HOST             Override DB host
    --db-user USER             Override DB user
    --db-pass PASS             Override DB password
    --db-name DB               Override DB name
    --map-name NAME            Override map name (default: from TITLE in conf)
    --menu-group NAME          Set map menu group
    --no-icons                 Use box nodes instead of device image icons
"""

import argparse
import json
import os
import re
import sys
import textwrap
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Optional


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


def node_label_bg_colour(style: str) -> Optional[str]:
    """Return label_bg_colour for a node style, or None for box nodes."""
    if style == 'circularImage':
        return '#FFFFCC'   # match node bg — yellow label on map/external nodes
    if style == 'image':
        return '#D2E5FF'   # match node bg — light blue label on device nodes
    return None            # box/other: no label background needed


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


def generate_sql(cfg: WMConfig, map_name: str, use_icons: bool = True,
                 menu_group: Optional[str] = None) -> str:
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
        'SET @map_id = LAST_INSERT_ID();',
        '',
        '-- ----------------------------------------------------------------',
        '-- custom_map_nodes',
        '-- ----------------------------------------------------------------',
    ]

    for node in cfg.nodes.values():
        var_name = '@node_' + re.sub(r'[^a-zA-Z0-9_]', '_', node.name)
        device_id_sql = str(node.device_id) if node.device_id is not None else 'NULL'
        label = (node.label or node.name)[:50]
        style, image = node_style_and_image(node, use_icons)

        lbg = node_label_bg_colour(style)
        lines += [
            '-- Node: {}'.format(node.name),
            'INSERT INTO `custom_map_nodes` (',
            '  `custom_map_id`, `device_id`, `label`, `style`, `icon`, `image`,',
            '  `size`, `border_width`, `text_face`, `text_size`, `text_colour`,',
            '  `label_bg_colour`, `label_offset_y`,',
            '  `colour_bg`, `colour_bdr`, `x_pos`, `y_pos`,',
            '  `created_at`, `updated_at`',
            ') VALUES (',
            '  @map_id, {device_id}, {label}, {style}, NULL, {image},'.format(
                device_id=device_id_sql,
                label=sql_escape(label),
                style=sql_escape(style),
                image=sql_escape(image or ''),
            ),
            '  {size}, 1, {face}, 14, {tc},'.format(
                size=node_size(style),
                face=sql_escape('arial'),
                tc=sql_escape('#343434'),
            ),
            '  {lbg}, NULL,'.format(lbg=sql_escape(lbg) if lbg else 'NULL'),
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

        var1 = '@node_' + re.sub(r'[^a-zA-Z0-9_]', '_', link.node1)
        var2 = '@node_' + re.sub(r'[^a-zA-Z0-9_]', '_', link.node2)
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
            '  `text_face`, `text_size`, `text_colour`, `mid_x`, `mid_y`,',
            '  `created_at`, `updated_at`',
            ') VALUES (',
            '  @map_id, {v1}, {v2},'.format(v1=var1, v2=var2),
            '  {port_id}, 0, {style}, 0, 1, {label},'.format(
                port_id=port_id_sql,
                style=sql_escape('dynamic'),
                label=sql_escape(edge_label),
            ),
            '  {},'.format(fixed_width_sql),
            '  {face}, 12, {tc}, {mid_x}, {mid_y},'.format(
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
# Direct DB insert
# ---------------------------------------------------------------------------

def insert_direct(cfg: WMConfig, map_name: str, db_conf: dict,
                  use_icons: bool = True, menu_group: Optional[str] = None) -> None:
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

    now = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S')
    legend_colours = build_legend_colours(cfg.scales)
    legend_steps = sum(1 for k in legend_colours if k.lstrip('-').isdigit() and int(k) >= 0)

    conn = mysql_driver.connect(**connect_kwargs)
    cursor = conn.cursor()

    try:
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

            cursor.execute(
                """INSERT INTO `custom_map_nodes`
                   (`custom_map_id`, `device_id`, `label`, `style`, `icon`, `image`,
                    `size`, `border_width`, `text_face`, `text_size`, `text_colour`,
                    `label_bg_colour`, `label_offset_y`,
                    `colour_bg`, `colour_bdr`, `x_pos`, `y_pos`,
                    `created_at`, `updated_at`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    map_id,
                    node.device_id,
                    label,
                    style,
                    None,
                    image or '',
                    node_size(style), 1, 'arial', 14, '#343434',
                    node_label_bg_colour(style), None,
                    node_colours(style)[0], node_colours(style)[1],
                    node.x_pos, node.y_pos,
                    now, now,
                )
            )
            node_ids[node.name] = cursor.lastrowid
            print('  Node {!r} id={} device_id={} style={!r}'.format(
                node.name, node_ids[node.name], node.device_id, style))

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
                    `text_face`, `text_size`, `text_colour`, `mid_x`, `mid_y`,
                    `created_at`, `updated_at`)
                   VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)""",
                (
                    map_id, n1_id, n2_id,
                    port_id, 0, 'dynamic', 0, 1, edge_label,
                    w,
                    'arial', 12, '#343434', mid_x, mid_y,
                    now, now,
                )
            )
            print('  Edge {!r} port_id={} width={}'.format(link.name, port_id, w))

        conn.commit()
        print('Done.')
    except Exception:
        conn.rollback()
        raise
    finally:
        cursor.close()
        conn.close()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description='Convert a PHP Weathermap .conf file to a LibreNMS Custom Map.',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=textwrap.dedent(__doc__),
    )
    parser.add_argument('conf_file', help='Path to Weathermap .conf file')
    parser.add_argument(
        '--output', choices=['sql', 'direct', 'both'], default='sql',
        help='Output mode: sql (default), direct (DB insert), both',
    )
    parser.add_argument('--sql-file', default=None, help='Write SQL to this file (default: stdout)')
    parser.add_argument('--librenms-path', default=None, help='LibreNMS root directory (contains config.php)')
    parser.add_argument('--db-host', default=None)
    parser.add_argument('--db-user', default=None)
    parser.add_argument('--db-pass', default=None)
    parser.add_argument('--db-name', default=None)
    parser.add_argument('--map-name', default=None, help='Override map name')
    parser.add_argument('--menu-group', default=None, help='Map menu group')
    parser.add_argument('--no-icons', action='store_true',
                        help='Use plain box nodes instead of device image icons')
    args = parser.parse_args()

    use_icons = not args.no_icons

    cfg = parse_weathermap_conf(args.conf_file)
    map_name = args.map_name or cfg.title

    print('Parsed {} nodes, {} links from {}'.format(
        len(cfg.nodes), len(cfg.links), args.conf_file), file=sys.stderr)
    print('  Legend position: ({}, {})'.format(cfg.keypos_x, cfg.keypos_y), file=sys.stderr)
    print('  Default link width: {}'.format(cfg.default_link_width), file=sys.stderr)
    for node in cfg.nodes.values():
        style, image = node_style_and_image(node, use_icons)
        print('  Node {!r}: device_id={}, pos=({},{}), style={!r}, image={!r}'.format(
            node.name, node.device_id, node.x_pos, node.y_pos, style, image), file=sys.stderr)
    for link in cfg.links:
        print('  Link {!r}: {} -> {}, port_id={}, width={}'.format(
            link.name, link.node1, link.node2, link.port_id,
            link.width or cfg.default_link_width), file=sys.stderr)

    if args.output in ('sql', 'both'):
        sql = generate_sql(cfg, map_name, use_icons=use_icons, menu_group=args.menu_group)
        if args.sql_file:
            with open(args.sql_file, 'w') as f:
                f.write(sql)
            print('SQL written to {}'.format(args.sql_file), file=sys.stderr)
        else:
            print(sql)

    if args.output in ('direct', 'both'):
        librenms_path = args.librenms_path
        if librenms_path is None:
            librenms_path = find_librenms_path(os.path.dirname(os.path.abspath(__file__)))
            if librenms_path is None:
                librenms_path = find_librenms_path(os.path.dirname(os.path.abspath(args.conf_file)))
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

        insert_direct(cfg, map_name, db_conf, use_icons=use_icons, menu_group=args.menu_group)


if __name__ == '__main__':
    main()
