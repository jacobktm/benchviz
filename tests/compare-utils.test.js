import { describe, it, expect } from 'vitest';

// Side-effect import: the IIFE in compare-utils.js sets globalThis.CompareUtils
import '../app/static/js/compare-utils.js';

const {
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
} = globalThis.CompareUtils;

// --------------------------------------------------------------------------
// defaultColors
// --------------------------------------------------------------------------
describe('defaultColors', () => {
    it('has 10 colors', () => {
        expect(defaultColors).toHaveLength(10);
    });

    it('starts with expected tableau palette', () => {
        expect(defaultColors[0]).toBe('#1f77b4');
        expect(defaultColors[1]).toBe('#ff7f0e');
    });
});

// --------------------------------------------------------------------------
// colorIndexForSystemId — stable color lookup across trace names
// --------------------------------------------------------------------------
describe('colorIndexForSystemId', () => {
    const colorOrderIds = ['sys-a', 'sys-b', 'sys-c'];

    it('returns index on exact match', () => {
        expect(colorIndexForSystemId('sys-a', colorOrderIds)).toBe(0);
        expect(colorIndexForSystemId('sys-c', colorOrderIds)).toBe(2);
    });

    it('returns index on prefix match with space separator', () => {
        expect(colorIndexForSystemId('sys-a (SN 123, cooler, 2024-01-01)', colorOrderIds)).toBe(0);
        expect(colorIndexForSystemId('sys-b with extra text', colorOrderIds)).toBe(1);
    });

    it('returns index on prefix match with paren separator', () => {
        expect(colorIndexForSystemId('sys-c(CUDA)', colorOrderIds)).toBe(2);
    });

    it('returns -1 for unknown system', () => {
        expect(colorIndexForSystemId('unknown-system', colorOrderIds)).toBe(-1);
    });

    it('returns -1 for null/undefined', () => {
        expect(colorIndexForSystemId(null, colorOrderIds)).toBe(-1);
        expect(colorIndexForSystemId(undefined, colorOrderIds)).toBe(-1);
    });

    it('works with empty colorOrderIds', () => {
        expect(colorIndexForSystemId('sys-a', [])).toBe(-1);
    });

    it('maintains consistent color for same system across different trace names', () => {
        const ids = ['desktop-01', 'desktop-02'];
        const traceNames = [
            'desktop-01 (SN 100, Noctua, 2024-01-01)',
            'desktop-02 (SN 200, Stock, 2024-06-15)',
        ];
        const indices = traceNames.map(n => colorIndexForSystemId(n, ids));
        expect(indices).toEqual([0, 1]);
    });

    it('handles numeric system IDs', () => {
        expect(colorIndexForSystemId(1, ['0', '1', '2'])).toBe(1);
    });

    // ---- REGRESSION TEST for the color bug ----
    // Systems were losing their graph color across comparisons because
    // non-pooled trace names like "sys-a (SN 1234, NH-D15, 2024-06-01)"
    // were not matching the short ID "sys-a" stored in colorOrderIds.
    it('regression: observation-label trace names resolve to correct system color', () => {
        const ids = ['workstation', 'laptop', 'server'];
        const observationLabels = [
            'workstation (SN 9876, Noctua NH-D15, 2024-03-15)',
            'laptop (SN 5432, Stock Cooler, 2024-06-01)',
            'server (SN 1111, , 2024-01-10)',
        ];
        // Each observation label should map to its system's position in colorOrderIds
        const indices = observationLabels.map(n => colorIndexForSystemId(n, ids));
        expect(indices).toEqual([0, 1, 2]);
    });

    it('regression: pooled trace names with suffix still resolve correctly', () => {
        const ids = ['sys-a', 'sys-b'];
        const pooledNames = [
            'sys-a (CUDA)',
            'sys-b (OpenCL)',
        ];
        const indices = pooledNames.map(n => colorIndexForSystemId(n, ids));
        expect(indices).toEqual([0, 1]);
    });

    it('regression: mixed pooled and non-pooled traces for same system get same color', () => {
        const ids = ['desktop'];
        const names = [
            'desktop',
            'desktop (SN 100, 2024-01-01)',
            'desktop (CUDA)',
            'desktop (OpenCL)',
        ];
        const indices = names.map(n => colorIndexForSystemId(n, ids));
        expect(indices).toEqual([0, 0, 0, 0]);
    });
});

// --------------------------------------------------------------------------
// canonicalSystemId — trace name to short_name resolution
// --------------------------------------------------------------------------
describe('canonicalSystemId', () => {
    const compGroup = {
        system_details: [
            { short_name: 'sys-a' },
            { short_name: 'sys-b' },
            { short_name: 'sys-c' },
        ],
    };

    it('returns short_name on exact match', () => {
        expect(canonicalSystemId('sys-a', compGroup)).toBe('sys-a');
    });

    it('returns short_name on prefix match with space', () => {
        expect(canonicalSystemId('sys-b (SN 123)', compGroup)).toBe('sys-b');
    });

    it('returns short_name on prefix match with paren', () => {
        expect(canonicalSystemId('sys-c(extra)', compGroup)).toBe('sys-c');
    });

    it('falls back to regex extraction for pooled names', () => {
        const result = canonicalSystemId('unknown (CUDA)', compGroup);
        expect(result).toBe('unknown');
    });

    it('returns trace name unchanged when no match found', () => {
        expect(canonicalSystemId('nothing-here', compGroup)).toBe('nothing-here');
    });

    it('returns empty string for null/empty input', () => {
        expect(canonicalSystemId(null, compGroup)).toBe('');
        expect(canonicalSystemId('', compGroup)).toBe('');
    });

    it('handles missing system_details gracefully', () => {
        expect(canonicalSystemId('sys-a', {})).toBe('sys-a');
    });
});

// --------------------------------------------------------------------------
// sortLabelsValuesBestFirst
// --------------------------------------------------------------------------
describe('sortLabelsValuesBestFirst', () => {
    it('sorts descending when higherIsBetter is true', () => {
        const result = sortLabelsValuesBestFirst(['a', 'b', 'c'], [10, 30, 20], true);
        expect(result.labels).toEqual(['b', 'c', 'a']);
        expect(result.values).toEqual([30, 20, 10]);
    });

    it('sorts ascending when higherIsBetter is false', () => {
        const result = sortLabelsValuesBestFirst(['a', 'b', 'c'], [10, 30, 20], false);
        expect(result.labels).toEqual(['a', 'c', 'b']);
        expect(result.values).toEqual([10, 20, 30]);
    });

    it('handles null values', () => {
        const result = sortLabelsValuesBestFirst(['a', 'b'], [10, null], true);
        expect(result.labels).toEqual(['a', 'b']);
        expect(result.values).toEqual([10, null]);
    });
});

// --------------------------------------------------------------------------
// sortLabelsValuesSystemIdsBestFirst
// --------------------------------------------------------------------------
describe('sortLabelsValuesSystemIdsBestFirst', () => {
    it('sorts higher values first when higherIsBetter is true', () => {
        const result = sortLabelsValuesSystemIdsBestFirst(
            ['a', 'b', 'c'], [10, 30, 20], ['sys-a', 'sys-b', 'sys-c'], true,
        );
        expect(result.labels).toEqual(['b', 'c', 'a']);
        expect(result.values).toEqual([30, 20, 10]);
        expect(result.systemIds).toEqual(['sys-b', 'sys-c', 'sys-a']);
    });

    it('sorts lower values first when higherIsBetter is false', () => {
        const result = sortLabelsValuesSystemIdsBestFirst(
            ['a', 'b', 'c'], [10, 30, 20], ['sys-a', 'sys-b', 'sys-c'], false,
        );
        expect(result.labels).toEqual(['a', 'c', 'b']);
    });

    it('handles null values', () => {
        const result = sortLabelsValuesSystemIdsBestFirst(
            ['a', 'b', 'c'], [10, null, 20], ['sys-a', 'sys-b', 'sys-c'], true,
        );
        expect(result.labels).toEqual(['c', 'a', 'b']);
        expect(result.values).toEqual([20, 10, null]);
    });

    it('handles undefined systemIds gracefully', () => {
        const result = sortLabelsValuesSystemIdsBestFirst(['a', 'b'], [10, 20], undefined, true);
        expect(result.systemIds).toEqual([undefined, undefined]);
    });
});

// --------------------------------------------------------------------------
// geometricMean
// --------------------------------------------------------------------------
describe('geometricMean', () => {
    it('computes geometric mean of positive numbers', () => {
        const gm = geometricMean([2, 8]);
        expect(gm).toBeCloseTo(4, 10);
    });

    it('returns null for empty array', () => {
        expect(geometricMean([])).toBeNull();
    });

    it('returns null for array with no positive numbers', () => {
        expect(geometricMean([-1, 0])).toBeNull();
    });

    it('filters out non-positive values', () => {
        const gm = geometricMean([2, 8, -1, 0]);
        expect(gm).toBeCloseTo(4, 10);
    });
});

// --------------------------------------------------------------------------
// harmonicMean
// --------------------------------------------------------------------------
describe('harmonicMean', () => {
    it('computes harmonic mean of positive numbers', () => {
        const hm = harmonicMean([2, 8]);
        expect(hm).toBeCloseTo(3.2, 10);
    });

    it('returns null for empty array', () => {
        expect(harmonicMean([])).toBeNull();
    });

    it('returns null when sum of inversions is zero', () => {
        expect(harmonicMean([0])).toBeNull();
    });
});

// --------------------------------------------------------------------------
// percentilePositive
// --------------------------------------------------------------------------
describe('percentilePositive', () => {
    it('computes median (p=0.5)', () => {
        const result = percentilePositive([1, 2, 3, 4, 5], 0.5);
        expect(result).toBe(3);
    });

    it('computes 100th percentile (max)', () => {
        const result = percentilePositive([3, 1, 4, 1, 5], 1.0);
        expect(result).toBe(5);
    });

    it('returns null for empty array', () => {
        expect(percentilePositive([], 0.5)).toBeNull();
    });

    it('filters out non-positive values', () => {
        const result = percentilePositive([-1, 0, 3, 1, 4], 0.5);
        expect(result).toBe(3);
    });
});

// --------------------------------------------------------------------------
// medianPositive
// --------------------------------------------------------------------------
describe('medianPositive', () => {
    it('returns middle value for odd-length array', () => {
        expect(medianPositive([1, 2, 3])).toBe(2);
    });

    it('returns average of two middle values for even-length array', () => {
        expect(medianPositive([1, 2, 3, 4])).toBe(2.5);
    });

    it('filters out non-positive values', () => {
        expect(medianPositive([-1, 0, 1, 2, 3])).toBe(2);
    });

    it('returns null for empty array', () => {
        expect(medianPositive([])).toBeNull();
    });
});

// --------------------------------------------------------------------------
// normalizeHarmonicScaleKey
// --------------------------------------------------------------------------
describe('normalizeHarmonicScaleKey', () => {
    it('normalizes MB/s variants', () => {
        expect(normalizeHarmonicScaleKey('MB/s')).toBe('MB/s');
        expect(normalizeHarmonicScaleKey('MiB/s')).toBe('MB/s');
        expect(normalizeHarmonicScaleKey('mb/s')).toBe('MB/s');
    });

    it('normalizes FPS variants', () => {
        expect(normalizeHarmonicScaleKey('FPS')).toBe('FPS');
        expect(normalizeHarmonicScaleKey('Frames Per Second')).toBe('FPS');
    });

    it('normalizes time units', () => {
        expect(normalizeHarmonicScaleKey('Seconds')).toBe('Seconds');
        expect(normalizeHarmonicScaleKey('ms')).toBe('ms');
    });

    it('returns null for empty string', () => {
        expect(normalizeHarmonicScaleKey('')).toBeNull();
        expect(normalizeHarmonicScaleKey(null)).toBeNull();
    });

    it('returns unrecognized scale unchanged', () => {
        expect(normalizeHarmonicScaleKey('Custom Unit')).toBe('Custom Unit');
    });
});

// --------------------------------------------------------------------------
// inferHibFromScaleKey
// --------------------------------------------------------------------------
describe('inferHibFromScaleKey', () => {
    it('returns false for time-based units', () => {
        expect(inferHibFromScaleKey('Seconds')).toBe(false);
        expect(inferHibFromScaleKey('ms')).toBe(false);
    });

    it('returns true for throughput units', () => {
        expect(inferHibFromScaleKey('MB/s')).toBe(true);
        expect(inferHibFromScaleKey('FPS')).toBe(true);
    });

    it('returns true for empty key', () => {
        expect(inferHibFromScaleKey('')).toBe(true);
    });
});

// --------------------------------------------------------------------------
// isHigherIsBetter
// --------------------------------------------------------------------------
describe('isHigherIsBetter', () => {
    it('returns true for HIB', () => {
        expect(isHigherIsBetter('HIB')).toBe(true);
    });

    it('returns false for LIB', () => {
        expect(isHigherIsBetter('LIB')).toBe(false);
    });

    it('returns true for natural language higher is better', () => {
        expect(isHigherIsBetter('Higher is better')).toBe(true);
    });

    it('returns false for natural language lower is better', () => {
        expect(isHigherIsBetter('Lower is better')).toBe(false);
    });

    it('returns false for null/empty', () => {
        expect(isHigherIsBetter(null)).toBe(false);
        expect(isHigherIsBetter('')).toBe(false);
    });

    it('handles edge case: more is better', () => {
        expect(isHigherIsBetter('More is better')).toBe(true);
    });
});

// --------------------------------------------------------------------------
// abbreviateSubtestChartLabel
// --------------------------------------------------------------------------
describe('abbreviateSubtestChartLabel', () => {
    it('returns empty for null/undefined', () => {
        expect(abbreviateSubtestChartLabel(null)).toBe('');
        expect(abbreviateSubtestChartLabel(undefined)).toBe('');
    });

    it('returns short strings unchanged', () => {
        expect(abbreviateSubtestChartLabel('hello')).toBe('hello');
    });

    it('truncates long strings without separators', () => {
        const long = 'a'.repeat(50);
        const result = abbreviateSubtestChartLabel(long, 20);
        expect(result).toHaveLength(20);
        expect(result).toMatch(/^a+…$/);
    });

    it('abbreviates structured benchmark names', () => {
        const label = '7-Zip Compression - Compression Rating (MIPS)';
        const result = abbreviateSubtestChartLabel(label);
        expect(result.length).toBeLessThan(label.length);
        expect(result).toContain('·');
    });
});

// --------------------------------------------------------------------------
// formatCompositeBarValue
// --------------------------------------------------------------------------
describe('formatCompositeBarValue', () => {
    it('formats normalized values with precision tiers', () => {
        expect(formatCompositeBarValue(1.00001, true)).toBe('1.0000');
        expect(formatCompositeBarValue(1.0008, true)).toBe('1.001');
        expect(formatCompositeBarValue(1.05, true)).toBe('1.05');
    });

    it('formats non-normalized values as percentages', () => {
        expect(formatCompositeBarValue(0.0499, false)).toBe('0%');
        expect(formatCompositeBarValue(0.123, false)).toBe('+0.1%');
        expect(formatCompositeBarValue(-0.05, false)).toBe('-0.1%');
    });

    it('returns empty for null/NaN', () => {
        expect(formatCompositeBarValue(null, true)).toBe('');
        expect(formatCompositeBarValue(undefined, true)).toBe('');
        expect(formatCompositeBarValue(NaN, true)).toBe('');
    });
});

// --------------------------------------------------------------------------
// formatPtsRelativeLabel
// --------------------------------------------------------------------------
describe('formatPtsRelativeLabel', () => {
    it('formats relative value', () => {
        expect(formatPtsRelativeLabel(1.05)).toBe('1.05');
    });

    it('returns empty for null/NaN', () => {
        expect(formatPtsRelativeLabel(null)).toBe('');
    });
});

// --------------------------------------------------------------------------
// compositeToPercentAdvantage
// --------------------------------------------------------------------------
describe('compositeToPercentAdvantage', () => {
    it('converts composite to percent advantage', () => {
        expect(compositeToPercentAdvantage(1.5)).toBe(50);
        expect(compositeToPercentAdvantage(2.0)).toBe(100);
    });

    it('returns null for invalid input', () => {
        expect(compositeToPercentAdvantage(null)).toBeNull();
        expect(compositeToPercentAdvantage(0)).toBeNull();
    });
});

// --------------------------------------------------------------------------
// compositeToPerformanceIndex
// --------------------------------------------------------------------------
describe('compositeToPerformanceIndex', () => {
    it('computes performance index', () => {
        expect(compositeToPerformanceIndex(2, 1)).toBe(200);
    });

    it('defaults refComposite to 1 when null', () => {
        expect(compositeToPerformanceIndex(1.5, null)).toBe(150);
    });

    it('returns null for invalid input', () => {
        expect(compositeToPerformanceIndex(null, 1)).toBeNull();
    });
});

// --------------------------------------------------------------------------
// formatPerformanceIndexLabel
// --------------------------------------------------------------------------
describe('formatPerformanceIndexLabel', () => {
    it('formats 100 as bare number', () => {
        expect(formatPerformanceIndexLabel(100)).toBe('100');
    });

    it('shows percent change for non-baseline values', () => {
        const label = formatPerformanceIndexLabel(125.0);
        expect(label).toContain('125.0');
        expect(label).toContain('+25.0');
    });

    it('returns empty for null', () => {
        expect(formatPerformanceIndexLabel(null)).toBe('');
    });
});

// --------------------------------------------------------------------------
// formatPtsRawMeanLabel
// --------------------------------------------------------------------------
describe('formatPtsRawMeanLabel', () => {
    it('formats large values with locale separators', () => {
        const formatted = formatPtsRawMeanLabel(12345);
        // Result includes a thousands separator (locale-dependent: ',' '.' or ' ')
        expect(Number(formatted.replace(/[^\d.]/g, ''))).toBe(12345);
    });

    it('formats medium values with 1 decimal', () => {
        expect(formatPtsRawMeanLabel(500)).toBe('500.0');
    });

    it('formats small values with 3 decimals', () => {
        expect(formatPtsRawMeanLabel(0.5)).toBe('0.500');
    });

    it('returns empty for null', () => {
        expect(formatPtsRawMeanLabel(null)).toBe('');
    });
});
