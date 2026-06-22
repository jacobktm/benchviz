/** Color palette used across comparison charts. */
const defaultColors = [
    '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd',
    '#8c564b', '#e377c2', '#7f7f7f', '#bcbd22', '#17becf'
];

/**
 * Resolve a trace name (possibly with suffix like "sys (SN 123, 2024-01-01)")
 * to a stable color index within the given colorOrderIds array using prefix matching.
 * Returns -1 if no match found.
 */
function colorIndexForSystemId(sysId, colorOrderIds) {
    if (sysId == null) return -1;
    const name = String(sysId);
    let idx = colorOrderIds.indexOf(name);
    if (idx >= 0) return idx;
    for (let i = 0; i < colorOrderIds.length; i++) {
        const id = colorOrderIds[i];
        if (name.startsWith(id + ' ') || name.startsWith(id + '(') || name === id) {
            return i;
        }
    }
    return -1;
}

/**
 * Map a trace name (possibly pooled suffix) to canonical system short_name.
 * Matches against system_details from an API comparison group.
 */
function canonicalSystemId(traceName, compGroup) {
    const name = String(traceName || '').trim();
    if (!name) return '';
    const details = (compGroup && compGroup.system_details) || [];
    for (const s of details) {
        const sn = (s.short_name || '').trim();
        if (!sn) continue;
        if (name === sn || name.startsWith(sn + ' ') || name.startsWith(sn + '(')) return sn;
    }
    const m = name.match(/^(\S+)\s+\([^()]+\)$/);
    return m ? m[1] : name;
}

/**
 * Sort parallel arrays of labels and values by value (descending for HIB).
 * Returns { labels, values } sorted in sync.
 */
function sortLabelsValuesBestFirst(labels, values, higherIsBetter) {
    const pairs = (Array.isArray(labels) ? labels : []).map((label, i) => ({
        label,
        value: (values && values[i] != null && !isNaN(values[i])) ? Number(values[i]) : null
    }));
    const sorted = pairs.sort((a, b) => {
        const va = a.value != null ? a.value : (higherIsBetter ? -Infinity : Infinity);
        const vb = b.value != null ? b.value : (higherIsBetter ? -Infinity : Infinity);
        return higherIsBetter ? vb - va : va - vb;
    });
    return { labels: sorted.map(p => p.label), values: sorted.map(p => p.value) };
}

/**
 * Sort parallel arrays of labels, values, and system IDs by value (descending for HIB).
 * Returns { labels, values, systemIds } all sorted in sync.
 */
function sortLabelsValuesSystemIdsBestFirst(labels, values, systemIds, higherIsBetter) {
    const pairs = (Array.isArray(labels) ? labels : []).map((label, i) => ({
        label,
        value: (values && values[i] != null && !isNaN(values[i])) ? Number(values[i]) : null,
        systemId: (Array.isArray(systemIds) ? systemIds[i] : undefined)
    }));
    const sorted = pairs.sort((a, b) => {
        const va = a.value != null ? a.value : (higherIsBetter ? -Infinity : Infinity);
        const vb = b.value != null ? b.value : (higherIsBetter ? -Infinity : Infinity);
        return higherIsBetter ? vb - va : va - vb;
    });
    return { labels: sorted.map(p => p.label), values: sorted.map(p => p.value), systemIds: sorted.map(p => p.systemId) };
}

/** Geometric mean of positive numbers. Returns null if no valid values. */
function geometricMean(arr) {
    const valid = arr.filter(v => typeof v === 'number' && !isNaN(v) && v > 0);
    if (!valid.length) return null;
    if (valid.some(v => v <= 0)) return valid.reduce((a, b) => a + b, 0) / valid.length;
    const logSum = valid.reduce((acc, v) => acc + Math.log(v), 0);
    return Math.exp(logSum / valid.length);
}

/** Harmonic mean of positive numbers. Returns null if no valid values. */
function harmonicMean(arr) {
    const valid = arr.filter(v => typeof v === 'number' && !isNaN(v) && v > 0);
    if (!valid.length) return null;
    const sumInv = valid.reduce((acc, v) => acc + 1 / v, 0);
    if (!sumInv) return null;
    return (1 / sumInv) * valid.length;
}

/** p-th percentile of positive numbers (0 < p ≤ 1). Returns null if no valid values. */
function percentilePositive(arr, p) {
    const valid = arr.filter(v => typeof v === 'number' && !isNaN(v) && v > 0).sort((a, b) => a - b);
    if (!valid.length) return null;
    const idx = Math.min(valid.length - 1, Math.max(0, Math.ceil(p * valid.length) - 1));
    return valid[idx];
}

/** Median of positive numbers. Returns null if no valid values. */
function medianPositive(arr) {
    const valid = arr.filter(v => typeof v === 'number' && !isNaN(v) && v > 0).sort((a, b) => a - b);
    if (!valid.length) return null;
    const mid = Math.floor(valid.length / 2);
    if (valid.length % 2 === 1) return valid[mid];
    return (valid[mid - 1] + valid[mid]) / 2;
}

/**
 * Normalize a scale string to a canonical bucket (e.g. "MB/s", "FPS", "Seconds").
 * Used for cross-benchmark unit detection.
 */
function normalizeHarmonicScaleKey(scale) {
    const rs = (scale || '').trim();
    if (!rs) return null;
    const rsLower = rs.toLowerCase();
    if (rsLower === 'mb/s' || rsLower === 'mib/s') return 'MB/s';
    if (rsLower.includes('byte') && (rs.includes('/') || rsLower.includes('sec') || rsLower.includes(' per '))) {
        return 'MB/s';
    }
    if (rsLower.endsWith('/s') && (rsLower.includes('mib') || rsLower.includes('mb'))) return 'MB/s';
    if (rsLower.includes('fps') || (rsLower.includes('frame') && rsLower.includes('second'))) return 'FPS';
    if (rsLower === 'mips' || rsLower.includes('mips') || rsLower.includes('million instructions')) return 'MIPS';
    if (rsLower.includes('iops')) return 'IOPS';
    if (rsLower.includes('bps')) return 'bps';
    if (rsLower.includes('run') && (rs.includes('/') || rsLower.includes(' per '))) return 'runs/min';
    if (rsLower === 'seconds' || rsLower === 'second' || rsLower === 'sec' || rsLower === 's') return 'Seconds';
    if (rsLower === 'ms' || rsLower.includes('millisecond')) return 'ms';
    return rs;
}

/**
 * Infer direction from a scale key when proportion is missing.
 * Time units → LIB (false), throughput → HIB (true).
 */
function inferHibFromScaleKey(scaleKey) {
    const k = (scaleKey || '').toLowerCase();
    if (!k) return true;
    if (k === 'seconds' || k === 'ms') return false;
    if (k.includes('second') || k.includes('millisecond') || k.includes('time')) return false;
    return true;
}

/**
 * Determine whether a proportion string means "higher is better" or "lower is better".
 */
function isHigherIsBetter(proportion) {
    const p = (proportion || '').trim().toUpperCase();
    if (p === 'HIB' || p === 'LIB') return p === 'HIB';
    const pl = (proportion || '').toLowerCase();
    if (pl.includes('lower') && pl.includes('better')) return false;
    if (pl.includes('higher') && pl.includes('better')) return true;
    if (pl.includes('more') && pl.includes('better')) return true;
    if (pl.includes('lower')) return false;
    return pl.includes('higher') || pl.includes('more');
}

/**
 * Abbreviate a long subtest chart label for display in tight spaces.
 */
function abbreviateSubtestChartLabel(full, maxLen) {
    const cap = maxLen == null ? 26 : maxLen;
    if (!full) return '';
    const raw = String(full).trim();
    if (raw.length <= cap) return raw;
    let s = raw.replace(/\s*\(\d+(?:\.\d+)*\)/g, '').replace(/\s+/g, ' ').trim();
    const pieces = s.split(/\s*[-·]\s*/).map(p => p.trim()).filter(Boolean);
    if (pieces.length >= 2) {
        const trimTail = (t, n) => (t.length > n ? t.slice(0, Math.max(4, n - 1)) + '…' : t);
        const bench = pieces[0].replace(/\s+Compression$/i, '').trim();
        const shortBench = trimTail(bench, 11);
        const mid = pieces.length > 2 ? pieces[1] : '';
        const shortMid = (mid && (mid.length <= 10 || /^[a-z]?\d+[a-z]?$/i.test(mid))) ? mid : '';
        const metric = pieces[pieces.length - 1];
        const shortMetric = trimTail(metric, 13);
        const bits = [shortBench];
        if (shortMid) bits.push(shortMid);
        if (shortMetric && shortMetric !== shortBench && shortMetric !== shortMid) bits.push(shortMetric);
        s = bits.join(' · ');
    }
    if (s.length <= cap) return s;
    return s.slice(0, Math.max(8, cap - 1)) + '…';
}

/** Format a composite bar value (asNormalized: true → relative to 1.0, false → %). */
function formatCompositeBarValue(v, asNormalized) {
    if (v == null || isNaN(v)) return '';
    const n = Number(v);
    if (asNormalized) {
        const pctFromOne = Math.abs(n - 1) * 100;
        if (pctFromOne < 0.01) return n.toFixed(4);
        if (pctFromOne < 0.1) return n.toFixed(3);
        return n.toFixed(2);
    }
    if (Math.abs(n) < 0.05) return '0%';
    return (n >= 0 ? '+' : '') + n.toFixed(1) + '%';
}

/** PTS normalized relative performance label (reference = 1.0). */
function formatPtsRelativeLabel(relative) {
    if (relative == null || isNaN(relative)) return '';
    return formatCompositeBarValue(Number(relative), true);
}

/** Convert capped relative composite (≥1.0) to % advantage vs class reference system. */
function compositeToPercentAdvantage(v) {
    if (v == null || isNaN(v) || Number(v) <= 0) return null;
    return (Number(v) - 1) * 100;
}

/** Bar-chart scale: reference system's composite maps to 100. */
function compositeToPerformanceIndex(v, refComposite) {
    if (v == null || isNaN(v) || Number(v) <= 0) return null;
    const ref = (refComposite != null && !isNaN(refComposite) && Number(refComposite) > 0)
        ? Number(refComposite) : 1;
    return (Number(v) / ref) * 100;
}

/** Data label for performance-index composite bars (100 = reference). */
function formatPerformanceIndexLabel(indexVal) {
    if (indexVal == null || isNaN(indexVal)) return '';
    const n = Number(indexVal);
    const pct = n - 100;
    if (Math.abs(pct) < 0.05) return '100';
    return n.toFixed(1) + ' (' + (pct >= 0 ? '+' : '') + pct.toFixed(1) + '%)';
}

/** PTS viewer raw composite value label (geo/harmonic mean in native units). */
function formatPtsRawMeanLabel(v) {
    if (v == null || isNaN(v)) return '';
    const n = Number(v);
    if (Math.abs(n) >= 10000) return n.toLocaleString(undefined, { maximumFractionDigits: 1 });
    if (Math.abs(n) >= 100) return n.toFixed(1);
    if (Math.abs(n) >= 10) return n.toFixed(2);
    return n.toFixed(3);
}

const CompareUtils = {
    defaultColors,
    colorIndexForSystemId,
    canonicalSystemId,
    sortLabelsValuesBestFirst,
    sortLabelsValuesSystemIdsBestFirst,
    geometricMean,
    harmonicMean,
    percentilePositive,
    medianPositive,
    normalizeHarmonicScaleKey,
    inferHibFromScaleKey,
    isHigherIsBetter,
    abbreviateSubtestChartLabel,
    formatCompositeBarValue,
    formatPtsRelativeLabel,
    compositeToPercentAdvantage,
    compositeToPerformanceIndex,
    formatPerformanceIndexLabel,
    formatPtsRawMeanLabel,
};

if (typeof globalThis !== 'undefined') {
    globalThis.CompareUtils = CompareUtils;
}
if (typeof window !== 'undefined') {
    window.CompareUtils = CompareUtils;
}
