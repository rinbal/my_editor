# SPDX-FileCopyrightText: 2026 rinbal
# SPDX-License-Identifier: AGPL-3.0-or-later
"""Photo group layout: design reference, not shipping code.

Line for line the same steps as photo_layout.js, so both give bit-identical
results: only +, -, *, /, min, max and comparisons (no log, pow or exp), sums
left to right, ties keep the earlier candidate. Python floats and JavaScript
numbers are both IEEE 754 doubles rounded to nearest, so the same operations
in the same order give the same bits.
"""

import math

CONSTANTS = {
    'version': 4,
    'panorama': 2.4,
    'tall': 0.4,
    'wideCap': {'px': 900, 'vh': 0.85},
    'provisional': (3, 2),
    'regular': {'width': 680, 'gap': 12, 'cap': 560, 'minSide': 0.16, 'captionWidth': 0.28,
                'captionPerPx': 0.12, 'lowHeight': 0.3, 'highHeight': 1.0},
    'phone': {'gap': 8, 'capPx': 520, 'capVh': 0.65, 'captionMin': 136, 'spread': 1.6,
              'refWidth': 350, 'refViewport': 844},
    'weights': {'empty': 3, 'sliver': 10, 'caption': 4, 'low': 1, 'high': 2, 'row': 0.15,
                'balance': 0.04, 'deviation': 1},
    'targetRow': 0.34,
}


def ratio_of(item):
    if item.get('w', 0) > 0 and item.get('h', 0) > 0:
        return item['w'] / item['h']
    w, h = CONSTANTS['provisional']
    return w / h


def cell_info(cell, items):
    """Ratio, internal gaps (k) and internal captions (c) of a cell; see cellInfo."""
    if isinstance(cell, int):
        return {'r': ratio_of(items[cell]), 'k': 0, 'c': 0, 'figs': [cell]}
    inverse = 0
    c = 0
    for n, index in enumerate(cell):
        inverse = inverse + 1 / ratio_of(items[index])
        if n < len(cell) - 1 and items[index].get('caption'):
            c = c + 1
    return {'r': 1 / inverse, 'k': len(cell) - 1, 'c': c, 'figs': list(cell)}


def row_geometry(row, items, W, g, cap):
    cells = [cell_info(cell, items) for cell in row]
    sum_r = 0
    sum_rk = 0
    for cell in cells:
        sum_r = sum_r + cell['r']
        sum_rk = sum_rk + cell['r'] * cell['k']
    m = len(cells)
    H = (W - (m - 1) * g + g * sum_rk) / sum_r
    if H > cap:
        H = cap
    width = (m - 1) * g
    for cell in cells:
        width = width + cell['r'] * (H - cell['k'] * g)
    tiles = []
    x = (W - width) / 2
    for cell in cells:
        cw = cell['r'] * (H - cell['k'] * g)
        y = 0
        for index in cell['figs']:
            th = H if len(cell['figs']) == 1 else cw / ratio_of(items[index])
            tiles.append({'index': index, 'x': x, 'y': y, 'w': cw, 'h': th})
            y = y + th + g
        x = x + cw + g
    return {'height': H, 'width': width, 'tiles': tiles}


def _caption_length(item):
    caption = item.get('caption') or 0
    return 1 if caption is True else (caption if caption > 0 else 0)


def _inside_captions_fit(row, items, ref):
    """Whether every caption inside a stack fits on one line; see insideCaptionsFit."""
    geo = row_geometry(row, items, ref['width'], ref['gap'], ref['cap'])
    t = 0
    for cell in row:
        if isinstance(cell, int):
            t = t + 1
            continue
        for n in range(len(cell) - 1):
            if _caption_length(items[cell[n]]) > math.floor(geo['tiles'][t + n]['w'] * ref['captionPerPx']):
                return False
        t = t + len(cell)
    return True


def _cost(rows, items, ref, weights):
    W = ref['width']
    empty = sliver = caption_short = total = 0
    amin = amax = 0
    first = True
    for n, row in enumerate(rows):
        geo = row_geometry(row, items, W, ref['gap'], ref['cap'])
        empty = empty + ((W - geo['width']) / W) * (geo['height'] / W)
        total = total + geo['height'] + (ref['gap'] if n > 0 else 0)
        for tile in geo['tiles']:
            side = min(tile['w'], tile['h']) / W
            if side < ref['minSide']:
                sliver = sliver + (ref['minSide'] - side)
            if items[tile['index']].get('caption') and tile['w'] / W < ref['captionWidth']:
                caption_short = caption_short + (ref['captionWidth'] - tile['w'] / W)
            area = (tile['w'] / W) * (tile['h'] / W)
            if first:
                amin = amax = area
                first = False
            else:
                amin = min(amin, area)
                amax = max(amax, area)
    t = total / W
    low = ref['lowHeight'] - t if t < ref['lowHeight'] else 0
    high = t - ref['highHeight'] if t > ref['highHeight'] else 0
    return (weights['empty'] * empty + weights['sliver'] * sliver + weights['caption'] * caption_short
            + weights['low'] * low + weights['high'] * high + weights['row'] * (len(rows) - 1)
            + weights['balance'] * (amax / amin - 1))


def _templates(s):
    if len(s) == 2:
        a, b = s
        return [('pair', [[a, b]]), ('column', [[a], [b]])]
    if len(s) == 3:
        a, b, c = s
        return [('trio', [[a, b, c]]), ('lead-left', [[a, [b, c]]]), ('lead-right', [[[a, b], c]]),
                ('lead-top', [[a], [b, c]]), ('lead-bottom', [[a, b], [c]])]
    a, b, c, d = s
    return [('grid', [[a, b], [c, d]]), ('lead-left', [[a, [b, c, d]]]), ('lead-right', [[[a, b, c], d]]),
            ('lead-top', [[a], [b, c, d]]), ('lead-bottom', [[a, b, c], [d]]), ('quartet', [[a, b, c, d]])]


def _justified(s, items, ref, weights):
    W = ref['width']
    target = CONSTANTS['targetRow']
    best = [0]
    came = [0]
    for i in range(1, len(s) + 1):
        chosen = -1
        value = 0
        for size in range(1, min(4, i) + 1):
            row = s[i - size:i]
            geo = row_geometry(row, items, W, ref['gap'], ref['cap'])
            h = geo['height'] / W
            deviation = (h - target) / target if h > target else (target - h) / h
            sliver = 0
            for tile in geo['tiles']:
                side = min(tile['w'], tile['h']) / W
                if side < ref['minSide']:
                    sliver = sliver + (ref['minSide'] - side)
            total = (best[i - size] + weights['deviation'] * deviation + weights['sliver'] * sliver
                     + weights['empty'] * ((W - geo['width']) / W) * h)
            if chosen < 0 or total < value:
                chosen = size
                value = total
        best.append(value)
        came.append(chosen)
    rows = []
    i = len(s)
    while i > 0:
        rows.insert(0, s[i - came[i]:i])
        i -= came[i]
    return rows


def layout(items, stacks=True):
    """The layout of one group; ``stacks=False`` only for comparing variant A."""
    ref = CONSTANTS['regular']
    weights = CONSTANTS['weights']
    rows, names, segment = [], [], []
    run_of, run_size = {}, {}
    for run in _compatible_runs(items):
        for index in run:
            run_of[index] = run[0]
            run_size[index] = len(run)

    def keeps_runs(stack):
        # A stack never splits a run: all its pictures in one run, or each a run of its own.
        return all(run_size[i] == 1 for i in stack) or all(run_of[i] == run_of[stack[0]] for i in stack)

    def flush():
        if not segment:
            return
        if len(segment) == 1:
            rows.append([segment[0]])
            names.append('single')
        elif len(segment) <= 4:
            chosen = None
            value = 0
            for name, candidate in _templates(list(segment)):
                if not stacks and any(not isinstance(cell, int) for row in candidate for cell in row):
                    continue
                if any(not isinstance(cell, int) and not keeps_runs(cell) for row in candidate for cell in row):
                    continue
                if not all(_inside_captions_fit(row, items, ref) for row in candidate):
                    continue
                score = _cost(candidate, items, ref, weights)
                if chosen is None or score < value:
                    chosen = (name, candidate)
                    value = score
            for row in chosen[1]:
                rows.append(row)
                names.append(chosen[0])
        else:
            for row in _justified(list(segment), items, ref, weights):
                rows.append(row)
                names.append('rows')
        segment.clear()

    for index, item in enumerate(items):
        r = ratio_of(item)
        if _stands_alone(item):
            flush()
            rows.append([index])
            names.append('wide' if item.get('wide') is True else 'panorama' if r >= CONSTANTS['panorama'] else 'tall')
        else:
            segment.append(index)
    flush()
    return [{'cells': cells, 'name': names[n]} for n, cells in enumerate(rows)]


def _stands_alone(item):
    """Panorama, very tall or #wide; see standsAlone in photo_layout.js."""
    r = ratio_of(item)
    return item.get('wide') is True or r >= CONSTANTS['panorama'] or r <= CONSTANTS['tall']


def _compatible_runs(items):
    """Runs of similar shape; see compatibleRuns in photo_layout.js."""
    spread = CONSTANTS['phone']['spread']
    runs = []
    run = None
    lo = 0
    hi = 0
    for index, item in enumerate(items):
        r = ratio_of(item)
        if _stands_alone(item):
            runs.append([index])
            run = None
            continue
        if run is not None and max(hi, r) <= min(lo, r) * spread:
            run.append(index)
            lo = min(lo, r)
            hi = max(hi, r)
            continue
        run = [index]
        runs.append(run)
        lo = r
        hi = r
    return runs


def _reads_as_row(figures, items):
    """Whether all side by side still read well; see readsAsRow in photo_layout.js."""
    p = CONSTANTS['phone']
    W = p['refWidth']
    cap = min(p['capPx'], p['capVh'] * p['refViewport'])
    sum_r = 0
    for index in figures:
        sum_r = sum_r + ratio_of(items[index])
    H = min(cap, (W - (len(figures) - 1) * p['gap']) / sum_r)
    for index in figures:
        r = ratio_of(items[index])
        w = r * H
        if w * p['spread'] < min(W, cap * r):
            return False
        if items[index].get('caption') and w < p['captionMin']:
            return False
    return True


def phone_segments(result, items):
    """Narrow-column segments, one for each run, never cut; see phoneSegments in photo_layout.js."""
    cell_of = {}
    for r, row in enumerate(result):
        for c, cell in enumerate(row['cells']):
            for index in ([cell] if isinstance(cell, int) else cell):
                cell_of[index] = [r, c]
    out = []
    for figures in _compatible_runs(items):
        sum_r = 0
        max_r = 0
        for index in figures:
            r = ratio_of(items[index])
            sum_r = sum_r + r
            max_r = max(max_r, r)
        mode = 'strip'
        if len(figures) == 1:
            mode = 'single'
        elif _reads_as_row(figures, items):
            mode = 'row'
        cells = []
        for index in figures:
            if not cells or cells[-1] != cell_of[index]:
                cells.append(cell_of[index])
        out.append({'figures': figures, 'mode': mode, 'cells': cells,
                    'properties': {'--n': str(len(figures)), '--sr': fixed4(sum_r), '--srmax': fixed4(max_r)}})
    return out


def phone_geometry(segment, items, W, V):
    """Geometry of a phone segment; see phoneGeometry in photo_layout.js."""
    p = CONSTANTS['phone']
    alone = len(segment['figures']) == 1 and items[segment['figures'][0]].get('wide') is True
    wide_cap = CONSTANTS['wideCap']
    cap = min(wide_cap['px'], wide_cap['vh'] * V) if alone else min(p['capPx'], p['capVh'] * V)
    n = len(segment['figures'])
    sum_r = 0
    max_r = 0
    for index in segment['figures']:
        r = ratio_of(items[index])
        sum_r = sum_r + r
        max_r = max(max_r, r)
    H = min(cap, (W - (n - 1) * p['gap']) / sum_r if segment['mode'] == 'row' else W / max_r)
    pictures = []
    figures = []
    total = (n - 1) * p['gap']
    smallest = 1
    for index in segment['figures']:
        r = ratio_of(items[index])
        w = r * H
        f = max(w, min(p['captionMin'], W)) if segment['mode'] != 'row' and items[index].get('caption') else w
        pictures.append(w)
        figures.append(f)
        total = total + f
        smallest = min(smallest, w / min(W, cap * r))
    return {'height': H, 'pictures': pictures, 'figures': figures, 'total': total, 'scrolls': total > W + 0.5,
            'smallest': smallest}


def notation(result):
    def cell_text(cell):
        return str(cell) if isinstance(cell, int) else '/'.join(str(i) for i in cell)
    return ' ; '.join(' | '.join(cell_text(c) for c in row['cells']) for row in result)


def fixed4(x):
    """Fixed-point text for markup; see fixed4 in photo_layout.js."""
    n = math.floor(x * 10000 + 0.5)
    a = -n if n < 0 else n
    whole = a // 10000
    frac = str(a - whole * 10000).rjust(4, '0').rstrip('0')
    return ('-' if n < 0 else '') + (f'{whole}.{frac}' if frac else str(whole))


def cell_properties(row, row_index, items):
    """Wide-layout place of each cell of one row; see cellProperties in photo_layout.js."""
    cells = [cell_info(cell, items) for cell in row['cells']]
    rs = rk = rl = 0
    rc = 0
    for cell in cells:
        rs = rs + cell['r']
        rk = rk + cell['r'] * cell['k']
        rl = rl + cell['r'] * cell['c']
        rc = max(rc, cell['c'])
    row_part = {'--row': str(row_index + 1), '--rs': fixed4(rs), '--rg': fixed4((len(cells) - 1) - rk),
                '--rl': fixed4(rl), '--rc': str(rc)}
    xr = xk = xl = 0
    out = []
    for n, cell in enumerate(cells):
        every = dict(row_part)
        every.update({'--r': fixed4(cell['r']), '--k': str(cell['k']), '--c': str(cell['c']),
                      '--xr': fixed4(xr), '--xg': fixed4(n - xk), '--xl': fixed4(xl)})
        xr = xr + cell['r']
        xk = xk + cell['r'] * cell['k']
        xl = xl + cell['r'] * cell['c']
        out.append({key: value for key, value in every.items() if value != '0'})
    return out


def group_markup(result, items):
    """The group's nodes in author order; see groupMarkup in photo_layout.js."""
    segment_of = {}
    for k, segment in enumerate(phone_segments(result, items)):
        for index in segment['figures']:
            segment_of[index] = (k, segment)
    nodes = []
    open_run = None
    for r, row in enumerate(result):
        placed = cell_properties(row, r, items)
        for c, cell in enumerate(row['cells']):
            if isinstance(cell, int):
                node = {'kind': 'figure', 'index': cell, 'properties': placed[c]}
            else:
                node = {'kind': 'stack', 'figures': cell, 'properties': placed[c]}
            k, segment = segment_of[cell if isinstance(cell, int) else cell[0]]
            if segment['mode'] == 'single':
                open_run = None
                nodes.append(node)
                continue
            if open_run is None or open_run[0] != k:
                open_run = (k, {'kind': 'run', 'mode': segment['mode'], 'properties': segment['properties'], 'cells': []})
                nodes.append(open_run[1])
            open_run[1]['cells'].append(node)
    return nodes


def style_text(properties):
    """Custom properties as style text; see styleText in photo_layout.js."""
    return ';'.join(f'{key}:{value}' for key, value in properties.items())


def group_html(nodes, figure):
    """The track content as HTML; see groupHTML in photo_layout.js."""
    def cell(node):
        if node['kind'] == 'figure':
            return figure(node['index'], node['properties'])
        inner = ''.join(figure(index, None) for index in node['figures'])
        return f'<div class="e21-pg-cell e21-pg-stack" style="{style_text(node["properties"])}">{inner}</div>'
    out = []
    for node in nodes:
        if node['kind'] == 'run':
            inner = ''.join(cell(c) for c in node['cells'])
            out.append(f'<div class="e21-pg-run" data-phone="{node["mode"]}" style="{style_text(node["properties"])}">{inner}</div>')
        else:
            out.append(cell(node))
    return ''.join(out)
